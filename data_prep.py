import pandas as pd
import numpy as np
from scipy.stats import gaussian_kde
from scipy.signal import argrelextrema
import talib  # Or import pandas_ta as pta if using alternative

def compute_rsi(df, period=14):
    print("Computing RSI...")
    return talib.RSI(df['close'], timeperiod=period)

def compute_custom_rsi_kde(df, rsi_period=14, pivot_len=14, bandwidth=2.71828, steps=100, threshold=0.25):
    print("Computing custom RSI-KDE...")
    df['rsi'] = compute_rsi(df, rsi_period)
    
    # Find pivots
    highs = argrelextrema(df['high'].values, np.greater, order=pivot_len)[0]
    lows = argrelextrema(df['low'].values, np.less, order=pivot_len)[0]
    print(f"Found {len(highs)} high pivots and {len(lows)} low pivots.")
    
    # RSI at pivots (use ALL pivots, removed [-300:] limit for better balance)
    high_rsi = df['rsi'].iloc[highs].dropna().values
    low_rsi = df['rsi'].iloc[lows].dropna().values
    
    # KDE for high/low pivots
    if len(high_rsi) > 0:
        kde_high = gaussian_kde(high_rsi, bw_method=1/bandwidth)
        x_high = np.linspace(min(high_rsi) - 1, max(high_rsi) + 1, steps)  # Slight padding
        y_high = kde_high(x_high)
        max_high_prob = y_high.max()
        print("High KDE computed.")
    else:
        max_high_prob = np.nan
        print("No high RSI values for KDE.")
    
    if len(low_rsi) > 0:
        kde_low = gaussian_kde(low_rsi, bw_method=1/bandwidth)
        x_low = np.linspace(min(low_rsi) - 1, max(low_rsi) + 1, steps)
        y_low = kde_low(x_low)
        max_low_prob = y_low.max()
        print("Low KDE computed.")
    else:
        max_low_prob = np.nan
        print("No low RSI values for KDE.")
    
    # Compute probs for each bar
    df['high_prob'] = np.nan
    df['low_prob'] = np.nan
    print("Computing probabilities for each bar...")
    for i in range(len(df)):
        rsi_val = df['rsi'].iloc[i]
        if not np.isnan(rsi_val):
            if len(high_rsi) > 0:
                nearest_high = np.argmin(np.abs(x_high - rsi_val))
                df.loc[df.index[i], 'high_prob'] = y_high[nearest_high] / max_high_prob if max_high_prob else 0
            if len(low_rsi) > 0:
                nearest_low = np.argmin(np.abs(x_low - rsi_val))
                df.loc[df.index[i], 'low_prob'] = y_low[nearest_low] / max_low_prob if max_low_prob else 0
    
    # Signals (kept as features, but not used for gating decisions)
    df['bullish_signal'] = (df['low_prob'] > (1 - threshold)) & (df['low_prob'] > df['high_prob'])
    df['bearish_signal'] = (df['high_prob'] > (1 - threshold)) & (df['high_prob'] > df['low_prob'])
    # Removed unused possible_pivot columns
    print("Signals computed.")
    
    # Log signal balance for monitoring
    num_bullish = df['bullish_signal'].sum()
    num_bearish = df['bearish_signal'].sum()
    print(f"Signal balance: Bullish signals={num_bullish}, Bearish signals={num_bearish}")
    
    return df

def create_bearish_dataset(df):
    print("Creating bearish dataset for data augmentation...")
    df_bear = df.copy()
    # Reflect prices around the mean to simulate a bearish trend
    mean_price = df['close'].mean()
    df_bear['open'] = 2 * mean_price - df['close']
    df_bear['close'] = 2 * mean_price - df['open']
    df_bear['high'] = 2 * mean_price - df['low']
    df_bear['low'] = 2 * mean_price - df['high']
    # Keep volume the same, as it's activity
    # Shift timestamps to avoid overlap (add max duration)
    max_timestamp = df.index.max()
    df_bear.index = df_bear.index + (max_timestamp - df.index.min() + pd.Timedelta(days=1))
    # Prefix chunk_id to distinguish
    df_bear['chunk_id'] = 'bear_' + df_bear['chunk_id']
    print("Bearish dataset created.")
    return df_bear

def main():
    print("Starting data preparation...")
    # Load data
    df = pd.read_csv('xauusd_data.csv', parse_dates=['timestamp'])
    print("Data loaded from xauusd_data.csv. Shape:", df.shape)
    df = df.sort_values('timestamp').set_index('timestamp')
    print("Data sorted and indexed.")
    
    # Add core features to original before augmentation
    df['time_idx'] = range(len(df))
    df['group'] = 'XAUUSD'
    chunk_size = 10080  # 60 min * 24 hours * 7 days
    df['chunk_id'] = (df['time_idx'] // chunk_size).astype(str)
    print("Core features added to original, including chunk_id.")
    
    # Create bearish augmented data
    df_bear = create_bearish_dataset(df)
    
    # Concatenate original and bearish
    df = pd.concat([df, df_bear])
    print("Augmented data (original + bearish). New shape:", df.shape)
    
    # Sort by timestamp to ensure chronological order
    df = df.sort_index()
    
    # Reset time_idx to be continuous after concatenation
    df['time_idx'] = range(len(df))
    print("Data sorted by timestamp, time_idx reset.")
    
    # Add simple indicators
    df['ema_fast'] = talib.EMA(df['close'], timeperiod=12)
    df['ema_slow'] = talib.EMA(df['close'], timeperiod=26)
    df['macd'], df['macd_signal'], _ = talib.MACD(df['close'], fastperiod=12, slowperiod=26, signalperiod=9)
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
    print("Simple indicators (EMA, MACD, ATR) computed.")
    
    # Lags
    for lag in range(1, 6):
        df[f'close_lag{lag}'] = df['close'].shift(lag)
    print("Lags added.")
    
    # Custom RSI-KDE (your indicator)
    df = compute_custom_rsi_kde(df)
    print("Custom RSI-KDE features added.")
    
    # Target: Log returns for next 1-10 bars (multi-horizon)
    for h in range(1, 11):
        df[f'target_h{h}'] = np.log(df['close'].shift(-h) / df['close'])
    print("Targets (log returns for horizons 1-10) added.")
    
    # Drop NaNs (from shifts/indicators)
    initial_shape = df.shape
    df = df.dropna()
    print(f"NaNs dropped. Rows reduced from {initial_shape[0]} to {df.shape[0]}.")
    
    # Save processed data
    df.to_csv('processed_data.csv')
    print("Processed data saved to processed_data.csv")
    print("Data preparation complete.")

if __name__ == "__main__":
    main()