import pandas as pd
from pytorch_forecasting import TimeSeriesDataSet, TemporalFusionTransformer
from pytorch_forecasting.data import GroupNormalizer
from pytorch_forecasting.metrics import QuantileLoss
from pytorch_forecasting.data.encoders import NaNLabelEncoder # Added import
import torch
import os
import sys
import multiprocessing

# Set multiprocessing start method to 'spawn' for compatibility with CUDA and multi-GPU
multiprocessing.set_start_method('spawn', force=True)

def pick_trainer_lib(model):
    """
    Pick the Lightning package that matches the model's LightningModule base class.
    Returns the module (either lightning.pytorch or pytorch_lightning) to use for Trainer.
    """
    lpl = None
    oldpl = None
    # try modern namespace first
    try:
        import lightning.pytorch as lightning_modern
        lpl = lightning_modern
    except Exception:
        lpl = None
    try:
        import pytorch_lightning as lightning_old
        oldpl = lightning_old
    except Exception:
        oldpl = None
    # Check isinstance against whichever was imported
    if lpl is not None:
        LightningModule = getattr(lpl, "LightningModule", object)
        try:
            if isinstance(model, LightningModule):
                print("Using lightning.pytorch (modern) for Trainer / LightningModule.")
                return lpl
        except Exception:
            pass
    if oldpl is not None:
        LightningModule = getattr(oldpl, "LightningModule", object)
        try:
            if isinstance(model, LightningModule):
                print("Using pytorch_lightning (legacy) for Trainer / LightningModule.")
                return oldpl
        except Exception:
            pass
    # If no direct match, prefer modern if available (but warn)
    if lpl is not None:
        print("Warning: model did not pass isinstance check but lightning.pytorch is available — attempting to use it.")
        return lpl
    if oldpl is not None:
        print("Warning: model did not pass isinstance check but pytorch_lightning is available — attempting to use it.")
        return oldpl
    raise ImportError("Neither 'lightning.pytorch' nor 'pytorch_lightning' is importable. "
                      "Install one of them so Trainer can be created (e.g. pip install lightning).")
def main():
    print("Starting model training...")
    df = pd.read_csv('processed_data.csv', parse_dates=['timestamp'], dtype={'chunk_id': str})
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    print("Processed data loaded. Shape:", df.shape)
    # Percentage-based split (80% train, 20% val) on sorted data
    num_rows = len(df)
    train_size = int(0.8 * num_rows)
    train_df = df.iloc[:train_size].copy()
    val_df = df.iloc[train_size:].copy()
    print("Data split: Train shape:", train_df.shape, "Val shape:", val_df.shape)
    # Removed categorical conversion for hour/day_of_week (features removed)
    max_encoder_length = 30
    max_prediction_length = 10
    print(f"Encoder length: {max_encoder_length}, Prediction length: {max_prediction_length}")
    # Fit encoder on full data to handle all chunk_ids (avoids unknown category errors)
    categorical_encoders = {'chunk_id': NaNLabelEncoder(add_nan=True).fit(df['chunk_id'])}
    print("Creating training dataset...")
    training = TimeSeriesDataSet(
        train_df,
        time_idx='time_idx',
        target='target_h1',
        group_ids=['chunk_id'], # Use chunk_id for grouping to reduce memorization
        min_encoder_length=5,
        max_encoder_length=max_encoder_length,
        max_prediction_length=max_prediction_length,
        static_categoricals=[],
        time_varying_known_categoricals=[], # Removed 'hour', 'day_of_week'
        time_varying_known_reals=['time_idx', 'atr', 'ema_fast', 'ema_slow', 'macd', 'macd_signal'],
        time_varying_unknown_categoricals=[],
        time_varying_unknown_reals=[
            'open', 'high', 'low', 'close', 'volume', 'rsi', 'high_prob', 'low_prob',
            'bullish_signal', 'bearish_signal', 'close_lag1', 'close_lag2', 'close_lag3', 'close_lag4', 'close_lag5'
        ],
        target_normalizer=GroupNormalizer(groups=[], transformation=None), # Global normalizer, no transformation for log returns
        categorical_encoders=categorical_encoders, # Added to handle all chunk_ids
        add_relative_time_idx=True,
        add_target_scales=True,
        add_encoder_length=True,
    )
    print("Training dataset created.")
    params = training.get_parameters()
    encoders = training.categorical_encoders
    torch.save({"params": params, "encoders": encoders}, "dataset_params.pt")
    print("Dataset params and encoders saved as dataset_params.pt for live use.")
    print("Creating validation dataset...")
    validation = TimeSeriesDataSet.from_dataset(training, val_df, predict=True, stop_randomization=True)
    print("Validation dataset created.")
    print("Initializing TFT model...")
    tft = TemporalFusionTransformer.from_dataset(
        training,
        hidden_size=64,  # Increased for better capacity
        lstm_layers=3,  # Increased for better sequence modeling
        dropout=0.1,
        attention_head_size=4,  # Increased for better attention
        output_size=7,
        loss=QuantileLoss(),
        log_interval=10,
        reduce_on_plateau_patience=4,
        learning_rate=0.01,  # Slightly reduced for stability
    )
    print("Model initialized.")
    print("tft object module:", type(tft).__module__, "type:", type(tft))
    # pick matching Trainer implementation automatically
    pl = pick_trainer_lib(tft)
    # conservative dataloader/trainer settings to avoid worker/memory issues
    # Use 'bf16-mixed' precision to avoid float16 overflow in attention masking while preserving training quality
    trainer = pl.Trainer(
        max_epochs=50,  # Increased for more training
        gradient_clip_val=0.1,
        log_every_n_steps=10,
        accelerator='gpu' if torch.cuda.is_available() else 'auto',
        devices='auto' if torch.cuda.is_available() else None,
        precision='bf16-mixed', # Changed to 'bf16-mixed' to prevent overflow in attention mask
        callbacks=[pl.callbacks.EarlyStopping(monitor="val_loss", patience=10, mode="min")], # Increased patience
    )
    # safer defaults: don't spawn too many workers by default
    num_workers = min(os.cpu_count() or 4, 4) # Limit to 4 workers to avoid resource issues
    batch_size = 256 # Increased for better GPU utilization (adjust down if OOM)
    print(f"Using {num_workers} workers for dataloaders and batch_size={batch_size}.")
    # Optional: Scale LR for larger batch (to maintain quality)
    # tft.hparams.learning_rate *= (batch_size / 128) ** 0.5
    train_dataloader = training.to_dataloader(
        train=True,
        batch_size=batch_size,
        num_workers=num_workers,
        persistent_workers=False, # Disable persistent workers to avoid fork issues
        pin_memory=True, # Faster CPU-to-GPU transfer
    )
    val_dataloader = validation.to_dataloader(
        train=False,
        batch_size=batch_size,
        num_workers=num_workers,
        persistent_workers=False, # Disable persistent workers to avoid fork issues
        pin_memory=True,
    )
    # Optimize CUDA kernels
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
    print("Dataloaders created. Starting training (this may take time)...")
    trainer.fit(tft, train_dataloaders=train_dataloader, val_dataloaders=val_dataloader)
    trainer.save_checkpoint('best_model.ckpt')
    print("Training complete. Checkpoint saved as best_model.ckpt")
    print("Converting model to CPU-compatible format...")
    checkpoint = torch.load('best_model.ckpt', map_location=torch.device('cpu'), weights_only=False)
    model_state = checkpoint['state_dict']
    torch.save(model_state, 'cpu_model.pth')
    print("CPU model saved as cpu_model.pth")
if __name__ == "__main__":
    main()