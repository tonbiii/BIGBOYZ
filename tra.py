# tra.py (Fixed: Removed broker_offset completely; all timestamps/queries/grouping now in UTC using raw Unix server time. Fetch ranges broadened to 5 years back. Timestamps in logs/predictions use ISO UTC+Z format. No local TZ conversions.)
# CHANGE: In normal range with high ROC, flip and place instead of skip (sets flipped=True, but no count increment unless flip-range).
# UPDATE: MT5 connection prioritizes manual terminal login; fallback to auto. Logs successful login with account/symbol. Fixed datetime deprecation.
# NEW: Pip calculation constant via symbol_info.digits/point; for gold, pip_size = point (0.01); SL/TP offset = pips * pip_size
# NEW: Sharpe (mean(profits)/std(profits)), Profit Factor (sum(profits)/abs(sum(losses))) in closed_trades summary/groups
# NEW: GMT trading hours check - skip cycles outside configured hours (auto on/off daily if enabled)
# NEW: Lot sizing for losses: symmetric to profits, with configurable thresholds
# NEW: Weekly basis for halving/quartering instead of daily; added trailing profit protection with activation and trail pct (additional x0.5 on drop)
import time
import pandas as pd
import numpy as np
from scipy.stats import gaussian_kde
from scipy.signal import argrelextrema
import talib
import MetaTrader5 as mt5
from pytorch_forecasting import TimeSeriesDataSet, TemporalFusionTransformer
import torch
from pytorch_forecasting.metrics import QuantileLoss
import json # For loading config from JSON
import pytorch_forecasting
import lightning.pytorch as pl
import threading
import sys
import io
from datetime import datetime, timedelta, date, timezone
import os
import glob
import math # For ROC calc (though not used now, kept for future)
import csv # Added for robust CSV handling
# Global control flag and logs
is_running = False
system_logs = [] # In-memory system logs for dashboard
prediction_logs = [] # In-memory prediction logs for dashboard
lock = threading.Lock() # For thread-safe log access
stop_event = None # For immediate stop signaling
trading_thread = None # Global for aggressive stop
config_data = {} # Global config
mt5_connected = False # New: Global connection state
high_weekly_profit = 0.0 # New: Trail high water mark
last_week = None # New: Track week for reset
# Hardcoded defaults (replaces defaults.json file dependency)
HARDCODED_DEFAULTS = {
    "SYMBOL": "XAUUSDm",
    "MT5_ACCOUNT": 0,
    "MT5_PASSWORD": "",
    "MT5_SERVER": "",
    "BASE_BALANCE": 1000,
    "BASE_LOT_SIZE": 0.1,
    "MAX_OPEN_TRADES": 5,
    "STOP_LOSS_PIPS": 300,
    "TAKE_PROFIT_PIPS": 600,
    "START_TIME_GMT": "07:00",
    "END_TIME_GMT": "17:00",
    "PROFIT_HALF_PCT": 100.0,
    "PROFIT_QUARTER_PCT": 200.0,
    "LOSS_HALF_PCT": 5.0,
    "LOSS_QUARTER_PCT": 15.0,
    "TRAIL_ACTIVATION_PCT": 50.0,
    "TRAIL_PCT": 20.0,
    "TIMEFRAME": "M1",
    "BARS_TO_FETCH": 10000,
    "NORMAL_LOW_1": 0.00001,
    "NORMAL_HIGH_1": 0.000015,
    "FLIP_LOW_1": 0.000016,
    "FLIP_HIGH_1": 0.00002,
    "NORMAL_LOW_2": 0.000023,
    "NORMAL_HIGH_2": 0.00003,
    "FLIP_LOW_2": 0.000032,
    "FLIP_HIGH_2": 0.00004,
    "FLIP_MAX_CONSECUTIVE": 10,
    "LOOP_INTERVAL_SECONDS": 60,
    "MAX_ENCODER_LENGTH": 30,
    "MAX_PREDICTION_LENGTH": 10,
    "ROC_WINDOW": 4,
    "NORMAL_ROC_SENSITIVITY": 1.0,
    "ROC_TRADE_SENSITIVITY": 1.0
}
# Load config from JSON file
def load_config():
    global config_data
    try:
        with open('config.json', 'r') as f:
            config_data = json.load(f)
    except FileNotFoundError:
        config_data = HARDCODED_DEFAULTS.copy()
        save_config() # Save defaults as config.json
    except json.JSONDecodeError:
        config_data = HARDCODED_DEFAULTS.copy()
        save_config() # Overwrite invalid with defaults
    else:
        # Add any missing keys from hardcoded defaults
        changed = False
        for k, v in HARDCODED_DEFAULTS.items():
            if k not in config_data:
                config_data[k] = v
                changed = True
        if changed:
            save_config()
    return config_data
def save_config():
    global config_data
    with open('config.json', 'w') as f:
        json.dump(config_data, f, indent=4)
def ensure_mt5_connection():
    global mt5_connected
    old_connected = mt5_connected
    # First, check if already logged in (manual terminal session)
    if mt5.account_info() is not None:
        mt5_connected = True
        # Select symbol with retry
        for attempt in range(3):
            if mt5.symbol_select(config_data['SYMBOL'], True):
                break
            time.sleep(1)
        else:
            captured_print(f"Warning: Failed to select symbol {config_data['SYMBOL']} after retries")
        if not old_connected:
            info = mt5.account_info()
            captured_print(f"Using existing login: account {info.login}, symbol {config_data['SYMBOL']}")
        return True
    # Fallback: Try automatic login
    captured_print("No active login detected—attempting auto-login...")
    mt5.shutdown() # Clean slate for re-init
    if not mt5.initialize(login=config_data['MT5_ACCOUNT'], server=config_data['MT5_SERVER'], password=config_data['MT5_PASSWORD']):
        code, msg = mt5.last_error()
        captured_print(f"MT5 auto-init failed: Code {code}: {msg}")
        mt5_connected = False
        return False
    # Verify login succeeded
    account_info = mt5.account_info()
    if not account_info:
        captured_print("Auto-init succeeded but account info unavailable—possible credential issue")
        mt5.shutdown()
        mt5_connected = False
        return False
    mt5_connected = True
    # Select symbol with retry
    for attempt in range(3):
        if mt5.symbol_select(config_data['SYMBOL'], True):
            break
        time.sleep(1)
    else:
        captured_print(f"Warning: Failed to select symbol {config_data['SYMBOL']} after retries")
    captured_print(f"Auto login success: account {account_info.login}, server {config_data['MT5_SERVER']}, symbol {config_data['SYMBOL']}")
    return True
# NEW: Get real-time conversion rate to USD using forex pairs
def get_usd_conversion_rate(account_currency):
    if account_currency == 'USD':
        return 1.0
    if not ensure_mt5_connection():
        captured_print("Cannot fetch USD rate: MT5 not connected")
        return 1.0  # Fallback
    # Try currency + 'USD' (e.g., GBPUSD)
    pair_direct = account_currency + 'USD'
    pair_inverse = 'USD' + account_currency
    rate = None
    for pair in [pair_direct, pair_inverse]:
        if mt5.symbol_select(pair, True):
            rates = mt5.copy_rates_from_pos(pair, mt5.TIMEFRAME_M1, 0, 1)
            if rates is not None and len(rates) > 0:
                close = rates[0]['close']
                if pair == pair_direct:
                    rate = close  # USD per unit currency
                else:
                    rate = 1 / close if close != 0 else None  # Unit currency per USD → USD per unit = 1/close
                if rate:
                    captured_print(f"USD rate for {account_currency} via {pair}: {rate:.4f}")
                    return rate
    captured_print(f"Warning: No pair found for {account_currency} to USD - assuming rate 1.0")
    return 1.0
# New: Check if current UTC time is within GMT trading hours (GMT=UTC)
def is_within_hours():
    config = load_config()
    try:
        start_t = datetime.strptime(config['START_TIME_GMT'], "%H:%M").time()
        end_t = datetime.strptime(config['END_TIME_GMT'], "%H:%M").time()
        now = datetime.now(timezone.utc).time()
        return now >= start_t and now < end_t
    except ValueError:
        captured_print("Invalid trading hours format - assuming always allowed")
        return True
def get_today_csv(prefix):
    # Use UTC date for CSV filenames (consistent with raw server time)
    today = datetime.now(timezone.utc).date().isoformat()
    csv_path = f"{prefix}_{today}.csv"
    # Ensure directory exists
    os.makedirs(os.path.dirname(csv_path) or '.', exist_ok=True)
    return csv_path
def load_all_predictions():
    global prediction_logs
    pattern = "predictions_*.csv"
    files = glob.glob(pattern)
    captured_print(f"Searching for predictions CSVs: {pattern}, found {len(files)} files")
    if not files:
        prediction_logs = []
        captured_print("No predictions CSVs found - starting empty")
        return
    # Parse dates and sort files by date
    file_dates = []
    for f in files:
        basename = os.path.basename(f)
        if basename.startswith("predictions_") and basename.endswith(".csv"):
            date_str = basename[len("predictions_"):-len(".csv")]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                file_dates.append((file_date, f))
            except ValueError:
                captured_print(f"Skipping invalid date file: {basename}")
                continue
    if not file_dates:
        prediction_logs = []
        captured_print("No valid dated predictions CSVs found")
        return
    file_dates.sort(key=lambda x: x[0]) # Sort chronologically by date
    df_all = pd.DataFrame()
    load_errors = 0
    for file_date, f in file_dates:
        try:
            captured_print(f"Loading predictions from {file_date}: {f}")
            df_day = pd.read_csv(f)
            # Fix NaN issues to prevent JSON errors (added new fields)
            df_day = df_day.fillna({
                'action': 'None',
                'median_return': 0.0,
                'open_trades': 0,
                'lot_size': 0.0,
                'entry_price': 0.0,
                'roc': 0.0,
                'flip_consecutive': 0,
                'pause_flips': False
            })
            # Ensure bool columns are properly typed (NaN -> False)
            bool_cols = ['bullish_signal', 'bearish_signal', 'flipped', 'executed', 'paused', 'pause_flips']
            for col in bool_cols:
                if col in df_day.columns:
                    df_day[col] = df_day[col].fillna(False).astype(bool)
            # Ensure numerics (added new)
            numeric_cols = ['median_return', 'open_trades', 'lot_size', 'entry_price', 'roc', 'flip_consecutive']
            for col in numeric_cols:
                if col in df_day.columns:
                    df_day[col] = pd.to_numeric(df_day[col], errors='coerce').fillna(0)
            df_all = pd.concat([df_all, df_day], ignore_index=True)
            captured_print(f"Loaded {len(df_day)} predictions from {file_date}")
        except Exception as e:
            load_errors += 1
            captured_print(f"Error loading {f}: {e}")
    if load_errors > 0:
        captured_print(f"Warning: {load_errors} files failed to load")
    if not df_all.empty:
        # Sort all by timestamp for chronological order
        if 'timestamp' in df_all.columns:
            df_all['timestamp'] = pd.to_datetime(df_all['timestamp'], utc=True, errors='coerce')
            df_all = df_all.sort_values('timestamp').reset_index(drop=True)
            # Fixed: Convert timestamp to str for JSON serialization (ISO Z)
            df_all['timestamp'] = df_all['timestamp'].apply(lambda x: x.isoformat().replace('+00:00', 'Z') if pd.notnull(x) else 'Invalid')
        prediction_logs = df_all.to_dict('records')
        captured_print(f"Loaded total {len(prediction_logs)} predictions from {len(file_dates)} days")
    else:
        prediction_logs = []
        captured_print("All predictions CSVs were empty after loading")
def load_daily_system_logs():
    global system_logs
    csv_file = get_today_csv("system_logs")
    captured_print(f"Attempting to load system logs from: {csv_file}")
    if os.path.exists(csv_file):
        try:
            # Use csv reader to load, skipping bad lines
            with open(csv_file, 'r', newline='') as f:
                reader = csv.reader(f)
                system_logs = []
                for row in reader:
                    if len(row) == 2:
                        system_logs.append(row[1]) # Only message
                    else:
                        captured_print(f"Skipping bad row in system_logs CSV: {row}")
            captured_print(f"Loaded {len(system_logs)} system logs from CSV (after skipping bad rows)")
        except Exception as e:
            print(f"Error loading system logs CSV: {e}")
            system_logs = []
            captured_print("System logs CSV load failed - starting empty")
    else:
        system_logs = []
        captured_print("No system logs CSV found - will create on first log")
def save_prediction(trade_info):
    csv_file = get_today_csv("predictions")
    captured_print(f"Attempting to save prediction to: {csv_file}")
    # Ensure no None/NaN before saving (added new fields)
    trade_info_safe = trade_info.copy()
    trade_info_safe['action'] = trade_info_safe.get('action', 'None') or 'None'
    trade_info_safe['roc'] = trade_info_safe.get('roc', 0.0) or 0.0
    trade_info_safe['flip_consecutive'] = trade_info_safe.get('flip_consecutive', 0) or 0
    trade_info_safe['pause_flips'] = trade_info_safe.get('pause_flips', False) or False
    df_new = pd.DataFrame([trade_info_safe])
    df_new = df_new.fillna({
        'action': 'None',
        'median_return': 0.0,
        'open_trades': 0,
        'lot_size': 0.0,
        'entry_price': 0.0,
        'roc': 0.0,
        'flip_consecutive': 0,
        'pause_flips': False
    })
    bool_cols = ['bullish_signal', 'bearish_signal', 'flipped', 'executed', 'paused', 'pause_flips']
    for col in bool_cols:
        if col in df_new.columns:
            df_new[col] = df_new[col].astype(bool)
    try:
        if os.path.exists(csv_file):
            df_old = pd.read_csv(csv_file)
            df = pd.concat([df_old, df_new], ignore_index=True)
            captured_print("Appended to existing predictions CSV")
        else:
            df = df_new
            captured_print("Created new predictions CSV")
        df.to_csv(csv_file, index=False)
        captured_print("Prediction saved successfully to CSV")
    except PermissionError as e:
        captured_print(f"Permission denied saving to {csv_file}: {e} - Check file permissions")
    except Exception as e:
        print(f"Error saving prediction: {e}")
        captured_print(f"Failed to save prediction to CSV: {e}")
def save_system_log(msg):
    csv_file = get_today_csv("system_logs")
    # Use UTC with Z for consistency in CSV timestamp (fixed deprecation)
    timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    new_row = [timestamp, msg]
    try:
        if os.path.exists(csv_file):
            with open(csv_file, 'r', newline='') as f:
                reader = csv.reader(f)
                old_data = list(reader)
        else:
            old_data = [['timestamp', 'message']] # Header if new
        old_data.append(new_row)
        with open(csv_file, 'w', newline='') as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL) # Quote all to handle commas in msg
            writer.writerows(old_data)
    except Exception as e:
        print(f"Error saving system log: {e}")
        print(f"Failed to save log to CSV: {e}")
# Custom print function to capture output
def captured_print(*args, **kwargs):
    # Use UTC time for log timestamps
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    msg = f"[{ts}] {' '.join(map(str, args))}"
    save_system_log(msg)
    with lock:
        system_logs.append(msg)
        if len(system_logs) > 1000: # Limit logs to last 1000 for memory
            system_logs.pop(0)
    print(msg)
# Load logs on startup
load_all_predictions()
load_daily_system_logs()
load_config() # Initial load
captured_print("System initialized - logs active") # Seed initial log
def compute_rsi(df, period=14):
    return talib.RSI(df['close'], timeperiod=period)
def compute_custom_rsi_kde(df, rsi_period=14, pivot_len=14, bandwidth=2.71828, steps=100, threshold=0.25):
    df['rsi'] = compute_rsi(df, rsi_period)
    highs = argrelextrema(df['high'].values, np.greater, order=pivot_len)[0]
    lows = argrelextrema(df['low'].values, np.less, order=pivot_len)[0]
    high_rsi = df['rsi'].iloc[highs].dropna().values
    low_rsi = df['rsi'].iloc[lows].dropna().values
    if len(high_rsi) > 0:
        kde_high = gaussian_kde(high_rsi, bw_method=1/bandwidth)
        x_high = np.linspace(min(high_rsi) - 1, max(high_rsi) + 1, steps)
        y_high = kde_high(x_high)
        max_high_prob = y_high.max()
    else:
        max_high_prob = np.nan
    if len(low_rsi) > 0:
        kde_low = gaussian_kde(low_rsi, bw_method=1/bandwidth)
        x_low = np.linspace(min(low_rsi) - 1, max(low_rsi) + 1, steps)
        y_low = kde_low(x_low)
        max_low_prob = y_low.max()
    else:
        max_low_prob = np.nan
    df['high_prob'] = np.nan
    df['low_prob'] = np.nan
    for i in range(len(df)):
        rsi_val = df['rsi'].iloc[i]
        if not np.isnan(rsi_val):
            if len(high_rsi) > 0:
                nearest_high = np.argmin(np.abs(x_high - rsi_val))
                df.loc[df.index[i], 'high_prob'] = y_high[nearest_high] / max_high_prob if max_high_prob else 0
            if len(low_rsi) > 0:
                nearest_low = np.argmin(np.abs(x_low - rsi_val))
                df.loc[df.index[i], 'low_prob'] = y_low[nearest_low] / max_low_prob if max_low_prob else 0
    df['bullish_signal'] = (df['low_prob'] > (1 - threshold)) & (df['low_prob'] > df['high_prob'])
    df['bearish_signal'] = (df['high_prob'] > (1 - threshold)) & (df['high_prob'] > df['low_prob'])
    num_bullish = df['bullish_signal'].sum()
    num_bearish = df['bearish_signal'].sum()
    captured_print(f"Signal balance: Bullish={num_bullish}, Bearish={num_bearish}")
    return df
def process_live_data(df):
    df['time_idx'] = range(len(df))
    df['group'] = config_data['SYMBOL']
    df['chunk_id'] = 'live'
    df['ema_fast'] = talib.EMA(df['close'], timeperiod=12)
    df['ema_slow'] = talib.EMA(df['close'], timeperiod=26)
    df['macd'], df['macd_signal'], _ = talib.MACD(df['close'], fastperiod=12, slowperiod=26, signalperiod=9)
    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
    for lag in range(1, 6):
        df[f'close_lag{lag}'] = df['close'].shift(lag)
    df = compute_custom_rsi_kde(df)
    df['target_h1'] = 0.0
    initial_shape = df.shape
    df = df.dropna()
    captured_print(f"Data processed. Shape reduced from {initial_shape} to {df.shape}")
    return df
# Updated: Opposite direction check - skip if conflicting open position
def can_place_order(action):
    if not ensure_mt5_connection():
        return False
    positions = mt5.positions_get(symbol=config_data['SYMBOL'])
    if not positions:
        return True
    has_buy = any(pos.type == mt5.ORDER_TYPE_BUY for pos in positions)
    has_sell = any(pos.type == mt5.ORDER_TYPE_SELL for pos in positions)
    if action == "BUY" and has_sell:
        captured_print("Cannot place BUY: Open SELL position exists (opposite direction protection)")
        return False
    if action == "SELL" and has_buy:
        captured_print("Cannot place SELL: Open BUY position exists (opposite direction protection)")
        return False
    return True
def place_order(action, symbol, sl_pips, tp_pips):
    global high_weekly_profit, last_week
    if not can_place_order(action): # New check
        return {'success': False, 'reason': 'Opposite direction blocked'}
    if not ensure_mt5_connection():
        captured_print("MT5 not connected for order placement")
        return {'success': False}
    try:
        account_info = mt5.account_info()
        if not account_info:
            captured_print("Failed to get account info for order")
            return {'success': False}
        # NEW: Get USD rate and convert to USD
        usd_rate = get_usd_conversion_rate(account_info.currency)
        current_balance = account_info.balance
        current_balance_usd = current_balance * usd_rate
        # New: Detect new week and reset high
        now_utc = datetime.now(timezone.utc)
        current_week = now_utc.strftime('%Y-W%U')
        if current_week != last_week:
            high_weekly_profit = 0.0
            last_week = current_week
            captured_print(f"New week {current_week} - reset trail high")
        # Compute weekly_profit (filtered by symbol) in account currency, then USD
        utc_epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        date_to = now_utc + timedelta(days=1)
        date_to = int((date_to - utc_epoch).total_seconds())
        start_of_week_utc = now_utc - timedelta(days=now_utc.weekday())
        start_of_week_utc = start_of_week_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from_week = int((start_of_week_utc - utc_epoch).total_seconds())
        # Use datetime objects for fetch
        from_date_week = datetime.fromtimestamp(date_from_week, tz=timezone.utc)
        to_date = datetime.fromtimestamp(date_to, tz=timezone.utc)
        deals_week = mt5.history_deals_get(from_date_week, to_date)
        weekly_profit = 0.0
        if deals_week:
            weekly_profit = sum(deal.profit for deal in deals_week if deal.entry == mt5.DEAL_ENTRY_OUT and deal.symbol == symbol)
        weekly_profit_usd = weekly_profit * usd_rate
        # Update high water mark (USD)
        if weekly_profit_usd > high_weekly_profit:
            high_weekly_profit = weekly_profit_usd
            captured_print(f"Updated weekly high profit (USD): {high_weekly_profit:.2f}")
        # Derive starting_balance and profit_perc (weekly basis, USD)
        starting_balance_usd = current_balance_usd - weekly_profit_usd
        if starting_balance_usd <= 0:
            starting_balance_usd = current_balance_usd # Fallback for edge cases
        profit_perc = ((weekly_profit_usd / starting_balance_usd) * 100) if starting_balance_usd > 0 else 0.0
        captured_print(f"Weekly profit (USD): {weekly_profit_usd:.2f}, Starting balance (USD): {starting_balance_usd:.2f}, Profit %: {profit_perc:.2f}")
        # New: Trail logic - compute percentages
        high_pct = (high_weekly_profit / starting_balance_usd * 100) if starting_balance_usd > 0 else 0.0
        # Base multiplier based on profit_perc (symmetric for losses)
        abs_profit_perc = abs(profit_perc)
        if profit_perc > 0:
            if abs_profit_perc > config_data['PROFIT_QUARTER_PCT']:
                multiplier = 0.25
            elif abs_profit_perc > config_data['PROFIT_HALF_PCT']:
                multiplier = 0.5
            else:
                multiplier = 1.0
            captured_print(f"Positive profit: Multiplier {multiplier:.2f} (|perc| {abs_profit_perc:.2f})")
        elif profit_perc < 0:
            if abs_profit_perc > config_data['LOSS_QUARTER_PCT']:
                multiplier = 0.25
            elif abs_profit_perc > config_data['LOSS_HALF_PCT']:
                multiplier = 0.5
            else:
                multiplier = 1.0
            captured_print(f"Negative profit: Multiplier {multiplier:.2f} (|perc| {abs_profit_perc:.2f})")
        else:
            multiplier = 1.0
            captured_print("Neutral profit: Multiplier 1.00")
        # New: Apply additional trail halving if triggered
        if high_pct >= config_data['TRAIL_ACTIVATION_PCT']:
            drop_pct = high_pct - profit_perc
            if drop_pct >= config_data['TRAIL_PCT']:
                multiplier *= 0.5
                captured_print(f"Trail triggered: drop {drop_pct:.2f}% >= {config_data['TRAIL_PCT']:.2f}% - halving multiplier to {multiplier:.2f}")
        effective_base_lot = config_data['BASE_LOT_SIZE'] * multiplier
        captured_print(f"Effective base lot: {effective_base_lot:.2f}")
        # Compute lot_size with USD balance
        lot_size = round((current_balance_usd / config_data['BASE_BALANCE']) * effective_base_lot, 2)
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            captured_print("Symbol info unavailable - cannot place order")
            return {'success': False}
        min_lot = symbol_info.volume_min
        lot_size = max(lot_size, min_lot)
        point = symbol_info.point
        digits = symbol_info.digits
        if point == 0:
            captured_print("Point value is 0 - cannot compute offset reliably")
            return {'success': False, 'reason': 'Point value unavailable'}
        pip_size = point * (10 ** (digits - 2))
        captured_print(f"Detected point value: {point}, digits: {digits}, pip_size: {pip_size}")
        sl_offset = sl_pips * pip_size
        tp_offset = tp_pips * pip_size
        captured_print(f"Computed offsets: SL={sl_offset:.3f}, TP={tp_offset:.3f}")
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            captured_print("Failed to get tick info")
            return {'success': False}
        price = tick.ask if action == "BUY" else tick.bid
        sl = price - sl_offset if action == "BUY" else price + sl_offset
        tp = price + tp_offset if action == "BUY" else price - tp_offset
        captured_print(f"Final SL: {sl:.3f}, TP: {tp:.3f} for {action} at {price:.3f}")
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": 234000,
            "comment": "TFT Trade",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            captured_print(f"Order failed: {result.comment}")
            return {'success': False}
        else:
            captured_print(f"Order placed: {action} {lot_size} lots at {price}")
            return {'success': True, 'lot_size': lot_size, 'entry_price': price}
    except Exception as e:
        captured_print(f"Order exception: {e}")
        return {'success': False}
def parse_predict_output(tft, dataloader, df):
    raw_output = tft.predict(dataloader, mode='quantiles', return_index=True)
    predictions = None
    index = None
    if not isinstance(raw_output, tuple):
        predictions = raw_output
    else:
        for item in raw_output:
            if isinstance(item, torch.Tensor) or (isinstance(item, np.ndarray) and item.ndim >= 2):
                predictions = item
            elif isinstance(item, pd.DataFrame) and 'time_idx' in item.columns:
                index = item
        if predictions is None and len(raw_output) > 0:
            predictions = raw_output[0]
        if index is None and len(raw_output) > 1:
            if isinstance(raw_output[-1], pd.DataFrame):
                index = raw_output[-1]
    if predictions is None:
        raise ValueError("Could not identify predictions in output")
    if isinstance(predictions, torch.Tensor):
        predictions = predictions.cpu().numpy()
    if index is None:
        num_preds = predictions.shape[0] if hasattr(predictions, 'shape') else len(predictions)
        index = pd.DataFrame({'time_idx': df['time_idx'].tail(num_preds).values})
    return predictions, index
def get_account_info():
    global config_data
    config_data = load_config() # Reload config for latest changes
    if not ensure_mt5_connection():
        error = "MT5 connection failed. Please ensure terminal is open and logged in."
        return {"error": error, "balance": 0.0, "equity": 0.0, "leverage": 0, "monthly_profit": 0.0, "daily_profit": 0.0, "weekly_profit": 0.0, "margin": 0.0, "margin_free": 0.0, "account_currency": "USD"}
    error = None
    try:
        info = mt5.account_info()
        if not info:
            code, msg = mt5.last_error()
            if code == -10004:
                error = "MT5 terminal not running. Please start MetaTrader 5, log in, and ensure it's connected."
            else:
                error = f"Failed to retrieve account info. Code {code}: {msg}"
            info = type('obj', (object,), {'balance': 0.0, 'equity': 0.0, 'leverage': 0, 'margin': 0.0, 'margin_free': 0.0, 'currency': 'USD'})() # Fallback
        # NEW: Get USD rate
        usd_rate = get_usd_conversion_rate(info.currency)
        # Use UTC for period boundaries
        now_utc = datetime.now(timezone.utc)
        utc_epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        date_to = now_utc + timedelta(days=1)
        date_to = int((date_to - utc_epoch).total_seconds())
        # Use datetime for fetch
        to_date = datetime.fromtimestamp(date_to, tz=timezone.utc)
        # Monthly: start of current month UTC
        start_of_month_utc = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        date_from_monthly = int((start_of_month_utc - utc_epoch).total_seconds())
        from_date_monthly = datetime.fromtimestamp(date_from_monthly, tz=timezone.utc)
        deals_monthly = mt5.history_deals_get(from_date_monthly, to_date)
        monthly_profit = 0.0
        if deals_monthly:
            monthly_profit = sum(deal.profit for deal in deals_monthly if deal.entry == mt5.DEAL_ENTRY_OUT)
        monthly_profit_usd = monthly_profit * usd_rate
        # Weekly: start of current week UTC (Monday)
        start_of_week_utc = now_utc - timedelta(days=now_utc.weekday())
        start_of_week_utc = start_of_week_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from_weekly = int((start_of_week_utc - utc_epoch).total_seconds())
        from_date_weekly = datetime.fromtimestamp(date_from_weekly, tz=timezone.utc)
        deals_weekly = mt5.history_deals_get(from_date_weekly, to_date)
        weekly_profit = 0.0
        if deals_weekly:
            weekly_profit = sum(deal.profit for deal in deals_weekly if deal.entry == mt5.DEAL_ENTRY_OUT)
        weekly_profit_usd = weekly_profit * usd_rate
        # Daily: start of current day UTC
        start_of_day_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from_daily = int((start_of_day_utc - utc_epoch).total_seconds())
        from_date_daily = datetime.fromtimestamp(date_from_daily, tz=timezone.utc)
        deals_daily = mt5.history_deals_get(from_date_daily, to_date)
        daily_profit = 0.0
        if deals_daily:
            daily_profit = sum(deal.profit for deal in deals_daily if deal.entry == mt5.DEAL_ENTRY_OUT)
        daily_profit_usd = daily_profit * usd_rate
        # Override weekly_profit only if current week has trades; else 0
        closed_data = get_closed_trades()
        groups = closed_data.get('groups', {})
        # Compute current week display name (in UTC, but name is month/day)
        end_of_week_utc = start_of_week_utc + timedelta(days=6)
        start_str = start_of_week_utc.strftime('%B %d')
        end_str = end_of_week_utc.strftime('%B %d')
        if start_of_week_utc.month == end_of_week_utc.month:
            current_week_name = f"{start_str} to {end_str}"
        else:
            current_week_name = f"{start_str} to {end_of_week_utc.strftime('%B %d')}"
        if current_week_name in groups:
            weekly_profit_usd = groups[current_week_name]['summary']['net_profit']
        else:
            weekly_profit_usd = 0.0
        result = {
            "error": error,
            "balance": round(info.balance * usd_rate, 2),
            "equity": round(info.equity * usd_rate, 2),
            "leverage": info.leverage,
            "monthly_profit": round(monthly_profit_usd, 2),
            "weekly_profit": round(weekly_profit_usd, 2),
            "daily_profit": round(daily_profit_usd, 2),
            "margin": round(info.margin * usd_rate, 2),
            "margin_free": round(info.margin_free * usd_rate, 2),
            "account_currency": info.currency  # NEW: Include for display
        }
    except Exception as e:
        result = {"error": f"Exception in account info: {str(e)}", "balance": 0.0, "equity": 0.0, "leverage": 0, "monthly_profit": 0.0, "weekly_profit": 0.0, "daily_profit": 0.0, "margin": 0.0, "margin_free": 0.0, "account_currency": "USD"}
    return result
def get_open_trades():
    global config_data
    config_data = load_config()
    if not ensure_mt5_connection():
        return {'error': "MT5 connection failed. Please ensure terminal is open.", 'trades': [], 'total_profit': 0.0, 'account_currency': "USD"}
    error = None
    try:
        account_info = mt5.account_info()
        usd_rate = get_usd_conversion_rate(account_info.currency)
        positions = mt5.positions_get(symbol=config_data['SYMBOL'])
        if positions is None:
            code, msg = mt5.last_error()
            if code == -10004:
                error = "MT5 terminal not running. Please start MetaTrader 5, log in, and ensure it's connected."
            else:
                error = f"Failed to get positions. Code {code}: {msg}"
            positions = []
        trades = []
        total_profit = 0.0
        for pos in positions:
            trade = {
                'ticket': pos.ticket,
                'type': 'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL',
                'volume': pos.volume,
                'entry_price': pos.price_open,
                'current_price': pos.price_current,
                'profit': pos.profit * usd_rate,  # USD
                'time': pos.time
            }
            trades.append(trade)
            total_profit += pos.profit * usd_rate
        result = {'error': error, 'trades': trades, 'total_profit': round(total_profit, 2), 'account_currency': account_info.currency}
    except Exception as e:
        result = {'error': f"Exception in open trades: {str(e)}", 'trades': [], 'total_profit': 0.0, 'account_currency': "USD"}
    return result
# Updated: History fix - original_type = opposite of closing deal.type; counts use original_type
# NEW: Sharpe and Profit Factor overall
def get_closed_trades(page=1):
    global config_data
    config_data = load_config()
    if not ensure_mt5_connection():
        return {'error': "MT5 connection failed after retries. Please ensure terminal is open.", 'groups': {}, 'summary': {'total_trades': 0, 'win_rate': 0, 'total_profit': 0.0, 'sharpe_ratio': 0.0, 'profit_factor': 0.0}, 'account_currency': "USD"}
    error = None
    try:
        account_info = mt5.account_info()
        usd_rate = get_usd_conversion_rate(account_info.currency)
        # Fetch history from 5 years back
        now_utc = datetime.now(timezone.utc)
        utc_epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        start_of_five_years = now_utc - timedelta(days=5 * 365 + 1) # Approx 5 years, +1 for leap
        start_of_five_years = start_of_five_years.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int((start_of_five_years - utc_epoch).total_seconds())
        end_timestamp = int((now_utc + timedelta(days=1) - utc_epoch).total_seconds())
        # Use datetime objects for fetch
        from_date = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
        to_date = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)
        deals = None
        for attempt in range(2): # Retry fetch once
            deals = mt5.history_deals_get(from_date, to_date)
            if deals is not None:
                break
            code, msg = mt5.last_error()
            if code == -10004:
                # Re-init on drop
                ensure_mt5_connection()
            time.sleep(1) # Brief pause
        if deals is None:
            code, msg = mt5.last_error()
            if code == -10004:
                error = "MT5 terminal not running. Please start MetaTrader 5, log in, and ensure it's connected."
            else:
                error = f"Failed to get history after retries. Code {code}: {msg}"
            deals = []
        else:
            # Filter deals for the configured symbol
            deals = [deal for deal in deals if deal.symbol == config_data['SYMBOL']]
        closed_groups = {}
        profits_all = []
        total_deals = 0
        profitable_deals = 0
        total_profit = 0.0
        min_deal_time = None
        max_deal_time = None
        for deal in deals:
            if deal.entry == mt5.DEAL_ENTRY_OUT:
                total_deals += 1
                profit = deal.profit
                profits_all.append(profit)
                if profit > 0:
                    profitable_deals += 1
                total_profit += profit * usd_rate  # USD
                # Use raw server unix -> UTC datetime for grouping
                deal_time = deal.time
                trade_time = datetime.utcfromtimestamp(deal_time) # UTC naive
                # Start of week (Monday) in UTC
                start_of_week = trade_time - timedelta(days=trade_time.weekday())
                week_key = start_of_week.strftime('%Y-%m-%d')
                end_of_week = start_of_week + timedelta(days=6)
                # Fixed: original_type = opposite of closing deal.type
                original_type = 'BUY' if deal.type == mt5.DEAL_TYPE_SELL else 'SELL'
                trade = {
                    'ticket': deal.ticket,
                    'original_type': original_type, # New: Correct original direction
                    'volume': deal.volume,
                    'profit': profit * usd_rate,  # USD
                    'time': deal.time
                }
                if week_key not in closed_groups:
                    start_key = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
                    end_key = end_of_week.replace(hour=23, minute=59, second=59, microsecond=999999)
                    closed_groups[week_key] = {'trades': [], 'total_profit': 0.0, 'start': start_key, 'end': end_key}
                closed_groups[week_key]['trades'].append(trade)
                closed_groups[week_key]['total_profit'] += profit * usd_rate  # USD
                closed_groups[week_key]['total_profit'] = round(closed_groups[week_key]['total_profit'], 2)
                if min_deal_time is None or deal_time < min_deal_time:
                    min_deal_time = deal_time
                if max_deal_time is None or deal_time > max_deal_time:
                    max_deal_time = deal_time
        # Generate all weeks from earliest deal to current, filling zeros (in UTC)
        current_date = now_utc.date()
        current_week_start = now_utc - timedelta(days=now_utc.weekday())
        current_week_start = current_week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if min_deal_time is None:
            min_date = start_of_five_years.date()
            min_week_start = start_of_five_years - timedelta(days=start_of_five_years.weekday())
            min_week_start = min_week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            min_date = datetime.fromtimestamp(min_deal_time, tz=timezone.utc).date()
            min_week_start = datetime(min_date.year, min_date.month, min_date.day, tzinfo=timezone.utc) - timedelta(days=datetime(min_date.year, min_date.month, min_date.day).weekday())
            min_week_start = min_week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        current = min_week_start
        while current <= current_week_start:
            week_key = current.strftime('%Y-%m-%d')
            if week_key not in closed_groups:
                end_w = current + timedelta(days=6)
                closed_groups[week_key] = {'trades': [], 'total_profit': 0.0, 'start': current, 'end': end_w}
            current += timedelta(weeks=1)
        win_rate = (profitable_deals / total_deals * 100) if total_deals > 0 else 0
        # NEW: Sharpe and Profit Factor overall (USD-scaled)
        profits_all_usd = [p * usd_rate for p in profits_all]
        profits_pos = [p for p in profits_all_usd if p > 0]
        profits_neg = [p for p in profits_all_usd if p < 0]
        gross_profit = sum(profits_pos)
        gross_loss = abs(sum(profits_neg))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)
        mean_profit = np.mean(profits_all_usd) if profits_all_usd else 0.0
        std_profit = np.std(profits_all_usd) if len(profits_all_usd) > 1 else 0.0
        sharpe_ratio = mean_profit / std_profit if std_profit > 0 else 0.0
        summary = {
            'total_trades': total_deals,
            'win_rate': round(win_rate, 1),
            'total_profit': round(total_profit, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'Inf'
        }
        # Compute weekly groups (sorted descending: latest week first)
        groups = {}
        week_list = sorted(closed_groups.items(), key=lambda x: datetime.strptime(x[0], '%Y-%m-%d'), reverse=True)
        # Page: 4 weeks per page
        weeks_per_page = 4
        start_idx = (page - 1) * weeks_per_page
        end_idx = start_idx + weeks_per_page
        paged_week_list = week_list[start_idx:end_idx]
        total_pages = math.ceil(len(week_list) / weeks_per_page)
        for week_key, g in paged_week_list:
            trades = g['trades']
            total_trades = len(trades)
            # Fixed: Count using original_type
            buys = sum(1 for t in trades if t['original_type'] == 'BUY')
            sells = total_trades - buys
            profitable = sum(1 for t in trades if t['profit'] > 0)
            losses_num = total_trades - profitable
            week_win_rate = (profitable / total_trades * 100) if total_trades else 0
            total_profits = sum(t['profit'] for t in trades if t['profit'] > 0)
            total_losses = sum(t['profit'] for t in trades if t['profit'] < 0)
            net_profit = g['total_profit']
            # NEW: Sharpe and Profit Factor per week (USD)
            profits_week = [t['profit'] for t in trades]
            profits_pos_week = [p for p in profits_week if p > 0]
            profits_neg_week = [p for p in profits_week if p < 0]
            gross_profit_week = sum(profits_pos_week)
            gross_loss_week = abs(sum(profits_neg_week))
            profit_factor_week = gross_profit_week / gross_loss_week if gross_loss_week > 0 else (float('inf') if gross_profit_week > 0 else 0.0)
            mean_profit_week = np.mean(profits_week) if profits_week else 0.0
            std_profit_week = np.std(profits_week) if len(profits_week) > 1 else 0.0
            sharpe_week = mean_profit_week / std_profit_week if std_profit_week > 0 else 0.0
            start = g['start']
            end = g['end']
            start_str = start.strftime('%B %d')
            end_str = end.strftime('%B %d')
            if start.month == end.month:
                display_name = f"{start_str} to {end_str}"
            else:
                display_name = f"{start_str} to {end.strftime('%B %d')}"
            groups[display_name] = {
                'summary': {
                    'total_trades': total_trades,
                    'buys': buys,
                    'sells': sells,
                    'win_rate': round(week_win_rate, 1),
                    'profitable': profitable,
                    'losses': losses_num,
                    'total_profits': round(total_profits, 2),
                    'total_losses': round(total_losses, 2),
                    'net_profit': round(net_profit, 2),
                    'sharpe_ratio': round(sharpe_week, 2),
                    'profit_factor': round(profit_factor_week, 2) if profit_factor_week != float('inf') else 'Inf'
                },
                'trades': trades # Includes original_type, profits in USD
            }
        result = {'error': error, 'groups': groups, 'summary': summary, 'total_pages': total_pages, 'current_page': page, 'account_currency': account_info.currency}
    except Exception as e:
        result = {'error': f"Exception in closed trades: {str(e)}", 'groups': {}, 'summary': {'total_trades': 0, 'win_rate': 0, 'total_profit': 0.0, 'sharpe_ratio': 0.0, 'profit_factor': 0.0}, 'total_pages': 0, 'current_page': page, 'account_currency': "USD"}
    return result
# Function to update config and save to JSON
def update_config(new_config):
    global config_data
    config_data.update(new_config)
    save_config()
    captured_print("Config updated successfully")
def trading_loop(stop_event):
    global is_running, config_data, mt5_connected
    captured_print("Starting live trading...")
    config_data = load_config() # Reload config
    # Ensure connection at start (no shutdown)
    if not ensure_mt5_connection():
        captured_print("MT5 connection failed in trading loop—aborting")
        return
    min_bars_needed = config_data['MAX_ENCODER_LENGTH'] + 50
    symbol_info = mt5.symbol_info(config_data['SYMBOL'])
    if symbol_info is None:
        captured_print("Symbol info unavailable")
        return
    timeframe = mt5.TIMEFRAME_M1 if config_data['TIMEFRAME'] == "M1" else mt5.TIMEFRAME_M5
    # Initial fetch with retry/re-init
    initial_rates = None
    for attempt in range(2): # Retry once
        if not ensure_mt5_connection(): # Ensure before fetch
            continue
        initial_rates = mt5.copy_rates_from_pos(config_data['SYMBOL'], timeframe, 0, config_data['BARS_TO_FETCH'])
        if initial_rates is None:
            code, msg = mt5.last_error()
            captured_print(f"Initial rates fetch failed (attempt {attempt+1}): code {code}, msg {msg}")
            if code == -10004:
                ensure_mt5_connection() # Re-init on drop
            time.sleep(1)
        else:
            break
    if initial_rates is None or len(initial_rates) < min_bars_needed:
        captured_print(f"Insufficient initial data after retries: {len(initial_rates) if initial_rates else 0} bars < {min_bars_needed}")
        return
    # Correct times to UTC using Pandas
    df_init = pd.DataFrame(initial_rates)
    df_init['time'] = df_init['time']
    df_init['timestamp'] = pd.to_datetime(df_init['time'], unit='s', utc=True)
    df_init = df_init.set_index('timestamp')
    df_init = df_init.rename(columns={'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'tick_volume': 'volume'})
    df_init = df_init.drop(columns=['spread', 'real_volume'])
    df_init = process_live_data(df_init)
    obj = torch.load("dataset_params.pt", map_location="cpu", weights_only=False)
    init_dataset = TimeSeriesDataSet.from_parameters(obj["params"], df_init, categorical_encoders=obj["encoders"])
    tft = TemporalFusionTransformer.from_dataset(
        init_dataset,
        hidden_size=64,
        lstm_layers=3,
        dropout=0.1,
        attention_head_size=4,
        output_size=7,
        loss=QuantileLoss(),
    )
    tft.load_state_dict(torch.load('cpu_model.pth', map_location=torch.device('cpu')))
    tft.eval()
    captured_print("Model loaded and ready")
    flip_consecutive = 0
    pause_flips = False
    loop_count = 0
    # Use UTC date for day change detection
    now_utc = datetime.now(timezone.utc)
    last_date = now_utc.date()
    health_check_counter = 0 # New: Periodic health check every 10 loops
    while is_running and not stop_event.is_set(): # Double-check at top
        loop_count += 1
        captured_print(f"Trading loop iteration {loop_count}")
        if stop_event.is_set() or not is_running:
            break
        # Check trading hours
        if not is_within_hours():
            captured_print(f"Outside GMT trading hours ({config_data['START_TIME_GMT']} - {config_data['END_TIME_GMT']}) - skipping cycle")
            time.sleep(60) # Check every minute
            continue
        # Check for new day and reload predictions history if needed
        now_utc = datetime.now(timezone.utc)
        current_date = now_utc.date()
        if current_date > last_date:
            load_all_predictions()
            last_date = current_date
            captured_print(f"New day {current_date} detected - reloaded full predictions history")
        config_data = load_config() # Reload config each loop for changes
        try:
            if not ensure_mt5_connection():
                captured_print("MT5 not connected—skipping loop")
                time.sleep(5) # Wait before retry
                continue
            # Periodic health check
            health_check_counter += 1
            if health_check_counter % 10 == 0:
                if mt5.account_info() is None:
                    captured_print("Health check failed—reconnecting MT5")
                    ensure_mt5_connection()
            # Loop fetch with retry/re-init
            rates = None
            for attempt in range(2): # Retry once
                rates = mt5.copy_rates_from_pos(config_data['SYMBOL'], timeframe, 0, config_data['BARS_TO_FETCH'])
                if rates is None:
                    code, msg = mt5.last_error()
                    captured_print(f"Rates fetch failed in loop {loop_count} (attempt {attempt+1}): code {code}, msg {msg}")
                    if code == -10004:
                        ensure_mt5_connection() # Re-init on drop
                    time.sleep(1)
                else:
                    break
            if rates is None or len(rates) < min_bars_needed:
                captured_print(f"Insufficient data fetched after retries in loop {loop_count}: {len(rates) if rates else 0} bars < {min_bars_needed} - Skipping save")
            else:
                captured_print(f"Fetched {len(rates)} bars - Processing...")
                # Correct times to UTC using Pandas
                df = pd.DataFrame(rates)
                df['time'] = df['time']
                df['timestamp'] = pd.to_datetime(df['time'], unit='s', utc=True)
                df = df.set_index('timestamp')
                df = df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'tick_volume': 'volume'})
                df = df.drop(columns=['spread', 'real_volume'])
                df = process_live_data(df)
                def get_dataset(df):
                    return TimeSeriesDataSet.from_parameters(
                        obj["params"],
                        df,
                        categorical_encoders=obj["encoders"],
                        predict=True
                    )
                dataset = get_dataset(df)
                dataloader = dataset.to_dataloader(train=False, batch_size=1, num_workers=0)
                predictions, index = parse_predict_output(tft, dataloader, df)
                last_pred = predictions[-1]
                median_return = round(last_pred[:, 3].mean().item(), 7)
                captured_print(f"Last bar median predicted return: {median_return:.7f}")
                last_row = df.iloc[-1]
                captured_print(f"Last bar signals: Bullish={last_row['bullish_signal']}, Bearish={last_row['bearish_signal']}")
                positions = mt5.positions_get(symbol=config_data['SYMBOL'])
                open_trades_count = len(positions) if positions else 0
                captured_print(f"Current open trades: {open_trades_count}")
                # Compute ROC if enough history (use full window from logs + current)
                roc = None
                if len(prediction_logs) >= (config_data['ROC_WINDOW'] - 1):
                    recent_medians = [p['median_return'] for p in prediction_logs[-(config_data['ROC_WINDOW'] - 1):]] + [median_return]
                    if len(recent_medians) >= config_data['ROC_WINDOW']:
                        window = np.array(recent_medians[-config_data['ROC_WINDOW'] :])
                        # Split: First 3 (past), last 3 (recent) — non-overlapping for pure change
                        past_group = window[:config_data['ROC_WINDOW'] - 1] # e.g., [0,1,2]
                        recent_group = window[1:] # e.g., [1,2,3] — slight overlap natural, but EWMA handles
                        # EWMA for recency (alpha=0.4: ~60% weight on newest → aggressive)
                        alpha = 0.4
                        ewma_past = pd.Series(past_group).ewm(alpha=alpha).mean().iloc[-1]
                        ewma_recent = pd.Series(recent_group).ewm(alpha=alpha).mean().iloc[-1]
                        delta = ewma_recent - ewma_past
                        sigma = np.std(window)
                        if sigma > 1e-6: # Dynamic vol-norm
                            roc = (delta / sigma) * config_data['NORMAL_ROC_SENSITIVITY'] # Use normal sens for scaling (legacy compat)
                        else: # Rare flat case
                            roc = delta * config_data['NORMAL_ROC_SENSITIVITY'] / 1e-6 # Scale like %
                        captured_print(f"ROC (EWMA-vol): {roc:.2f} (delta={delta:.7f}, sigma={sigma:.7f})")
                    else:
                        captured_print("ROC window too short - static only")
                else:
                    captured_print("Insufficient history for ROC - static only")
                # New: Multiple ranges
                normal_ranges = [
                    (config_data['NORMAL_LOW_1'], config_data['NORMAL_HIGH_1']),
                    (config_data['NORMAL_LOW_2'], config_data['NORMAL_HIGH_2']),
                ]
                flip_ranges = [
                    (config_data['FLIP_LOW_1'], config_data['FLIP_HIGH_1']),
                    (config_data['FLIP_LOW_2'], config_data['FLIP_HIGH_2']),
                ]
                abs_median = abs(median_return)
                abs_roc = abs(roc) if roc is not None else 0.0
                normal_range = any(low <= abs_median <= high for low, high in normal_ranges if low < high)  # Skip invalid
                flip_range = any(low <= abs_median <= high for low, high in flip_ranges if low < high)
                action = None
                flipped = False
                static_action = None
                is_flip_range_trade = False # New: For precise counting
                if open_trades_count >= config_data['MAX_OPEN_TRADES']:
                    captured_print(f"Max open trades ({config_data['MAX_OPEN_TRADES']}) reached. Skipping new trade.")
                else:
                    # Static checks first (ranges)
                    if normal_range:
                        # Normal: gate by ROC (separate sensitivity)
                        if abs_roc <= config_data['NORMAL_ROC_SENSITIVITY']:
                            static_action = "BUY" if median_return > 0 else "SELL"
                            captured_print(f"Normal range trade: {static_action} (|median| {abs_median:.7f} in normal ranges, |ROC| {abs_roc:.2f} <= {config_data['NORMAL_ROC_SENSITIVITY']:.2f})")
                        else:
                            # Flip and place (treat as flip for direction, but not for count)
                            static_action = "SELL" if median_return > 0 else "BUY"
                            flipped = True
                            captured_print(f"Normal range flip trade: {static_action} (|median| {abs_median:.7f} in normal ranges, high ROC {abs_roc:.2f} > {config_data['NORMAL_ROC_SENSITIVITY']:.2f})")
                    elif flip_range and not pause_flips:
                        # Flip: no ROC gate
                        static_action = "SELL" if median_return > 0 else "BUY"
                        flipped = True
                        is_flip_range_trade = True # New: For counting
                        captured_print(f"Flip range trade: {static_action} (|median| {abs_median:.7f} in flip ranges, not paused)")
                    else:
                        if flip_range and pause_flips:
                            captured_print(f"Flip range skipped: Pause active (consecutive: {flip_consecutive})")
                        captured_print(f"No static trade: |median| {abs_median:.7f} not in ranges")
                    # ROC independent trigger if no static (separate sensitivity)
                    if static_action is None and roc is not None and abs_roc > config_data['ROC_TRADE_SENSITIVITY']:
                        # UPDATED: Flip the independent ROC trade direction (opposite of ROC sign)
                        # and treat as flipped for counters/logging, but not for flip-range count.
                        action = "SELL" if roc > 0 else "BUY"
                        flipped = True # NEW: Set to True so it increments flip_consecutive and respects pause logic
                        captured_print(f"ROC-driven flipped trade: {action} (|ROC| {abs_roc:.2f} > {config_data['ROC_TRADE_SENSITIVITY']:.2f})")
                    else:
                        action = static_action
                executed = False
                lot_size = 0.0
                entry_price = 0.0
                if action:
                    order_result = place_order(action, config_data['SYMBOL'], config_data['STOP_LOSS_PIPS'], config_data['TAKE_PROFIT_PIPS'])
                    if order_result['success']:
                        executed = True
                        lot_size = order_result['lot_size']
                        entry_price = order_result['entry_price']
                        # Update counters based on execution type (new logic: only flip-range increments)
                        if is_flip_range_trade:
                            flip_consecutive += 1
                            if flip_consecutive >= config_data['FLIP_MAX_CONSECUTIVE']:
                                pause_flips = True
                                captured_print(f"Flip streak reached {config_data['FLIP_MAX_CONSECUTIVE']}; pausing flips")
                        else:
                            # Normal or ROC: reset
                            pause_flips = False
                            flip_consecutive = 0
                            captured_print("Normal/ROC trade: Reset flip pause/counter")
                    else:
                        captured_print(f"No trade executed due to order failure: {order_result.get('reason', 'Unknown')}")
                else:
                    captured_print("No trade executed")
                # Log the structured trade info for the dashboard
                # Use UTC with Z for consistency and to fix JS parsing shift
                timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                trade_info = {
                    'timestamp': timestamp,
                    'median_return': median_return,
                    'bullish_signal': bool(last_row['bullish_signal']),
                    'bearish_signal': bool(last_row['bearish_signal']),
                    'open_trades': open_trades_count,
                    'action': action or 'None', # Ensure string
                    'flipped': flipped,
                    'executed': executed,
                    'lot_size': lot_size,
                    'entry_price': entry_price,
                    'paused': pause_flips,
                    'flip_consecutive': flip_consecutive,
                    'pause_flips': pause_flips,
                    'roc': roc if roc is not None else 0.0 # New
                }
                captured_print("About to save prediction...")
                save_prediction(trade_info)
                with lock:
                    prediction_logs.append(trade_info)
                    if len(prediction_logs) > 1000:
                        prediction_logs.pop(0)
                captured_print(f"Prediction generated: median_return={median_return:.7f}, action={trade_info['action']}, executed={executed}, roc={trade_info['roc']:.2f}, flip_consec={flip_consecutive}, pause_flips={pause_flips}")
            if not is_running or stop_event.is_set(): # Check after main logic
                break
        except Exception as e:
            captured_print(f"Trading loop exception: {e}")
            if not is_running or stop_event.is_set():
                break
        # Interruptible wait for next cycle
        if is_running:
            stopped = stop_event.wait(config_data['LOOP_INTERVAL_SECONDS'])
            if stopped or not is_running:
                break
    captured_print("Trading loop stopped")
def start_trading():
    global is_running, stop_event, trading_thread
    if not is_running:
        is_running = True
        stop_event = threading.Event() # Create stop event
        trading_thread = threading.Thread(target=trading_loop, args=(stop_event,))
        trading_thread.start() # No daemon - for join control
        captured_print("Trading started")
def stop_trading():
    global is_running, stop_event, trading_thread
    is_running = False
    if stop_event:
        stop_event.set() # Signal immediate exit from wait/sleep
    captured_print("Stopping trading...")
    if trading_thread and trading_thread.is_alive():
        trading_thread.join(timeout=2.0) # Graceful wait
        if trading_thread.is_alive():
            captured_print("Warning: Trading thread still active after join - restart app if needed")
    # No global shutdown here—keep for dashboard polls until app exit