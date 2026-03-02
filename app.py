# app.py (FIXED: Resolved zero values and flickering issues)
from flask import Flask, render_template, redirect, url_for, jsonify, request, flash, g, get_flashed_messages, session, make_response
from flask_compress import Compress
from functools import wraps
from werkzeug.utils import secure_filename
import tra # Import your modified tra.py
import MetaTrader5 as mt5 # Import for validation
from collections import defaultdict
import time
import os
from datetime import time as dt_time
import atexit # New: For clean shutdown on app exit
from flask import send_from_directory
import hashlib # For ETag
import json # For json.dumps in ETag calculation
import threading # NEW: For monitor thread
import numpy as np # Added for marketing dashboard
from datetime import datetime, timedelta # Added/updated for marketing
import scipy.stats as stats # Added for marketing
app = Flask(__name__)
app.secret_key = 'your_secret_key' # Change to a secure key in production
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False # Set to True in production with HTTPS
Compress(app) # Auto-gzip HTML/JS/CSS for 70-80% size reduction & speed
ADMIN_CODE = "adminhuggingtrade" # Fixed admin secret code
# Load/save user config (single user)
def load_user_config():
    try:
        with open('user_config.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
def save_user_config(config):
    with open('user_config.json', 'w') as f:
        json.dump(config, f, indent=4)
# Updated: In-memory rate limiter (per IP, 60 reqs/min on /live for real-time)
@app.before_request
def rate_limit():
    if request.endpoint in ['live', 'history', 'logs', 'predictions']:
        ip = request.remote_addr
        if not hasattr(g, 'requests'):
            g.requests = defaultdict(list)
        recent = [t for t in g.requests[ip] if time.time() - t < 60]
        if len(recent) > 60: # Increased for polling (1s intervals)
            return jsonify({'error': 'Rate limited—try again in 1min'}), 429
        g.requests[ip].append(time.time())
# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            if request.endpoint in ['live', 'history', 'logs', 'predictions', 'config']:
                return jsonify({'error': 'Not authenticated. Please log in.'}), 401
            flash('Please log in to access the dashboard.', 'error')
            return redirect(url_for('login'))
        login_time = session.get('login_time', 0)
        if time.time() - login_time > 3600: # 1 hour
            session.pop('authenticated', None)
            session.pop('login_time', None)
            session.pop('user_type', None)
            if request.endpoint in ['live', 'history', 'logs', 'predictions', 'config']:
                return jsonify({'error': 'Session expired. Please log in again.', 'traceback': ''}), 401
            flash('Session expired. Please log in again.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function
# New: Admin required decorator (for updates)
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_type') != 'admin':
            flash('Admin access required for this action.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function
# Serve logo.jpg as favicon from root dir
@app.route('/logo.jpg')
def logo():
    if os.path.exists('logo.jpg'):
        return send_from_directory('.', 'logo.jpg', mimetype='image/jpeg')
    return '', 404
@app.route('/')
def index():
    return redirect(url_for('login'))
@app.route('/login', methods=['GET'])
def login():
    flashed_messages = [{'message': msg[1], 'category': msg[0]} for msg in get_flashed_messages(with_categories=True)]
    return render_template('login.html', flashed_messages=flashed_messages)
# New: User login route
@app.route('/user_login', methods=['POST'])
def user_login():
    tra.captured_print(f"Received POST to /user_login from IP: {request.remote_addr}")
    username = request.form.get('username')
    password = request.form.get('password')
    tra.captured_print(f"User login attempt for username: {username}")
    user_config = load_user_config()
    if user_config.get('username') == username and user_config.get('password') == password:
        session['authenticated'] = True
        session['login_time'] = time.time()
        session['user_type'] = 'user'
        app.logger.info(f"Session created for user: {username}")
        tra.captured_print(f"User login successful for username: {username}")
        flash('Logged in successfully as user.', 'success')
        return redirect(url_for('dashboard'))
    else:
        tra.captured_print(f"User login failed for username: {username}")
        flash('Invalid username or password.', 'error')
        return redirect(url_for('login'))
# New: Create account route (single user)
@app.route('/create_account', methods=['POST'])
def create_account():
    tra.captured_print(f"Received POST to /create_account from IP: {request.remote_addr}")
    tra.captured_print("Create account attempt")
    user_config = load_user_config()
    if user_config: # Already exists
        tra.captured_print("Create account failed: Account already exists")
        flash('Account already exists. Only one user allowed.', 'error')
        return redirect(url_for('login'))
    username = request.form.get('username')
    password = request.form.get('password')
    confirm = request.form.get('confirm_password')
    if not username or not password:
        tra.captured_print("Create account failed: Missing username or password")
        flash('Username and password required.', 'error')
        return redirect(url_for('login'))
    if password != confirm:
        tra.captured_print("Create account failed: Passwords do not match")
        flash('Passwords do not match.', 'error')
        return redirect(url_for('login'))
    save_user_config({'username': username, 'password': password})
    tra.captured_print(f"Create account successful for username: {username}")
    flash('Account created successfully. You can now log in.', 'success')
    return redirect(url_for('login'))
# New: Forget password route (simple reset)
@app.route('/forget_password', methods=['POST'])
def forget_password():
    tra.captured_print(f"Received POST to /forget_password from IP: {request.remote_addr}")
    tra.captured_print("Forget password attempt")
    user_config = load_user_config()
    if not user_config:
        tra.captured_print("Forget password failed: No account exists")
        flash('No account exists to reset.', 'error')
        return redirect(url_for('login'))
    username = request.form.get('username')
    new_password = request.form.get('new_password')
    confirm = request.form.get('confirm_new')
    if username != user_config.get('username'):
        tra.captured_print(f"Forget password failed: Invalid username {username}")
        flash('Invalid username.', 'error')
        return redirect(url_for('login'))
    if new_password != confirm:
        tra.captured_print("Forget password failed: Passwords do not match")
        flash('Passwords do not match.', 'error')
        return redirect(url_for('login'))
    user_config['password'] = new_password
    save_user_config(user_config)
    tra.captured_print(f"Forget password successful for username: {username}")
    flash('Password reset successfully. Log in with new password.', 'success')
    return redirect(url_for('login'))
# New: Admin login route
@app.route('/admin_login', methods=['POST'])
def admin_login():
    tra.captured_print(f"Received POST to /admin_login from IP: {request.remote_addr}")
    code = request.form.get('code')
    tra.captured_print("Admin login attempt")
    if code == ADMIN_CODE:
        session['authenticated'] = True
        session['login_time'] = time.time()
        session['user_type'] = 'admin'
        app.logger.info("Session created for admin")
        tra.captured_print("Admin login successful")
        flash('Admin access granted.', 'success')
        return redirect(url_for('dashboard'))
    else:
        tra.captured_print("Admin login failed")
        flash('Invalid admin code.', 'error')
        return redirect(url_for('login'))
@app.route('/logout')
@login_required
def logout():
    session.pop('authenticated', None)
    session.pop('login_time', None)
    session.pop('user_type', None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))
# No caching—fetch fresh data each time for real-time updates
def get_account_info():
    return tra.get_account_info()
def get_open_trades():
    return tra.get_open_trades()
def get_closed_trades(page=1):
    return tra.get_closed_trades(page=page)
@app.route('/dashboard')
@login_required
def dashboard():
    # Initial data fetch (for any server-side render needs, though JS overrides)
    account = get_account_info()
    current_config = tra.load_config()
    status = "Running" if tra.is_running else "Stopped"
    open_trades = get_open_trades()
    closed_trades = get_closed_trades()
    # Pass flashes to template for JS display
    flashed_messages = [{'message': msg[1], 'category': msg[0]} for msg in get_flashed_messages(with_categories=True)]
    template = 'dashboard.html' if session.get('user_type') == 'admin' else 'dashboarduser.html'
    return render_template(template, status=status, account=account, config=current_config,
                           open_trades=open_trades, closed_trades=closed_trades, flashed_messages=flashed_messages)
@app.route('/start')
@login_required
@admin_required
def start():
    within = tra.is_within_hours()
    if within:
        if not tra.is_running:
            tra.start_trading()
            flash("Trading started successfully!", "success")
        else:
            flash("Trading already running.", "warning")
    else:
        if not tra.is_running:
            tra.start_trading()
        flash("Outside trading hours unless you change the time range for trading.", "warning")
    return redirect(url_for('dashboard'))
@app.route('/stop')
@login_required
@admin_required
def stop():
    if tra.is_running:
        open_trades = tra.get_open_trades()
        tra.stop_trading()
        if not open_trades.get('error') and len(open_trades.get('trades', [])) > 0:
            flash("Trading stopped. Open positions remain active for safety—manually close if needed.", "warning")
        else:
            flash("Trading stopped successfully!", "success")
    else:
        flash("Trading already stopped.", "warning")
    return redirect(url_for('dashboard'))
@app.route('/update_credentials', methods=['POST'])
@login_required
@admin_required
def update_credentials():
    current_config = tra.load_config()
    new_config = current_config.copy()
    errors = []
    # Log received form data (sanitized for security) - uses tra.captured_print for dashboard logs
    received = {k: v[:4] + '...' if k == 'password' else v for k, v in request.form.items() if v.strip()}
    tra.captured_print(f"Received update request: {received}")
    # Mapping: form_key -> (config_key, parser, default)
    fields = {
        'symbol': ('SYMBOL', str, current_config.get('SYMBOL', 'XAUUSDm')),
        'account': ('MT5_ACCOUNT', int, current_config.get('MT5_ACCOUNT', 0)),
        'password': ('MT5_PASSWORD', str, current_config.get('MT5_PASSWORD', '')),
        'server': ('MT5_SERVER', str, current_config.get('MT5_SERVER', ''))
    }
    updated_count = 0
    for form_key, (config_key, parser, default) in fields.items():
        val = request.form.get(form_key)
        if val and val.strip(): # Only update if provided and non-blank
            try:
                new_config[config_key] = parser(val)
                updated_count += 1
            except ValueError:
                errors.append(f"Invalid value for {config_key}: {val}")
    if errors:
        tra.captured_print(f"Update errors: {'; '.join(errors)}")
        flash("; ".join(errors) + ". Unchanged fields kept as-is.", "error")
    # Temporarily stop trading if running to avoid connection conflicts during update
    was_running = tra.is_running
    if was_running:
        tra.stop_trading()
        tra.captured_print("Temporarily stopped trading for credentials update")
    # Shutdown current connection for testing
    mt5.shutdown()
    success = False
    if updated_count > 0:
        # Test new credentials
        test_account = new_config['MT5_ACCOUNT']
        test_password = new_config['MT5_PASSWORD']
        test_server = new_config['MT5_SERVER']
        tra.captured_print(f"Testing MT5 init: Account={test_account}, Server={test_server} (password masked)")
        success = mt5.initialize(login=test_account, server=test_server, password=test_password)
        if success:
            account_info = mt5.account_info()
            if account_info and account_info.server != test_server:
                mt5.shutdown()
                tra.captured_print(f"Server mismatch: Input '{test_server}', Connected to '{account_info.server}'")
                flash(f"Server name mismatch. Connected to '{account_info.server}' but input '{test_server}'. Please use the exact server name '{account_info.server}'.", "error")
                # Restart trading if was running
                tra.ensure_mt5_connection()
                if was_running:
                    tra.start_trading()
                    tra.captured_print("Trading restarted after server mismatch")
                return redirect(url_for('dashboard'))
            # Quick balance check for 0-balance log
            info = mt5.account_info()
            balance = info.balance if info else 0.0
            mt5.shutdown() # Close test connection
            if balance == 0:
                tra.captured_print("Test successful, but account balance is 0. Trading may skip orders until funded.")
            else:
                tra.captured_print("Test successful - credentials valid.")
            tra.update_config(new_config)
            # Re-init main connection post-update
            tra.ensure_mt5_connection()
            if was_running:
                tra.start_trading()
                flash(f"Updated {updated_count} credentials successfully! Trading restarted with new account.", "success")
                tra.captured_print("Trading restarted after successful credentials update")
            else:
                flash(f"Updated {updated_count} credentials successfully! Connection validated.", "success")
            # Refresh marketing cache immediately after successful update
            fetch_marketing_data_safe()
        else:
            code, msg = mt5.last_error()
            tra.captured_print(f"Test failed: Code {code}, Msg: {msg}. Config unchanged.")
            # Re-init main connection after failed test (with old config)
            tra.ensure_mt5_connection()
            # Restart trading if it was running
            if was_running:
                tra.start_trading()
                tra.captured_print("Trading restarted after failed credentials update (old config)")
            flash(f"Invalid credentials. Connection failed. Code {code}: {msg}. Unchanged fields kept as-is.", "error")
    else:
        tra.captured_print("No changes detected in update request.")
        flash("No changes detected.", "warning")
        # Ensure connection and restart if needed
        tra.ensure_mt5_connection()
        if was_running:
            tra.start_trading()
    return redirect(url_for('dashboard'))
@app.route('/update_settings', methods=['POST'])
@login_required
@admin_required
def update_settings():
    current_config = tra.load_config()
    new_config = current_config.copy()
    errors = []
    # Mapping: form_key -> (config_key, parser, default) (updated for new ROC sensitivities)
    fields = {
        'base_balance': ('BASE_BALANCE', float, current_config.get('BASE_BALANCE', 1000)),
        'base_lot_size': ('BASE_LOT_SIZE', float, current_config.get('BASE_LOT_SIZE', 0.1)),
        'max_open_trades': ('MAX_OPEN_TRADES', int, current_config.get('MAX_OPEN_TRADES', 5)),
        'stop_loss_pips': ('STOP_LOSS_PIPS', int, current_config.get('STOP_LOSS_PIPS', 300)),
        'take_profit_pips': ('TAKE_PROFIT_PIPS', int, current_config.get('TAKE_PROFIT_PIPS', 600)),
        'start_time_gmt': ('START_TIME_GMT', str, current_config.get('START_TIME_GMT', '07:00')), # New: str
        'end_time_gmt': ('END_TIME_GMT', str, current_config.get('END_TIME_GMT', '17:00')), # New: str
        'profit_half_pct': ('PROFIT_HALF_PCT', float, current_config.get('PROFIT_HALF_PCT', 100.0)), # New
        'profit_quarter_pct': ('PROFIT_QUARTER_PCT', float, current_config.get('PROFIT_QUARTER_PCT', 200.0)), # New
        'loss_half_pct': ('LOSS_HALF_PCT', float, current_config.get('LOSS_HALF_PCT', 5.0)), # New
        'loss_quarter_pct': ('LOSS_QUARTER_PCT', float, current_config.get('LOSS_QUARTER_PCT', 15.0)), # New
        'trail_activation_pct': ('TRAIL_ACTIVATION_PCT', float, current_config.get('TRAIL_ACTIVATION_PCT', 50.0)), # FIXED: Added missing field
        'trail_pct': ('TRAIL_PCT', float, current_config.get('TRAIL_PCT', 20.0)), # FIXED: Added missing field
        'timeframe': ('TIMEFRAME', str, current_config.get('TIMEFRAME', 'M1')), # str, no parse error
        'bars_to_fetch': ('BARS_TO_FETCH', int, current_config.get('BARS_TO_FETCH', 10000)),
        'normal_low_threshold': ('NORMAL_LOW_1', float, current_config.get('NORMAL_LOW_1', 0.000010)), # Renamed to _1
        'normal_high_threshold': ('NORMAL_HIGH_1', float, current_config.get('NORMAL_HIGH_1', 0.000014)), # Renamed to _1
        'flip_low_threshold': ('FLIP_LOW_1', float, current_config.get('FLIP_LOW_1', 0.000017)), # Renamed to _1
        'flip_high_threshold': ('FLIP_HIGH_1', float, current_config.get('FLIP_HIGH_1', 0.000022)), # Renamed to _1
        'normal_low_2': ('NORMAL_LOW_2', float, current_config.get('NORMAL_LOW_2', 0.000023)), # New: 2nd normal
        'normal_high_2': ('NORMAL_HIGH_2', float, current_config.get('NORMAL_HIGH_2', 0.000030)), # New: 2nd normal
        'flip_low_2': ('FLIP_LOW_2', float, current_config.get('FLIP_LOW_2', 0.000032)), # New: 2nd flip
        'flip_high_2': ('FLIP_HIGH_2', float, current_config.get('FLIP_HIGH_2', 0.000040)), # New: 2nd flip
        'flip_max_consecutive': ('FLIP_MAX_CONSECUTIVE', int, current_config.get('FLIP_MAX_CONSECUTIVE', 10)),
        'loop_interval_seconds': ('LOOP_INTERVAL_SECONDS', int, current_config.get('LOOP_INTERVAL_SECONDS', 60)),
        'max_encoder_length': ('MAX_ENCODER_LENGTH', int, current_config.get('MAX_ENCODER_LENGTH', 30)),
        'max_prediction_length': ('MAX_PREDICTION_LENGTH', int, current_config.get('MAX_PREDICTION_LENGTH', 10)),
        'roc_window': ('ROC_WINDOW', int, current_config.get('ROC_WINDOW', 4)), # New
        'normal_roc_sensitivity': ('NORMAL_ROC_SENSITIVITY', float, current_config.get('NORMAL_ROC_SENSITIVITY', 1.0)), # New
        'roc_trade_sensitivity': ('ROC_TRADE_SENSITIVITY', float, current_config.get('ROC_TRADE_SENSITIVITY', 1.0)) # New
    }
    updated_count = 0
    for form_key, (config_key, parser, default) in fields.items():
        val = request.form.get(form_key)
        if val and val.strip(): # Only update if provided and non-blank
            try:
                new_config[config_key] = parser(val)
                updated_count += 1
            except ValueError:
                errors.append(f"Invalid value for {config_key}: {val}")
    if errors:
        flash("; ".join(errors) + ". Unchanged fields kept as-is.", "error")
    if updated_count > 0:
        tra.update_config(new_config)
        flash(f"Updated {updated_count} settings successfully! Unchanged fields kept as-is.", "success")
    else:
        flash("No changes detected.", "warning")
    return redirect(url_for('dashboard'))
@app.route('/live')
@login_required
def live():
    try:
        data = {
            'running': tra.is_running,
            'within_hours': tra.is_within_hours(), # New
            'account': get_account_info(),
            'open_trades': get_open_trades()
        }
        etag = hashlib.md5(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()
        if request.headers.get('If-None-Match') == etag:
            return make_response('', 304)
        response = jsonify(data)
        response.set_etag(etag)
        return response
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(error_msg) # Log to server console
        return jsonify({'error': str(e), 'traceback': error_msg}), 500
@app.route('/history')
@login_required
def history():
    try:
        page = request.args.get('page', 1, type=int)
        data = get_closed_trades(page=page)
        etag = hashlib.md5(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()
        if request.headers.get('If-None-Match') == etag:
            return make_response('', 304)
        response = jsonify(data)
        response.set_etag(etag)
        return response
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(error_msg) # Log to server console
        return jsonify({'error': str(e), 'traceback': error_msg}), 500
@app.route('/logs')
@login_required
def logs():
    try:
        with tra.lock:
            data = {'system_logs': tra.system_logs[-200:]}
        etag = hashlib.md5(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()
        if request.headers.get('If-None-Match') == etag:
            return make_response('', 304)
        response = jsonify(data)
        response.set_etag(etag)
        return response
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(error_msg) # Log to server console
        return jsonify({'error': str(e), 'traceback': error_msg}), 500
@app.route('/predictions')
@login_required
def predictions():
    try:
        with tra.lock:
            data = {'prediction_logs': tra.prediction_logs[-200:]}
        etag = hashlib.md5(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()
        if request.headers.get('If-None-Match') == etag:
            return make_response('', 304)
        response = jsonify(data)
        response.set_etag(etag)
        return response
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(error_msg) # Log to server console
        return jsonify({'error': str(e), 'traceback': error_msg}), 500
@app.route('/config')
@login_required
def config():
    try:
        data = tra.load_config()
        etag = hashlib.md5(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()
        if request.headers.get('If-None-Match') == etag:
            return make_response('', 304)
        response = jsonify(data)
        response.set_etag(etag)
        return response
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(error_msg) # Log to server console
        return jsonify({'error': str(e), 'traceback': error_msg}), 500
@app.route('/diagnostics', methods=['POST'])
def diagnostics():
    error = request.json.get('error')
    tra.captured_print(f"Client error: {error}")
    return '', 204
# Cache static assets (JS/CSS) for 1hr
@app.after_request
def add_cache_control(response):
    if request.path.startswith('/static/') or 'text/css' in response.content_type or 'application/javascript' in response.content_type:
        response.headers['Cache-Control'] = 'public, max-age=3600' # 1hr cache
    return response
def init_mt5():
    tra.load_config() # Ensure config loaded
    if not tra.ensure_mt5_connection():
        print("Warning: MT5 init failed on startup - dashboard data may show errors/zeros until fixed.")
        return False
    print("MT5 initialized and connected successfully at startup.")
    return True
# New: Clean shutdown on app exit
def shutdown_mt5():
    mt5.shutdown()
    print("MT5 connection closed on app shutdown.")
atexit.register(shutdown_mt5)
# NEW: Monitor thread for auto-start on entering trading hours
def hours_monitor():
    prev_within = False
    while True:
        current_within = tra.is_within_hours() # Reloads config dynamically
        if not prev_within and current_within and not tra.is_running:
            tra.start_trading()
            tra.captured_print("Auto-started trading on entering configured hours")
        prev_within = current_within
        time.sleep(10) # Check every 10 seconds for quick response to time changes
# ---------------------------
# Marketing Dashboard Integration (FIXED)
# ---------------------------
# JINJA FILTERS
@app.template_filter('number_format')
def number_format(value, decimals=2):
    try:
        return f"{float(value):,.{decimals}f}"
    except (ValueError, TypeError):
        return value
@app.template_filter('datetime_format')
def datetime_format(timestamp, fmt='%Y-%m-%d %H:%M:%S'):
    try:
        return datetime.fromtimestamp(timestamp).strftime(fmt)
    except (ValueError, TypeError):
        return timestamp
# GLOBALS for marketing
cached_marketing_data = None
marketing_last_update = None
current_account_id = None # NEW: Track current account for change detection
MYFXBOOK_LINK = "https://www.myfxbook.com/members/yourusername/youraccount/12345678"
# FIXED DATA FETCH - Handles edge cases properly
def fetch_marketing_data():
    global cached_marketing_data, marketing_last_update, current_account_id
   
    # Ensure MT5 connection before any calls
    if not tra.ensure_mt5_connection():
        print("[ERROR] Failed to ensure MT5 connection in fetch_marketing_data")
        return {
            'error': 'Failed to Connect to MT5',
            'error_details': 'Unable to establish or verify MT5 connection.',
            'troubleshooting': [
                '1. Check MT5 terminal is running and logged in.',
                '2. Verify credentials in config.',
                '3. Check internet connection.'
            ]
        }
   
    data = {
        'metrics': {},
        'equity_data': [],
        'drawdown_data': [],
        'monthly_returns': [],
        'day_of_week_pnl': [],
        'hour_of_day_pnl': [],
        'session_pnl': [],
        'profit_hist': {'labels': [], 'counts': []},
        'duration_hist': {'labels': [], 'counts': []},
        'symbol_pie': {'labels': [], 'values': []},
        'symbol_stats': [],
        'recent_trades': [],
        'open_positions': [],
        'trade_type_stats': {'labels': [], 'values': []},
        'daily_returns': [],
        'weekly_stats': [],
        'performance_summary': {}
    }
   
    # ----- Check MT5 Connection Status -----
    if not mt5.terminal_info():
        error_code, error_msg = mt5.last_error()
        print(f"[ERROR] MT5 terminal not connected. Error code: {error_code}, Message: {error_msg}")
        return {
            'error': f'MT5 Terminal Not Connected',
            'error_details': f'Error Code: {error_code} - {error_msg}',
            'troubleshooting': [
                '1. Check if MetaTrader 5 terminal is running',
                '2. Verify you are logged into your MT5 account',
                '3. Check your MT5 credentials in the dashboard settings',
                '4. Try restarting the Flask application',
                '5. Try restarting MetaTrader 5 terminal'
            ]
        }
   
    # ----- Account Info -----
    acc = mt5.account_info()
    if not acc:
        error_code, error_msg = mt5.last_error()
        terminal_info = mt5.terminal_info()
        print(f"[ERROR] Unable to fetch account info. Error code: {error_code}, Message: {error_msg}")
        print(f"[DEBUG] Terminal connected: {terminal_info is not None}")
        if terminal_info:
            print(f"[DEBUG] Terminal company: {terminal_info.company}")
            print(f"[DEBUG] Terminal name: {terminal_info.name}")
            print(f"[DEBUG] Terminal path: {terminal_info.path}")
        return {
            'error': 'Unable to Fetch Account Information',
            'error_details': f'MT5 Error Code: {error_code} - {error_msg}',
            'connection_status': 'Terminal connected' if terminal_info else 'Terminal disconnected',
            'troubleshooting': [
                '1. Verify you are logged into your MT5 account (not just investor password)',
                '2. Check if your MT5 account credentials are correct',
                '3. Go to Dashboard → Update Credentials and verify account, password, and server',
                '4. Make sure you have an active internet connection',
                '5. Check if your broker server is online',
                '6. Try logging out and back into MT5 terminal',
                f'7. MT5 Last Error Code: {error_code}'
            ]
        }
   
    # NEW: Get USD conversion rate (from tra.py)
    usd_rate = tra.get_usd_conversion_rate(acc.currency)
    tra.captured_print(f"Account currency: {acc.currency}, USD rate: {usd_rate:.4f}")
   
    # NEW: Detect account changes
    if current_account_id is not None and current_account_id != acc.login:
        print(f"Account change detected: {current_account_id} -> {acc.login}")
        current_account_id = acc.login
        # Force cache refresh
        cached_marketing_data = None
    else:
        current_account_id = acc.login
   
    # ----- Open Positions -----
    positions = mt5.positions_get() or []
    open_profit = sum(p.profit for p in positions) * usd_rate  # Convert to USD
    open_trades_count = len(positions)
   
    data['open_positions'] = [
        {
            'symbol': p.symbol,
            'type': 'Buy' if p.type == 0 else 'Sell',
            'volume': p.volume,
            'open_price': p.price_open,
            'current_price': p.price_current,
            'profit': p.profit * usd_rate,  # USD
            'swap': p.swap * usd_rate,  # USD
            'comment': p.comment
        } for p in positions
    ]
   
    # ----- All Historical Deals (CLOSED TRADES ONLY) -----
    from_date = datetime.now() - timedelta(days=365) # Last 1 year
    to_date = datetime.now()
   
    try:
        deals = mt5.history_deals_get(from_date, to_date)
        if deals is None:
            error_code, error_msg = mt5.last_error()
            print(f"[ERROR] Failed to fetch historical deals. Error code: {error_code}, Message: {error_msg}")
            return {
                'error': 'Failed to Fetch Trading History',
                'error_details': f'MT5 Error Code: {error_code} - {error_msg}',
                'troubleshooting': [
                    '1. Check if you have trading history in your MT5 account',
                    '2. Verify your account has been active for at least a few days',
                    '3. Try reducing the history period (currently set to 1 year)',
                    '4. Check if your broker allows API access to history',
                    '5. Try refreshing your trading history in MT5 terminal',
                    f'6. MT5 Error Code: {error_code}'
                ]
            }
        deals = list(deals) if deals else []
        print(f"[DEBUG] Fetched {len(deals)} historical deals from the last year")
    except Exception as e:
        print(f"[ERROR] Exception while fetching deals: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {
            'error': 'Exception While Fetching Trading History',
            'error_details': str(e),
            'troubleshooting': [
                '1. Check the console/terminal for detailed error logs',
                '2. Verify MT5 connection is stable',
                '3. Try restarting the Flask application',
                '4. Check if you have sufficient trading history'
            ]
        }
   
    if not deals:
        print("[WARNING] No historical deals found in the account")
        # FIXED: Return valid data structure with zeros instead of error for new accounts
        data['metrics'] = {
            'balance': acc.balance * usd_rate,  # USD
            'equity': acc.equity * usd_rate,  # USD
            'margin': acc.margin * usd_rate,  # USD
            'margin_free': acc.margin_free * usd_rate,  # USD
            'margin_level': round(acc.margin_level, 2) if acc.margin > 0 else 0,
            'open_profit': open_profit,
            'open_trades_count': open_trades_count,
            'total_deposits': 0,
            'total_withdrawals': 0,
            'net_deposits': 0,
            'total_profit': 0,
            'win_rate': 0,
            'total_trades': 0,
            'wins': 0,
            'losses': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'sharpe_ratio': 0,
            'sortino_ratio': 0,
            'max_drawdown': 0,
            'max_dd_duration': 0,
            'rr_ratio': 0,
            'expectancy': 0,
            'recovery_factor': 0,
            'ulcer_index': 0,
            'avg_duration': 0,
            'best_trade': 0,
            'worst_trade': 0,
            'max_win_streak': 0,
            'max_loss_streak': 0,
            'total_pips': 0,
            'total_commissions': 0,
            'total_swaps': 0,
            'avg_volume': 0,
            'trades_per_month': 0,
            'trades_per_week': 0,
            'roi': 0,
            'skewness': 0,
            'kurtosis': 0,
            'var_95': 0
        }
        data['performance_summary'] = {
            'today_pnl': 0,
            'today_pct': 0,
            'this_week_pnl': 0,
            'this_week_pct': 0,
            'this_month_pnl': 0,
            'this_month_pct': 0,
            'avg_daily_pnl': 0,
            'avg_daily_pct': 0
        }
        data['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data['account_id'] = acc.login
        data['account_currency'] = acc.currency  # NEW: Include for display
        cached_marketing_data = data
        marketing_last_update = datetime.now()
        print("[SUCCESS] Marketing data initialized for new account (no trades yet)")
        return data
   
    # Sort deals by time
    deals = sorted(deals, key=lambda d: d.time)
   
    # ----- Deposits and Withdrawals -----
    balance_deals = [d for d in deals if d.type == mt5.DEAL_TYPE_BALANCE]
    total_deposits = sum(d.profit for d in balance_deals if d.profit > 0) * usd_rate  # USD
    total_withdrawals = abs(sum(d.profit for d in balance_deals if d.profit < 0)) * usd_rate  # USD
    net_deposits = total_deposits - total_withdrawals
   
    # ----- Total Commissions and Swaps -----
    total_commissions = abs(sum(d.commission for d in deals)) * usd_rate  # USD
    total_swaps = sum(d.swap for d in deals) * usd_rate  # USD
   
    # ----- Process Trades (CLOSED TRADES ONLY) -----
    pos_dict = defaultdict(dict)
    profits = []
    pips_list = []
    durations = [] # in seconds
    daily_trade_pnl = defaultdict(float)
    monthly_trade_pnl = defaultdict(float)
    weekly_trade_pnl = defaultdict(lambda: {'pnl': 0, 'trades': 0})
    day_of_week_pnl = defaultdict(float)
    hour_of_day_pnl = defaultdict(float)
    session_pnl = defaultdict(float)
    trade_type_counts = {'Buy': 0, 'Sell': 0}
    min_time = float('inf')
    max_time = 0
   
    for d in deals:
        if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            pid = d.position_id
            if d.entry == mt5.DEAL_ENTRY_IN:
                pos_dict[pid]['open_time'] = d.time
                pos_dict[pid]['open_price'] = d.price
                pos_dict[pid]['type'] = 'Buy' if d.type == mt5.DEAL_TYPE_BUY else 'Sell'
                pos_dict[pid]['volume'] = d.volume
                pos_dict[pid]['symbol'] = d.symbol
                pos_dict[pid]['commission'] = d.commission
                pos_dict[pid]['swap'] = d.swap
            elif d.entry == mt5.DEAL_ENTRY_OUT:
                # ONLY process CLOSED trades
                pos_dict[pid]['close_time'] = d.time
                pos_dict[pid]['close_price'] = d.price
                pos_dict[pid]['profit'] = d.profit
                pos_dict[pid]['commission'] += d.commission
                pos_dict[pid]['swap'] += d.swap
               
                day_dt = datetime.fromtimestamp(d.time)
                day_date = day_dt.date()
                month = day_dt.strftime('%Y-%m')
                week = day_dt.strftime('%Y-W%U')
               
                daily_trade_pnl[day_date] += d.profit
                monthly_trade_pnl[month] += d.profit
                weekly_trade_pnl[week]['pnl'] += d.profit
                weekly_trade_pnl[week]['trades'] += 1
               
                profits.append(d.profit)
                min_time = min(min_time, pos_dict[pid]['open_time'])
                max_time = max(max_time, d.time)
   
    # Complete trades
    complete_trades = [pos for pos in pos_dict.values() if 'close_time' in pos]
   
    # Calculate durations, pips, time-based pnl, avg volume
    total_pips = 0
    total_volume = 0
   
    for t in complete_trades:
        duration_seconds = t['close_time'] - t['open_time']
        durations.append(duration_seconds)
        t['duration'] = duration_seconds
       
        # Calculate pips
        symbol_info = mt5.symbol_info(t['symbol'])
        if symbol_info:
            point = symbol_info.point
            if t['type'] == 'Buy':
                pips = (t['close_price'] - t['open_price']) / point
            else:
                pips = (t['open_price'] - t['close_price']) / point
            # Adjust for gold if necessary
            if 'XAU' in t['symbol'].upper() or 'GOLD' in t['symbol'].upper():
                pips /= 10
            t['pips'] = round(pips, 1)
            total_pips += pips
            pips_list.append(pips)
        else:
            t['pips'] = 0
       
        total_volume += t['volume']
        trade_type_counts[t['type']] += 1
       
        # Time-based
        open_dt = datetime.fromtimestamp(t['open_time'])
        day_of_week = open_dt.strftime('%A')
        hour = open_dt.hour
       
        day_of_week_pnl[day_of_week] += t['profit']
        hour_of_day_pnl[hour] += t['profit']
       
        # Sessions (UTC assumed)
        open_time_obj = open_dt.time()
        if dt_time(0,0) <= open_time_obj < dt_time(8,0): # Asia
            session = 'Asia'
        elif dt_time(8,0) <= open_time_obj < dt_time(16,0): # London
            session = 'London'
        elif dt_time(13,0) <= open_time_obj < dt_time(21,0): # New York
            session = 'New York'
        else:
            session = 'Overlap'
        session_pnl[session] += t['profit']
   
    avg_volume = total_volume / len(complete_trades) if complete_trades else 0
   
    # Period calculations
    days = (max_time - min_time) / 86400 if max_time > min_time else 0
    months = days / 30 if days > 0 else 0
    weeks = days / 7 if days > 0 else 0
   
    trades_per_month = len(complete_trades) / months if months > 0 else 0
    trades_per_week = len(complete_trades) / weeks if weeks > 0 else 0
   
    # Symbol stats
    symbol_stats = defaultdict(lambda: {'count': 0, 'pnl': 0, 'wins': 0, 'pips': 0, 'volume': 0})
    for t in complete_trades:
        s = t['symbol']
        symbol_stats[s]['count'] += 1
        symbol_stats[s]['pnl'] += t['profit'] * usd_rate  # USD
        symbol_stats[s]['pips'] += t.get('pips', 0)
        symbol_stats[s]['volume'] += t['volume']
        if t['profit'] > 0:
            symbol_stats[s]['wins'] += 1
   
    data['symbol_stats'] = sorted([
        {
            'symbol': s,
            'count': stats['count'],
            'pnl': round(stats['pnl'], 2),
            'win_rate': round(stats['wins'] / stats['count'] * 100, 1) if stats['count'] else 0,
            'pips': round(stats['pips'], 1),
            'avg_volume': round(stats['volume'] / stats['count'], 2) if stats['count'] else 0
        }
        for s, stats in symbol_stats.items()
    ], key=lambda x: x['pnl'], reverse=True)
   
    # Symbol pie for trade count
    data['symbol_pie'] = {
        'labels': [stat['symbol'] for stat in data['symbol_stats'][:6]],
        'values': [stat['count'] for stat in data['symbol_stats'][:6]]
    }
   
    # Trade type stats
    data['trade_type_stats'] = {
        'labels': list(trade_type_counts.keys()),
        'values': list(trade_type_counts.values())
    }
   
    # Recent trades
    recent = sorted(complete_trades, key=lambda t: t['close_time'], reverse=True)[:30]
    data['recent_trades'] = [
        {
            'symbol': t['symbol'],
            'type': t['type'],
            'open_time': t['open_time'],
            'close_time': t['close_time'],
            'profit': round(t['profit'] * usd_rate, 2),  # USD
            'duration': t['duration'],
            'pips': t.get('pips', 0),
            'volume': t['volume']
        }
        for t in recent
    ]
   
    # ----- Metrics -----
    total_trades = len(profits)
    wins = len([p for p in profits if p > 0])
    losses = total_trades - wins
    total_profit = sum(profits) * usd_rate  # USD
   
    win_rate = (wins / total_trades * 100) if total_trades else 0
   
    gross_profit = sum(p for p in profits if p > 0) * usd_rate  # USD
    gross_loss = abs(sum(p for p in profits if p < 0)) * usd_rate  # USD
    avg_win = gross_profit / wins if wins else 0
    avg_loss = gross_loss / losses if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss != 0 else 0
    rr_ratio = avg_win / avg_loss if avg_loss != 0 else 0
    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)
   
    mean_profit = np.mean(profits) * usd_rate if profits else 0  # USD
    std_profit = np.std(profits) * usd_rate if profits else 0  # USD (approx, since std scales)
    sharpe = (mean_profit / std_profit) * np.sqrt(252) if std_profit != 0 else 0 # Annualized
   
    negative_profits = [p * usd_rate for p in profits if p < 0]  # USD
    downside_std = np.std(negative_profits) if negative_profits else 0
    sortino = (mean_profit / downside_std) * np.sqrt(252) if downside_std != 0 else 0 # Annualized
   
    best_trade = max(profits) * usd_rate if profits else 0  # USD
    worst_trade = min(profits) * usd_rate if profits else 0  # USD
    avg_duration = np.mean(durations) if durations else 0
   
    # Streaks
    max_win_streak = 0
    max_loss_streak = 0
    current_win = 0
    current_loss = 0
    for p in profits:
        if p > 0:
            current_win += 1
            current_loss = 0
            max_win_streak = max(max_win_streak, current_win)
        elif p < 0:
            current_loss += 1
            current_win = 0
            max_loss_streak = max(max_loss_streak, current_loss)
        else:
            current_win = 0
            current_loss = 0
   
    # ----- Equity Curve -----
    daily_changes = defaultdict(float)
    for d in deals:
        day_date = datetime.fromtimestamp(d.time).date()
        daily_changes[day_date] += d.profit + d.commission + d.swap
   
    all_days = sorted(daily_changes.keys())
   
    first_deposit_date = None
    if balance_deals:
        first_deposit = min((d for d in balance_deals if d.profit > 0), key=lambda d: d.time, default=None)
        if first_deposit:
            first_deposit_date = datetime.fromtimestamp(first_deposit.time).date()
   
    if not first_deposit_date and all_days:
        first_deposit_date = all_days[0]
   
    if first_deposit_date:
        sorted_days = [d for d in all_days if d >= first_deposit_date]
    else:
        sorted_days = []
   
    cum_pnl = 0.0
    equity_data = []
    daily_returns = []
   
    for day in sorted_days:
        prev_equity = cum_pnl
        cum_pnl += daily_changes[day] * usd_rate  # USD
        equity_data.append({'date': day.isoformat(), 'equity': round(cum_pnl, 2)})
       
        if prev_equity > 0:
            daily_return = (daily_trade_pnl[day] * usd_rate / prev_equity) * 100  # USD-based
            daily_returns.append({'date': day.isoformat(), 'return': round(daily_return, 2)})
   
    data['equity_data'] = equity_data
    data['daily_returns'] = daily_returns
   
    # ----- Drawdown -----
    peak = 0.0
    max_dd = 0
    max_dd_amount = 0
    max_dd_duration = 0
    current_dd_start = None
    drawdown_data = []
    dd_values = []
   
    for i, e in enumerate(equity_data):
        v = e['equity']
        if v > peak:
            if current_dd_start is not None:
                current_dd_start = None
            peak = v
        else:
            if current_dd_start is None:
                current_dd_start = i
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            dd_values.append(dd)
            if dd > max_dd:
                max_dd = dd
                max_dd_amount = peak - v
                if current_dd_start is not None:
                    start_date = datetime.fromisoformat(equity_data[current_dd_start]['date'])
                    current_date = datetime.fromisoformat(e['date'])
                    max_dd_duration = (current_date - start_date).days
            drawdown_data.append({'date': e['date'], 'drawdown': round(-dd, 2)})
   
    data['drawdown_data'] = drawdown_data
   
    recovery_factor = total_profit / max_dd_amount if max_dd_amount != 0 else 0
   
    # Ulcer Index
    ulcer = np.sqrt(np.mean([d**2 for d in dd_values])) if dd_values else 0
   
    # ----- ROI (FIXED) -----
    if net_deposits > 0:
        roi = ((acc.equity * usd_rate / net_deposits) - 1) * 100  # USD-based
    else:
        roi = 0
   
    # Skewness and Kurtosis
    daily_pnl = [daily_trade_pnl[day] * usd_rate for day in sorted(daily_trade_pnl.keys())]  # USD
    skewness = stats.skew(daily_pnl) if len(daily_pnl) > 1 else 0
    kurtosis = stats.kurtosis(daily_pnl) if len(daily_pnl) > 1 else 0
   
    # Value at Risk (95%)
    if profits:
        var_95 = np.percentile(sorted([p * usd_rate for p in profits]), 5)  # USD
    else:
        var_95 = 0
   
    # ----- Monthly Returns with Percentages (FIXED) -----
    sorted_months = sorted(monthly_trade_pnl.keys())
    monthly_returns = []
   
    for month in sorted_months:
        pnl = monthly_trade_pnl[month] * usd_rate  # USD
        month_start_date = datetime.strptime(month + '-01', '%Y-%m-%d').date()
       
        # Calculate previous equity (net deposits + all profits before this month)
        prev_equity = net_deposits
        for day in sorted(daily_trade_pnl.keys()):
            if day < month_start_date:
                prev_equity += daily_trade_pnl[day] * usd_rate  # USD
            else:
                break
       
        # FIXED: Handle zero/negative equity properly
        if prev_equity > 0:
            month_return = (pnl / prev_equity * 100)
        elif pnl != 0:
            # If starting from zero but made profit/loss, show as percentage based on absolute PnL
            month_return = pnl * 100 # Simplified: show large percentage for first trades
        else:
            month_return = 0
       
        monthly_returns.append({
            'month': month,
            'pnl': round(pnl, 2),
            'return': round(month_return, 2)
        })
   
    data['monthly_returns'] = monthly_returns
   
    # ----- Weekly Stats with Percentages (FIXED) -----
    sorted_weeks = sorted(weekly_trade_pnl.keys(), key=lambda x: datetime.strptime(x.split('-W')[0] + '-W' + x.split('-W')[1] + '-1', '%Y-W%U-%w'), reverse=True)[:12]
    weekly_stats = []
   
    for week in sorted_weeks:
        pnl = weekly_trade_pnl[week]['pnl'] * usd_rate  # USD
        trades = weekly_trade_pnl[week]['trades']
        year, week_num_str = week.split('-W')
        week_num = int(week_num_str)
       
        try:
            first_day = datetime.strptime(f'{year} {week_num} 1', '%Y %U %w').date()
        except ValueError:
            first_day = datetime(int(year), 1, 1 + (week_num - 1) * 7).date() # Approximate
       
        prev_equity = net_deposits + sum(daily_trade_pnl[d] * usd_rate for d in sorted(daily_trade_pnl.keys()) if d < first_day)  # USD
       
        # FIXED: Handle zero/negative equity properly
        if prev_equity > 0:
            week_return = (pnl / prev_equity * 100)
        elif pnl != 0:
            week_return = pnl * 100 # Simplified
        else:
            week_return = 0
       
        weekly_stats.append({
            'week': week,
            'pnl': round(pnl, 2),
            'trades': trades,
            'return': round(week_return, 2)
        })
   
    data['weekly_stats'] = weekly_stats
   
    # ----- Time-Based Data -----
    days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    data['day_of_week_pnl'] = [
        {'day': day, 'pnl': round(day_of_week_pnl.get(day, 0) * usd_rate, 2)}  # USD
        for day in days_order
    ]
   
    data['hour_of_day_pnl'] = [
        {'hour': f"{h:02d}:00", 'pnl': round(hour_of_day_pnl.get(h, 0) * usd_rate, 2)}  # USD
        for h in range(24)
    ]
   
    sessions_order = ['Asia', 'London', 'New York', 'Overlap']
    data['session_pnl'] = [
        {'session': s, 'pnl': round(session_pnl.get(s, 0) * usd_rate, 2)}  # USD
        for s in sessions_order
    ]
   
    # ----- Histograms -----
    if profits:
        profits_usd = [p * usd_rate for p in profits]  # USD
        hist, edges = np.histogram(profits_usd, bins=15)
        data['profit_hist']['labels'] = [f"${edges[i]:.0f}" for i in range(len(edges)-1)]
        data['profit_hist']['counts'] = hist.tolist()
   
    if durations:
        dur_hist, dur_edges = np.histogram(durations, bins=12)
        data['duration_hist']['labels'] = [f"{dur_edges[i]:.0f}s" for i in range(len(dur_edges)-1)]
        data['duration_hist']['counts'] = dur_hist.tolist()
   
    # ----- Performance Summary (FIXED) -----
    today_date = datetime.now().date()
    today_pnl = daily_trade_pnl.get(today_date, 0) * usd_rate  # USD
   
    # Calculate previous equity for today
    prev_equity_today = net_deposits + sum(daily_trade_pnl[d] * usd_rate for d in sorted(daily_trade_pnl.keys()) if d < today_date)  # USD
   
    # FIXED: Handle zero/negative equity
    if prev_equity_today > 0:
        today_pct = (today_pnl / prev_equity_today * 100)
    elif today_pnl != 0:
        today_pct = today_pnl * 100 # Simplified for first day trading
    else:
        today_pct = 0
   
    # Current week
    current_week_str = datetime.now().strftime('%Y-W%U')
    year, week_num_str = current_week_str.split('-W')
    week_num = int(week_num_str)
   
    try:
        current_week_start = datetime.strptime(f'{year} {week_num} 1', '%Y %U %w').date()
    except ValueError:
        current_week_start = datetime(int(year), 1, 1 + (week_num - 1) * 7).date()
   
    prev_equity_week = net_deposits + sum(daily_trade_pnl[d] * usd_rate for d in sorted(daily_trade_pnl.keys()) if d < current_week_start)  # USD
    this_week_pnl = weekly_trade_pnl.get(current_week_str, {'pnl': 0})['pnl'] * usd_rate  # USD
   
    # FIXED: Handle zero/negative equity
    if prev_equity_week > 0:
        this_week_pct = (this_week_pnl / prev_equity_week * 100)
    elif this_week_pnl != 0:
        this_week_pct = this_week_pnl * 100 # Simplified
    else:
        this_week_pct = 0
   
    # Current month
    current_month_str = datetime.now().strftime('%Y-%m')
    current_month_start = datetime.now().replace(day=1).date()
   
    prev_equity_month = net_deposits + sum(daily_trade_pnl[d] * usd_rate for d in sorted(daily_trade_pnl.keys()) if d < current_month_start)  # USD
    this_month_pnl = monthly_trade_pnl.get(current_month_str, 0) * usd_rate  # USD
   
    # FIXED: Handle zero/negative equity
    if prev_equity_month > 0:
        this_month_pct = (this_month_pnl / prev_equity_month * 100)
    elif this_month_pnl != 0:
        this_month_pct = this_month_pnl * 100 # Simplified
    else:
        this_month_pct = 0
   
    avg_daily_pnl = np.mean([p * usd_rate for p in daily_pnl]) if daily_pnl else 0  # USD
    avg_daily_pct = np.mean([r['return'] for r in daily_returns]) if daily_returns else 0
   
    data['performance_summary'] = {
        'today_pnl': round(today_pnl, 2),
        'today_pct': round(today_pct, 2),
        'this_week_pnl': round(this_week_pnl, 2),
        'this_week_pct': round(this_week_pct, 2),
        'this_month_pnl': round(this_month_pnl, 2),
        'this_month_pct': round(this_month_pct, 2),
        'avg_daily_pnl': round(avg_daily_pnl, 2),
        'avg_daily_pct': round(avg_daily_pct, 2)
    }
   
    # ----- Final Metrics -----
    data['metrics'] = {
        'balance': round(acc.balance * usd_rate, 2),
        'equity': round(acc.equity * usd_rate, 2),
        'margin': round(acc.margin * usd_rate, 2),
        'margin_free': round(acc.margin_free * usd_rate, 2),
        'margin_level': round(acc.margin_level, 2) if acc.margin > 0 else 0,
        'open_profit': round(open_profit, 2),
        'open_trades_count': open_trades_count,
        'total_deposits': round(total_deposits, 2),
        'total_withdrawals': round(total_withdrawals, 2),
        'net_deposits': round(net_deposits, 2),
        'total_profit': round(total_profit, 2),
        'win_rate': round(win_rate, 1),
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'avg_win': round(avg_win, 2),
        'avg_loss': round(abs(avg_loss), 2),
        'profit_factor': round(profit_factor, 2),
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sortino, 2),
        'max_drawdown': round(max_dd, 1),
        'max_dd_duration': max_dd_duration,
        'rr_ratio': round(rr_ratio, 2),
        'expectancy': round(expectancy, 2),
        'recovery_factor': round(recovery_factor, 2),
        'ulcer_index': round(ulcer, 2),
        'avg_duration': round(avg_duration, 0),
        'best_trade': round(best_trade, 2),
        'worst_trade': round(worst_trade, 2),
        'max_win_streak': max_win_streak,
        'max_loss_streak': max_loss_streak,
        'total_pips': round(total_pips, 1),
        'total_commissions': round(total_commissions, 2),
        'total_swaps': round(total_swaps, 2),
        'avg_volume': round(avg_volume, 2),
        'trades_per_month': round(trades_per_month, 1),
        'trades_per_week': round(trades_per_week, 1),
        'roi': round(roi, 1),
        'skewness': round(skewness, 2),
        'kurtosis': round(kurtosis, 2),
        'var_95': round(var_95, 2)
    }
   
    data['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data['account_id'] = acc.login
    data['account_currency'] = acc.currency  # NEW: Include for display
   
    cached_marketing_data = data
    marketing_last_update = datetime.now()
   
    print(f"[SUCCESS] Marketing data refreshed successfully. Total trades: {data['metrics'].get('total_trades', 0)}")
    return data
# Wrap the entire function execution in try-catch
def fetch_marketing_data_safe():
    """Safe wrapper for fetch_marketing_data with comprehensive error handling"""
    try:
        return fetch_marketing_data()
    except Exception as e:
        print(f"[CRITICAL ERROR] Unexpected error in fetch_marketing_data: {str(e)}")
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)
        return {
            'error': 'Critical Error While Fetching Data',
            'error_details': str(e),
            'error_traceback': error_trace.split('\n')[-5:], # Last 5 lines
            'troubleshooting': [
                '1. Check the console/terminal for full error details',
                '2. Verify MT5 is running and connected',
                '3. Try restarting the Flask application',
                '4. Check if all required Python packages are installed',
                '5. Verify your MT5 account has valid data',
                '6. Contact support if the issue persists'
            ]
        }
# BACKGROUND REFRESH - Reduced to every 5 seconds to reduce load
def marketing_data_refresh_thread():
    while True:
        try:
            fetch_marketing_data_safe()
        except Exception as e:
            print(f"[ERROR] Error in marketing refresh thread: {e}")
        time.sleep(5) # Changed from 2 to 5 seconds
# Marketing Dashboard Route
@app.route('/marketing_dashboard')
@login_required
def marketing_dashboard():
    global cached_marketing_data
    if cached_marketing_data is None:
        cached_marketing_data = fetch_marketing_data_safe()
   
    config = tra.load_config()
    mt5_details = {
        'account': config.get('MT5_ACCOUNT'),
        'password': config.get('MT5_PASSWORD'),
        'server': config.get('MT5_SERVER')
    }
   
    return render_template(
        'marketing_dashboard.html',
        data=cached_marketing_data,
        mt5_details=mt5_details,
        myfxbook_link=MYFXBOOK_LINK,
        last_update=cached_marketing_data.get('last_update', 'Never')
    )
# API Data
@app.route('/api/data')
@login_required
def api_marketing_data():
    global cached_marketing_data
    if cached_marketing_data is None:
        cached_marketing_data = fetch_marketing_data_safe()
    return jsonify(cached_marketing_data)
# Quick status endpoint - returns real-time account data
@app.route('/api/quick_status')
@login_required
def api_quick_status():
    """Comprehensive endpoint for frequent polling - returns all real-time account data"""
    try:
        # Ensure MT5 connection
        if not tra.ensure_mt5_connection():
            print("[ERROR] Failed to ensure MT5 connection in /api/quick_status")
            return jsonify({
                'error': 'MT5 Not Connected',
                'troubleshooting': 'Check if MT5 terminal is running and logged in'
            }), 500
       
        # Check MT5 Connection Status
        if not mt5.terminal_info():
            error_code, error_msg = mt5.last_error()
            print(f"[ERROR] Quick status: MT5 not connected. Code: {error_code}, Msg: {error_msg}")
            return jsonify({
                'error': 'MT5 Not Connected',
                'error_code': error_code,
                'error_msg': error_msg,
                'troubleshooting': 'Check if MT5 terminal is running and logged in'
            }), 500
       
        acc = mt5.account_info()
        if not acc:
            error_code, error_msg = mt5.last_error()
            print(f"[ERROR] Quick status: Cannot fetch account info. Code: {error_code}, Msg: {error_msg}")
            return jsonify({
                'error': 'Cannot Fetch Account Info',
                'error_code': error_code,
                'error_msg': error_msg,
                'troubleshooting': 'Verify you are logged into MT5 with correct credentials'
            }), 500
       
        positions = mt5.positions_get()
        if positions is None:
            error_code, error_msg = mt5.last_error()
            print(f"[WARNING] Quick status: Cannot fetch positions. Code: {error_code}, Msg: {error_msg}")
            positions = [] # Continue with empty positions
        else:
            positions = list(positions)
       
        # NEW: Get USD rate
        usd_rate = tra.get_usd_conversion_rate(acc.currency)
       
        # Calculate open profit and positions data (USD)
        open_profit = sum(p.profit * usd_rate for p in positions)
        open_trades_count = len(positions)
       
        # Build comprehensive quick data with ALL account metrics (USD)
        quick_data = {
            'account_id': acc.login,
            'account_currency': acc.currency,  # NEW
            'balance': round(acc.balance * usd_rate, 2),
            'equity': round(acc.equity * usd_rate, 2),
            'margin': round(acc.margin * usd_rate, 2),
            'margin_free': round(acc.margin_free * usd_rate, 2),
            'margin_level': round(acc.margin_level, 2) if acc.margin > 0 else 0,
            'open_profit': round(open_profit, 2),
            'open_trades_count': open_trades_count,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'open_positions': [
                {
                    'symbol': p.symbol,
                    'type': 'Buy' if p.type == 0 else 'Sell',
                    'volume': round(p.volume, 2),
                    'open_price': round(p.price_open, 5),
                    'current_price': round(p.price_current, 5),
                    'profit': round(p.profit * usd_rate, 2),
                    'swap': round(p.swap * usd_rate, 2)
                } for p in positions
            ]
        }
       
        return jsonify(quick_data)
       
    except Exception as e:
        print(f"[CRITICAL ERROR] Quick status exception: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return jsonify({
            'error': 'Critical Error',
            'error_details': str(e),
            'troubleshooting': 'Check console logs for details'
        }), 500
# API Refresh
@app.route('/api/refresh')
@login_required
def api_marketing_refresh():
    data = fetch_marketing_data_safe()
    return jsonify({'status': 'success', 'last_update': data.get('last_update', 'Never')})
# Session check endpoint for JS polling
@app.route('/check_session')
@login_required
def check_session():
    return jsonify({'valid': True})
if __name__ == '__main__':
    init_mt5() # Init persistent connection
    monitor_thread = threading.Thread(target=hours_monitor, daemon=True)
    monitor_thread.start()
    marketing_thread = threading.Thread(target=marketing_data_refresh_thread, daemon=True)
    marketing_thread.start()
    fetch_marketing_data_safe() # Initial cache population to avoid lag on first access
    # For dev only; use 'waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app' for prod
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)