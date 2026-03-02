#!/usr/bin/env python3
# telegram_service.py — Enhanced, presentation-focused Telegram alert service
# - Paste over your existing file
# - Keep TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in env
# - Requires: python-telegram-bot, MetaTrader5, pandas/numpy if already used by tra.py

import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, date, timedelta, time as dt_time
import tempfile
import asyncio
import math

import MetaTrader5 as mt5
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from telegram.constants import ParseMode

import tra  # reuse ensure_mt5_connection and captured_print

# ---------------------------
# Configuration (via env)
# ---------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL = int(os.getenv("TG_POLL_INTERVAL", "10"))
PNL_CHANGE_THRESHOLD = float(os.getenv("TG_PNL_CHANGE_USD", "50"))  # Increased for less frequent updates
ACCOUNT_CHANGE_THRESHOLD = float(os.getenv("TG_ACC_CHANGE_USD", "1"))
CONNECT_ALERT_COOLDOWN = int(os.getenv("TG_CONN_COOLDOWN", "3600"))
ACCOUNT_ALERT_COOLDOWN = int(os.getenv("TG_ACCOUNT_ALERT_COOLDOWN", "1200"))  # New: cooldown for account updates, default 20 mins (1200s)
STATE_FILE = os.getenv("TG_STATE_FILE", "telegram_state.json")
LOG_FILE = os.getenv("TG_LOG_FILE", "telegram_service.log")
DEAL_RETRIES = int(os.getenv("TG_DEAL_RETRIES", "3"))
DEAL_RETRY_DELAY = float(os.getenv("TG_DEAL_RETRY_DELAY", "0.5"))
SEND_STARTUP_MESSAGE = os.getenv("TG_SEND_STARTUP", "1") == "1"

# Removed floating profit/loss hit thresholds; alerts only on close
DRAWDOWN_WARN_PCT = float(os.getenv("TG_DRAWDOWN_WARNING_PCT", "10.0"))

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

config_data = tra.load_config()

# ---------------------------
# Logging setup
# ---------------------------
logger = logging.getLogger("telegram_service")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(ch)
fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%SZ"))
logger.addHandler(fh)

def log_and_capture(level, tag, *args):
    """
    Structured logging + mirror to tra.captured_print
    Tag example: TRADE_OPEN, TRADE_CLOSE, ACCOUNT_WARN, MT5_CONN
    """
    msg = f"[{tag}] " + " ".join(map(str, args))
    if level == "debug":
        logger.debug(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.info(msg)
    try:
        tra.captured_print(msg)
    except Exception:
        logger.debug("tra.captured_print failed", exc_info=True)

# ---------------------------
# State persistence
# ---------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                if "last_positions" in s:
                    s["last_positions"] = {int(k): v for k, v in s["last_positions"].items()}
                return s
        except Exception:
            logger.exception("Failed to load state file — starting fresh")
    return {
        "last_positions": {},      # ticket -> metadata
        "last_account": {"balance": 0.0, "equity": 0.0, "margin": 0.0, "margin_free": 0.0, "profit": 0.0},
        "last_daily_pnl": 0.0,
        "last_alert_times": {}     # keys: connection_loss, account_change, trade_* per ticket
    }

def save_state_atomic(state_obj):
    s = dict(state_obj)
    lp = s.get("last_positions", {})
    s["last_positions"] = {str(k): v for k, v in lp.items()}
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="tgstate_", dir=".")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(s, f, indent=2, default=float)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        logger.exception("Failed to save state atomically")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

state = load_state()

# Helper to safely send messages with retries
async def send_message_safe(bot, text, parse_mode=ParseMode.HTML, disable_preview=True, retries=2):
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=disable_preview)
            return True
        except Exception as e:
            logger.exception("Failed to send message (attempt %d)", attempt)
            time.sleep(0.5 * attempt)
    return False

# ---------------------------
# MT5 helpers (robust)
# ---------------------------
def ensure_connection(retries=3, backoff=0.5):
    for attempt in range(1, retries + 1):
        try:
            ok = tra.ensure_mt5_connection()
            if ok:
                info = mt5.account_info()
                if info is not None:
                    log_and_capture("debug", "MT5_CONN", f"MT5 connected (attempt {attempt}). Account: {getattr(info,'login','unknown')}")
                    return True
            else:
                log_and_capture("warning", "MT5_CONN", f"tra.ensure_mt5_connection returned False (attempt {attempt})")
        except Exception:
            logger.exception("Exception ensuring MT5 connection")
        time.sleep(backoff * attempt)
    log_and_capture("error", "MT5_CONN", "MT5 connection failed after retries")
    return False

def get_account_info_from_mt5():
    if not ensure_connection():
        return {"error": "MT5 connection failed", "balance": 0.0, "equity": 0.0, "margin": 0.0, "margin_free": 0.0, "profit": 0.0}
    info = mt5.account_info()
    if info is None:
        return {"error": "account_info() returned None", "balance": 0.0, "equity": 0.0, "margin": 0.0, "margin_free": 0.0, "profit": 0.0}
    res = {
        "balance": float(getattr(info, "balance", 0.0)),
        "equity": float(getattr(info, "equity", 0.0)),
        "margin": float(getattr(info, "margin", 0.0)),
        "margin_free": float(getattr(info, "margin_free", 0.0)),
    }
    profit_val = getattr(info, "profit", None)
    if profit_val is None:
        # sum open positions profit
        positions = mt5.positions_get()
        profit_sum = 0.0
        if positions:
            for p in positions:
                try:
                    profit_sum += float(getattr(p, "profit", 0.0) or 0.0)
                except Exception:
                    continue
        res["profit"] = float(profit_sum)
    else:
        res["profit"] = float(profit_val)
    return res

def get_positions_from_mt5(symbol=None):
    if not ensure_connection():
        return None
    try:
        positions = mt5.positions_get(symbol=config_data.get("SYMBOL") if symbol is None else symbol)
    except Exception:
        logger.exception("positions_get failed")
        positions = None
    if positions is None:
        return {}
    d = {}
    for p in positions:
        try:
            d[int(p.ticket)] = {
                "type": int(p.type),
                "volume": float(p.volume),
                "price_open": float(getattr(p, "price_open", getattr(p, "price", 0.0))),
                "price_current": float(getattr(p, "price_current", getattr(p, "price", 0.0))),
                "profit": float(getattr(p, "profit", 0.0) or 0.0),
                "time_setup": int(getattr(p, "time", getattr(p, "time_setup", time.time()))),
                "sl": float(getattr(p, "sl", 0.0) or 0.0),
                "tp": float(getattr(p, "tp", 0.0) or 0.0),
            }
        except Exception:
            logger.exception("Failed parsing position object")
    return d

def _history_deals_get_with_retries(dt_from, dt_to, symbol=None, retries=DEAL_RETRIES, delay=DEAL_RETRY_DELAY):
    for attempt in range(1, retries + 1):
        try:
            try:
                deals = mt5.history_deals_get(dt_from, dt_to, symbol) if symbol else mt5.history_deals_get(dt_from, dt_to)
            except TypeError:
                deals = mt5.history_deals_get(dt_from, dt_to)
            if deals is not None:
                return deals
        except Exception:
            logger.debug("history_deals_get(datetime) attempt %d failed", attempt, exc_info=True)
            try:
                tra.ensure_mt5_connection()
            except Exception:
                pass
            time.sleep(delay * attempt)
    # try integer timestamps
    try:
        from_ts = int(dt_from.timestamp())
        to_ts = int(dt_to.timestamp())
        for attempt in range(1, retries + 1):
            try:
                try:
                    deals = mt5.history_deals_get(from_ts, to_ts, symbol) if symbol else mt5.history_deals_get(from_ts, to_ts)
                except TypeError:
                    deals = mt5.history_deals_get(from_ts, to_ts)
                if deals is not None:
                    return deals
            except Exception:
                logger.debug("history_deals_get(int) attempt %d failed", attempt, exc_info=True)
                try:
                    tra.ensure_mt5_connection()
                except Exception:
                    pass
                time.sleep(delay * attempt)
    except Exception:
        logger.debug("failed building int timestamps", exc_info=True)
    return None

def _symbol_match(config_sym, deal_sym):
    if not deal_sym or not config_sym:
        return False
    a = str(deal_sym).lower()
    b = str(config_sym).lower()
    if a == b or b in a or a in b:
        return True
    return False

def get_daily_realized_pnl(symbol=None):
    symbol = symbol or config_data.get("SYMBOL")
    if not ensure_connection():
        return 0.0
    today = datetime.now(timezone.utc).date()
    from_dt = datetime.combine(today, dt_time.min, tzinfo=timezone.utc)
    to_dt = datetime.now(timezone.utc)
    deals = _history_deals_get_with_retries(from_dt, to_dt, symbol=symbol)
    if deals is None:
        deals = _history_deals_get_with_retries(from_dt, to_dt, symbol=None)
    if not deals:
        log_and_capture("debug", "DAILY_PNL", f"No deals returned for daily PnL query for {symbol}")
        return 0.0
    entry_candidates = set()
    for name in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT", "DEAL_ENTRY_OUT_BY"):
        if hasattr(mt5, name):
            entry_candidates.add(getattr(mt5, name))
    if not entry_candidates:
        entry_candidates = {1}
    pnl = 0.0
    count = 0
    for d in deals:
        try:
            d_symbol = getattr(d, "symbol", "") or ""
            if not _symbol_match(symbol, d_symbol):
                continue
            entry = getattr(d, "entry", None)
            if entry in entry_candidates:
                profit = float(getattr(d, "profit", 0.0) or 0.0)
                pnl += profit
                count += 1
        except Exception:
            continue
    pnl = round(float(pnl), 2)
    log_and_capture("debug", "DAILY_PNL", f"Calculated daily realized PnL for {symbol}: {pnl:.2f} from {count} deals")
    return pnl

def get_close_info_for_ticket(ticket):
    if not ensure_connection():
        return None
    try:
        deals = mt5.history_deals_get(position=int(ticket))
    except Exception:
        today = datetime.now(timezone.utc).date()
        from_dt = datetime.combine(today - timedelta(days=7), dt_time.min, tzinfo=timezone.utc)
        deals = _history_deals_get_with_retries(from_dt, datetime.now(timezone.utc))
    if not deals:
        return None
    entry_candidates = set()
    for name in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT", "DEAL_ENTRY_OUT_BY"):
        if hasattr(mt5, name):
            entry_candidates.add(getattr(mt5, name))
    if not entry_candidates:
        entry_candidates = {1}
    last = None
    for d in deals:
        try:
            pos_id = getattr(d, "position_id", None) or getattr(d, "position", None) or getattr(d, "position_ticket", None)
            if pos_id is not None and int(pos_id) != int(ticket):
                continue
            entry = getattr(d, "entry", None)
            if entry in entry_candidates:
                last = d
        except Exception:
            continue
    if not last:
        return None
    return {
        "price_close": float(getattr(last, "price", getattr(last, "price_close", 0.0))),
        "profit": float(getattr(last, "profit", 0.0) or 0.0),
        "time": getattr(last, "time", None)
    }

# ---------------------------
# Pretty HTML format helpers
# ---------------------------
def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def money(x):
    try:
        return f"{float(x):+.2f}"
    except Exception:
        return str(x)

def pretty_num(x, decimals=5):
    try:
        if abs(float(x)) >= 1000:
            return f"{float(x):,.2f}"
        return f"{float(x):.{decimals}f}"
    except Exception:
        return str(x)

def highlight(text):
    return f"<b>{text}</b>"

def subtle(text):
    return f"<i>{text}</i>"

SEPARATOR = "\n\n---\n\n"  # Simplified separator with spacing

def craft_trade_message(event, ticket, pos, account, daily_realized, unrealized, close_info=None, extra_note=None):
    ts = utc_now_iso()
    direction = "🟢 Buy" if pos.get("type") == getattr(mt5, "ORDER_TYPE_BUY", 0) else "🔴 Sell"
    lot = pos.get("volume", 0.0)
    entry = pretty_num(pos.get("price_open", 0.0))
    curr = pretty_num(close_info["price_close"] if event == "Trade Close" and close_info else pos.get("price_current", pos.get("price_open", 0.0)))
    pnl = pos.get("profit", 0.0)
    entry_balance = pos.get("entry_balance", account.get("balance", 0.0) or 1.0)
    pnl_pct_entry = (pnl / entry_balance) * 100 if entry_balance else 0.0
    pnl_pct_base = (pnl / (config_data.get("BASE_BALANCE") or 1.0)) * 100
    daily_pct = (daily_realized / (account.get("balance",1.0) - daily_realized)) * 100 if (account.get("balance",1.0) - daily_realized) > 0 else 0.0
    setup_time = datetime.fromtimestamp(pos.get("time_setup", time.time()), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    sl = pretty_num(pos.get("sl", 0.0))
    tp = pretty_num(pos.get("tp", 0.0))

    event_title = event
    if pnl > 0:
        pnl_emoji = " ✅📈"
    elif pnl < 0:
        pnl_emoji = " ⚠️📉"
    else:
        pnl_emoji = ""

    lines = []
    lines.append(f"{highlight(event_title)}{pnl_emoji}")
    lines.append(subtle(ts))
    lines.append("")
    lines.append(f"💱 {highlight('Symbol:')} <code>{config_data.get('SYMBOL')}</code>")
    lines.append(f"🎟️ {highlight('Ticket:')} <code>{ticket}</code>")
    lines.append("")
    lines.append(f"🧭 {highlight('Side:')} {direction}")
    lines.append(f"📏 {highlight('Lots:')} {lot:.2f}")
    lines.append("")
    lines.append(f"📥 {highlight('Entry Price:')} <code>{entry}</code>")
    lines.append(f"📤 {highlight('Current/Exit Price:')} <code>{curr}</code>")
    lines.append("")
    lines.append(f"⏰ {highlight('Setup Time:')} {subtle(setup_time)}")
    lines.append(f"🛑 {highlight('Stop Loss:')} <code>{sl}</code>")
    lines.append(f"🎯 {highlight('Take Profit:')} <code>{tp}</code>")
    lines.append("")
    lines.append(f"💵 {highlight('PnL:')} <code>{money(pnl)}</code>")
    lines.append(f"📊 {highlight('PnL% (entry):')} <code>{pnl_pct_entry:+.2f}%</code>")
    lines.append(f"📊 {highlight('PnL% (base):')} <code>{pnl_pct_base:+.2f}%</code>")
    lines.append("")
    lines.append(f"📅 {highlight('Realized Today:')} <code>{money(daily_realized)}</code> ({daily_pct:+.2f}%)")
    lines.append(f"🔄 {highlight('Unrealized Open:')} <code>{money(unrealized)}</code>")
    lines.append("")
    lines.append(f"🏦 {highlight('Balance:')} <code>{pretty_num(account.get('balance'))}</code>")
    lines.append(f"⚖️ {highlight('Equity:')} <code>{pretty_num(account.get('equity'))}</code>")
    lines.append(f"🛡️ {highlight('Margin:')} <code>{pretty_num(account.get('margin'))}</code>")
    lines.append(f"🆓 {highlight('Free Margin:')} <code>{pretty_num(account.get('margin_free', 0.0))}</code>")
    if extra_note:
        lines.append("")
        lines.append(f"📝 {highlight('Note:')} {extra_note}")
    lines.append(SEPARATOR)
    return "\n".join(lines)

def craft_account_change_message(changes, account, daily_realized, unrealized, extra_note=None):
    ts = utc_now_iso()
    lines = [f"{highlight('Account Update')} ⚙️📊", subtle(ts), ""]
    for k, v in changes.items():
        delta_emoji = " 📈" if v > 0 else " 📉"
        lines.append(f"{highlight(k + ' Change:')}{delta_emoji} <code>{money(v)}</code>")
        lines.append("")
    drawdown = ((account.get("balance",0.0) - account.get("equity",0.0)) / (account.get("balance",1.0))) * 100 if account.get("balance",0.0) else 0.0
    exposure = (account.get("margin",0.0) / account.get("equity",1.0)) * 100 if account.get("equity",0.0) else 0.0
    lines.append(f"🏦 {highlight('Balance:')} <code>{pretty_num(account.get('balance',0.0))}</code>")
    lines.append(f"⚖️ {highlight('Equity:')} <code>{pretty_num(account.get('equity',0.0))}</code>")
    lines.append("")
    lines.append(f"🛡️ {highlight('Margin:')} <code>{pretty_num(account.get('margin',0.0))}</code>")
    lines.append(f"🆓 {highlight('Free Margin:')} <code>{pretty_num(account.get('margin_free',0.0))}</code>")
    lines.append("")
    lines.append(f"📉 {highlight('Drawdown:')} <code>{drawdown:.2f}%</code>")
    lines.append(f"📊 {highlight('Exposure:')} <code>{exposure:.2f}%</code>")
    lines.append("")
    lines.append(f"📅 {highlight('Realized Today:')} <code>{money(daily_realized)}</code>")
    lines.append(f"🔄 {highlight('Unrealized:')} <code>{money(unrealized)}</code>")
    if extra_note:
        lines.append("")
        lines.append(f"📝 {highlight('Note:')} {extra_note}")
    lines.append(SEPARATOR)
    return "\n".join(lines)

# ---------------------------
# Monitor: detection + events
# ---------------------------
monitor_lock = None  # will be created as asyncio.Lock in main()

async def monitor_callback(context: ContextTypes.DEFAULT_TYPE):
    global monitor_lock
    if monitor_lock is None:
        monitor_lock = asyncio.Lock()
    if monitor_lock.locked():
        log_and_capture("warning", "MONITOR", "Previous monitor run still in progress — skipping poll")
        return
    await monitor_lock.acquire()
    try:
        now_ts = time.time()
        account = get_account_info_from_mt5()
        if "error" in account:
            log_and_capture("warning", "MT5_CONN", f"Account info not available: {account.get('error')}")
            if now_ts - state["last_alert_times"].get("connection_loss", 0) > CONNECT_ALERT_COOLDOWN:
                try:
                    await send_message_safe(context.bot, craft_account_change_message({}, account, 0.0, 0.0, extra_note="⚠️ MT5 connection lost — service will retry automatically."))
                except Exception:
                    logger.exception("Failed to send connection lost message")
                state["last_alert_times"]["connection_loss"] = now_ts
                save_state_atomic(state)
            return

        positions = get_positions_from_mt5()
        daily_realized = get_daily_realized_pnl()
        unrealized = account.get("profit", 0.0)

        # Detect external account changes (deposits/withdrawals)
        prev_balance = state.get("last_account", {}).get("balance", 0.0)
        balance_delta = account.get("balance", 0.0) - prev_balance
        pnl_delta = daily_realized - state.get("last_daily_pnl", 0.0)
        external_change = abs(balance_delta - pnl_delta) > ACCOUNT_CHANGE_THRESHOLD
        external_note = None
        if external_change:
            if balance_delta > 0:
                external_note = "💵 Deposit detected."
            else:
                external_note = "💸 Withdrawal detected."

        # Account-level changes — break into per-field messages for clarity
        changes = {}
        for key in ("balance", "equity", "margin", "margin_free"):
            new_val = account.get(key, 0.0)
            old_val = state.get("last_account", {}).get(key, 0.0)
            delta = float(new_val - old_val)
            if abs(delta) > ACCOUNT_CHANGE_THRESHOLD:
                changes[key.capitalize()] = delta

        # If drawdown surpasses threshold -> alert
        if account.get("balance", 0.0) > 0:
            drawdown_pct = ((account.get("balance",0.0) - account.get("equity",0.0)) / account.get("balance",1.0)) * 100
        else:
            drawdown_pct = 0.0

        if (drawdown_pct >= DRAWDOWN_WARN_PCT) and (now_ts - state["last_alert_times"].get("drawdown_warn", 0) > CONNECT_ALERT_COOLDOWN):
            note = f"⚠️ Drawdown exceeded {DRAWDOWN_WARN_PCT:.1f}% — current drawdown {drawdown_pct:.2f}%"
            await send_message_safe(context.bot, craft_account_change_message({}, account, daily_realized, unrealized, extra_note=note))
            state["last_alert_times"]["drawdown_warn"] = now_ts
            log_and_capture("warning", "ACCOUNT_WARN", note)

        if changes and (now_ts - state["last_alert_times"].get("account_change", 0) > ACCOUNT_ALERT_COOLDOWN):
            msg = craft_account_change_message(changes, account, daily_realized, unrealized, extra_note=external_note)
            await send_message_safe(context.bot, msg)
            state["last_alert_times"]["account_change"] = now_ts
            log_and_capture("info", "ACCOUNT_CHANGE", f"Changes: {changes}")

        # Update daily_pnl cache
        if abs(daily_realized - state.get("last_daily_pnl", 0.0)) > ACCOUNT_CHANGE_THRESHOLD:
            state["last_daily_pnl"] = daily_realized

        # Track trade events
        last_positions = state.setdefault("last_positions", {})
        current_positions = positions or {}

        # New opens
        for ticket, p in current_positions.items():
            if ticket not in last_positions:
                p_meta = dict(p)
                p_meta["last_profit"] = p.get("profit", 0.0)
                p_meta["entry_balance"] = account.get("balance", 0.0)
                last_positions[ticket] = p_meta
                msg = craft_trade_message("Trade Open", ticket, p_meta, account, daily_realized, unrealized)
                await send_message_safe(context.bot, msg)
                state["last_alert_times"][f"trade_open_{ticket}"] = now_ts
                log_and_capture("info", "TRADE_OPEN", f"Ticket {ticket} opened: {p_meta}")

        # Updates
        for ticket, p in current_positions.items():
            if ticket in last_positions:
                last_profit = float(last_positions[ticket].get("last_profit", 0.0))
                now_profit = float(p.get("profit", 0.0))
                # major floating PnL change -> update (with higher threshold)
                if abs(now_profit - last_profit) > PNL_CHANGE_THRESHOLD and (now_ts - state["last_alert_times"].get(f"trade_update_{ticket}", 0) > 300):
                    msg = craft_trade_message("Trade Update", ticket, p, account, daily_realized, unrealized)
                    await send_message_safe(context.bot, msg)
                    state["last_alert_times"][f"trade_update_{ticket}"] = now_ts
                    log_and_capture("info", "TRADE_UPDATE", f"Ticket {ticket} update: {now_profit:+.2f}")

                # refresh
                last_positions[ticket]["last_profit"] = now_profit
                last_positions[ticket].update({"price_current": p.get("price_current"), "profit": now_profit})

        # Closures
        closed_tickets = [t for t in list(last_positions.keys()) if t not in current_positions]
        for ticket in closed_tickets:
            close_info = get_close_info_for_ticket(ticket)
            p_meta = last_positions.get(ticket, {})
            if close_info:
                p_meta["profit"] = close_info.get("profit", p_meta.get("profit", 0.0))
                # Detect SL/TP/Manual close
                close_price = close_info.get("price_close", 0.0)
                sl = p_meta.get("sl", 0.0)
                tp = p_meta.get("tp", 0.0)
                note = ""
                if abs(close_price - sl) < 0.0001 and sl != 0.0:
                    note = "⚠️ Hit Stop Loss."
                elif abs(close_price - tp) < 0.0001 and tp != 0.0:
                    note = "✅ Hit Take Profit."
                else:
                    note = "Manual Close."
                if p_meta.get("profit", 0.0) > 0:
                    note += " ✅ Profitable trade."
                else:
                    note += " ⚠️ Loss realized."
                msg = craft_trade_message("Trade Close", ticket, p_meta, account, daily_realized, unrealized, close_info=close_info, extra_note=note)
                await send_message_safe(context.bot, msg)
                state["last_alert_times"][f"trade_close_{ticket}"] = now_ts
                log_and_capture("info", "TRADE_CLOSE", f"Ticket {ticket} closed: profit={p_meta.get('profit'):+.2f}")
            else:
                # fallback generic close message
                msg = craft_trade_message("Trade Close", ticket, p_meta, account, daily_realized, unrealized, extra_note="Closed (details unavailable).")
                await send_message_safe(context.bot, msg)
                log_and_capture("info", "TRADE_CLOSE", f"Ticket {ticket} closed (no deal details)")
            # cleanup
            last_positions.pop(ticket, None)

        # Update account snapshot and persist
        state["last_account"] = {k: float(v) for k, v in account.items() if k != "error"}
        save_state_atomic(state)
        log_and_capture("info", "MONITOR", f"Poll complete: {len(current_positions)} open trades, daily realized PnL {daily_realized:.2f}")
    except Exception:
        logger.exception("Unhandled exception in monitor_callback")
        if time.time() - state["last_alert_times"].get("monitor_exception", 0) > CONNECT_ALERT_COOLDOWN:
            try:
                await send_message_safe(context.bot, f"⚠️ {highlight('Monitor exception occurred')} — check logs: {LOG_FILE}")
            except Exception:
                pass
            state["last_alert_times"]["monitor_exception"] = time.time()
            save_state_atomic(state)
    finally:
        try:
            monitor_lock.release()
        except Exception:
            pass

# ---------------------------
# Commands (pretty outputs)
# ---------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/start requested by {getattr(user,'id',None)}")
    welcome_msg = [
        highlight('Welcome to Trading Alert Bot') + " 🚀🌟",
        "",
        "This bot monitors your MT5 account and sends real-time alerts for trades, account changes, and performance updates. 📈💹",
        "",
        "Stay informed and make better decisions! Use /help for a list of commands. 📋",
        SEPARATOR
    ]
    await update.message.reply_text("\n".join(welcome_msg), parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/help requested by {getattr(user,'id',None)}")
    help_msg = [
        highlight('Available Commands') + " 🛠️📜",
        "",
        "/start - Welcome message and bot info 🚀",
        "/help - Show this command list 📋",
        "/status - Get current account status 🏦",
        "/open - List open trades 📂",
        "/closed - Today's closed trades summary 🔒",
        "/performance - Cumulative performance 📈",
        "/health - System health check 🩺",
        "/price - Get current symbol price 💱",
        "/debug_pnl - Debug recent PnL deals 🛠️",
        "/test_send - Send a test message ✅",
        SEPARATOR
    ]
    await update.message.reply_text("\n".join(help_msg), parse_mode=ParseMode.HTML)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/status requested by {getattr(user,'id',None)}")
    account = get_account_info_from_mt5()
    if "error" in account:
        await update.message.reply_text("⚠️ Error fetching account status from MT5.", parse_mode=ParseMode.HTML)
        return
    drawdown = ((account.get("balance",0.0) - account.get("equity",0.0)) / (account.get("balance",1.0))) * 100 if account.get("balance",0.0) else 0.0
    exposure = (account.get("margin",0.0) / account.get("equity",1.0)) * 100 if account.get("equity",0.0) else 0.0
    lines = [highlight('Account Status') + " 🏦⚖️", subtle(utc_now_iso()), ""]
    lines += [
        f"💰 {highlight('Balance:')} <code>{pretty_num(account.get('balance',0.0))}</code>",
        "",
        f"⚖️ {highlight('Equity:')} <code>{pretty_num(account.get('equity',0.0))}</code>",
        "",
        f"🛡️ {highlight('Margin:')} <code>{pretty_num(account.get('margin',0.0))}</code>",
        "",
        f"🆓 {highlight('Free Margin:')} <code>{pretty_num(account.get('margin_free',0.0))}</code>",
        "",
        f"🔄 {highlight('Unrealized PnL:')} <code>{money(account.get('profit',0.0))}</code>",
        "",
        f"📉 {highlight('Drawdown:')} <code>{drawdown:.2f}%</code>",
        "",
        f"📊 {highlight('Exposure:')} <code>{exposure:.2f}%</code>",
        SEPARATOR
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/open requested by {getattr(user,'id',None)}")
    positions = get_positions_from_mt5()
    account = get_account_info_from_mt5()
    if positions is None:
        await update.message.reply_text("⚠️ Error fetching open trades.", parse_mode=ParseMode.HTML)
        return
    if not positions:
        await update.message.reply_text("No open trades at the moment. 😌", parse_mode=ParseMode.HTML)
        return
    lines = [highlight('Open Trades') + " 📂🔍", subtle(utc_now_iso()), ""]
    for t, p in positions.items():
        pnl = p.get("profit", 0.0)
        entry_balance = state.get("last_positions", {}).get(t, {}).get("entry_balance", account.get("balance",1.0))
        pnl_pct = (pnl / entry_balance) * 100 if entry_balance else 0.0
        typ = "🟢 Buy" if p.get("type") == getattr(mt5, "ORDER_TYPE_BUY", 0) else "🔴 Sell"
        setup_time = datetime.fromtimestamp(p.get("time_setup", time.time()), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        sl = pretty_num(p.get("sl", 0.0))
        tp = pretty_num(p.get("tp", 0.0))
        lines.append(f"🎟️ {highlight('Ticket:')} <code>{t}</code> — {typ} {p.get('volume',0.0):.2f} lots @ <code>{pretty_num(p.get('price_open'))}</code>")
        lines.append(f"📊 Current Price: <code>{pretty_num(p.get('price_current'))}</code>")
        lines.append(f"💵 PnL: <code>{money(pnl)}</code> ({pnl_pct:+.2f}%)")
        lines.append(f"⏰ Setup Time: {subtle(setup_time)}")
        lines.append(f"🛑 Stop Loss: <code>{sl}</code>")
        lines.append(f"🎯 Take Profit: <code>{tp}</code>")
        lines.append("")
    lines.append(SEPARATOR)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_closed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/closed requested by {getattr(user,'id',None)}")
    try:
        closed = tra.get_closed_trades(page=1)
        if "error" in closed:
            await update.message.reply_text("⚠️ Error fetching closed trades from tra.", parse_mode=ParseMode.HTML)
            return
        summary = closed.get("summary", {})
        lines = [highlight("Today's Closed Trades Summary") + " 🔒📊", subtle(utc_now_iso()), ""]
        lines.append(f"📊 Total Trades: <code>{summary.get('total_trades',0)}</code>")
        lines.append(f"🏆 Win Rate: <code>{summary.get('win_rate',0.0):.1f}%</code>")
        lines.append("")
        lines.append(f"💵 Net Profit: <code>{money(summary.get('net_profit',0.0))}</code>")
        lines.append(f"📈 Sharpe Ratio: <code>{summary.get('sharpe_ratio',0.0):.2f}</code>")
        lines.append(f"⚖️ Profit Factor: <code>{summary.get('profit_factor',0.0):.2f}</code>")
        lines.append(SEPARATOR)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("cmd_closed failed")
        await update.message.reply_text("⚠️ Error computing closed trades summary.", parse_mode=ParseMode.HTML)

async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/performance requested by {getattr(user,'id',None)}")
    try:
        net_profit = 0.0
        total_trades = 0
        page = 1
        while True:
            closed = tra.get_closed_trades(page)
            if "error" in closed:
                await update.message.reply_text("⚠️ Error fetching performance from tra.", parse_mode=ParseMode.HTML)
                return
            summary = closed.get("summary", {})
            net_profit += float(summary.get("net_profit", 0.0))
            total_trades += int(summary.get("total_trades", 0))
            if page >= int(closed.get("total_pages", 1)):
                break
            page += 1
        lines = [highlight('Cumulative Performance') + " 📈💹", subtle(utc_now_iso()), ""]
        lines.append(f"📊 Total Trades: <code>{total_trades}</code>")
        lines.append(f"💵 Net Profit: <code>{money(net_profit)}</code>")
        lines.append(SEPARATOR)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("cmd_performance failed")
        await update.message.reply_text("⚠️ Error computing cumulative performance.", parse_mode=ParseMode.HTML)

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/health requested by {getattr(user,'id',None)}")
    connected = ensure_connection()
    symbol_info = None
    try:
        symbol_info = mt5.symbol_info(config_data.get("SYMBOL"))
    except Exception:
        pass
    symbol_selected = bool(symbol_info and getattr(symbol_info, "select", True))
    running = False
    last_trade_time = "N/A"
    csv_file = tra.get_today_csv("system_logs")
    try:
        if os.path.exists(csv_file):
            with open(csv_file, "r") as f:
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    try:
                        last_ts_str = last_line.split(",")[0]
                        last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - last_ts) < timedelta(minutes=5):
                            running = True
                    except Exception:
                        pass
    except Exception:
        logger.exception("health log read failed")
    pred_csv = tra.get_today_csv("predictions")
    try:
        if os.path.exists(pred_csv):
            with open(pred_csv, "r") as f:
                lines = f.readlines()
                if len(lines) > 1:
                    last_trade_time = lines[-1].split(",")[0]
    except Exception:
        logger.exception("health pred read failed")
    lines = [
        highlight('System Health') + " 🩺🔍", subtle(utc_now_iso()),
        "",
        f"🤖 Trading Running (recent logs): {'✅ Yes' if running else '❌ No'}",
        "",
        f"🔌 MT5 Connected: {'✅ Yes' if connected else '❌ No'}",
        "",
        f"💱 Symbol Selected: {'✅ Yes' if symbol_selected else '❌ No'}",
        "",
        f"⏰ Last Prediction Timestamp: {last_trade_time}",
        SEPARATOR
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/price requested by {getattr(user,'id',None)}")
    if not ensure_connection():
        await update.message.reply_text("⚠️ MT5 not connected.", parse_mode=ParseMode.HTML)
        return
    symbol = config_data.get("SYMBOL")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        await update.message.reply_text(f"⚠️ Error fetching price for {symbol}.", parse_mode=ParseMode.HTML)
        return
    last = pretty_num(getattr(tick, "last", 0.0))
    bid = pretty_num(getattr(tick, "bid", 0.0))
    ask = pretty_num(getattr(tick, "ask", 0.0))
    lines = [
        highlight('Current Price for') + f" <code>{symbol}</code> 💱📊", subtle(utc_now_iso()),
        "",
        f"📊 Last: <code>{last}</code>",
        "",
        f"🟢 Ask: <code>{ask}</code>",
        "",
        f"🔴 Bid: <code>{bid}</code>",
        SEPARATOR
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_debug_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/debug_pnl requested by {getattr(user,'id',None)}")
    n = 10
    try:
        args = context.args
        if args and args[0].isdigit():
            n = min(100, max(1, int(args[0])))
    except Exception:
        pass
    if not ensure_connection():
        await update.message.reply_text("⚠️ MT5 not connected.", parse_mode=ParseMode.HTML)
        return
    today = datetime.now(timezone.utc).date()
    from_dt = datetime.combine(today - timedelta(days=7), dt_time.min, tzinfo=timezone.utc)
    to_dt = datetime.now(timezone.utc)
    deals = _history_deals_get_with_retries(from_dt, to_dt, symbol=None)
    if not deals:
        await update.message.reply_text("No deals returned (or error). Check logs.", parse_mode=ParseMode.HTML)
        return
    last = deals[-n:]
    msgs = [highlight('Last deals (raw)') + " 🛠️🔍", subtle(utc_now_iso()), ""]
    for d in last:
        try:
            msgs.append(f"time={getattr(d,'time',None)} symbol={getattr(d,'symbol',None)} price={getattr(d,'price',None)} profit={getattr(d,'profit',None)} entry={getattr(d,'entry',None)} position_id={getattr(d,'position_id',None)}")
            msgs.append("")
        except Exception:
            msgs.append(str(d))
            msgs.append("")
    msgs.append(SEPARATOR)
    await update.message.reply_text("\n".join(msgs), parse_mode=ParseMode.HTML)

async def cmd_test_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_and_capture("info", "CMD", f"/test_send requested by {getattr(user,'id',None)}")
    ok = await send_message_safe(context.bot, f"{highlight('Telegram service test')} ✅ \n{subtle(utc_now_iso())}")
    if ok:
        await update.message.reply_text("✅ Test message sent to configured chat.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed to send test message. Check logs.", parse_mode=ParseMode.HTML)

# ---------------------------
# Bootstrap + jobs
# ---------------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("closed", cmd_closed))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("debug_pnl", cmd_debug_pnl))
    app.add_handler(CommandHandler("test_send", cmd_test_send))

def start_jobs(app: Application):
    app.job_queue.run_repeating(monitor_callback, interval=POLL_INTERVAL, first=5)
    if SEND_STARTUP_MESSAGE:
        async def startup_notify(context: ContextTypes.DEFAULT_TYPE):
            try:
                if ensure_connection():
                    info = mt5.account_info()
                    acct = getattr(info, "login", "unknown") if info else "unknown"
                    msg = [
                        highlight('Telegram Trading Alert Service Started') + " 🚀🌟",
                        "",
                        f"🏦 Account: <code>{acct}</code>",
                        "",
                        f"⏰ Time: {utc_now_iso()}",
                        "",
                        "Monitoring trades and account changes. 📈💹",
                        "Use /help for commands. 📋",
                        SEPARATOR
                    ]
                    await send_message_safe(context.bot, "\n".join(msg))
                    log_and_capture("info", "STARTUP", "Startup message sent")
            except Exception:
                logger.exception("Startup notify failed")
        app.job_queue.run_once(startup_notify, when=3)

def main():
    global monitor_lock
    monitor_lock = asyncio.Lock()
    log_and_capture("info", "STARTUP", "Starting telegram_service")
    ensure_connection()
    app = Application.builder().token(BOT_TOKEN).build()
    register_handlers(app)
    start_jobs(app)
    log_and_capture("info", "STARTUP", "Telegram bot initialized and jobs scheduled")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        log_and_capture("info", "SHUTDOWN", "KeyboardInterrupt — exiting")
    except Exception:
        logger.exception("Application crashed")
    finally:
        try:
            save_state_atomic(state)
        except Exception:
            logger.exception("Failed saving state on exit")
        try:
            mt5.shutdown()
        except Exception:
            logger.exception("MT5 shutdown failed")

if __name__ == "__main__":
    main()