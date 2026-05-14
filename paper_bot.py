import json
import os
import sys
import asyncio
import threading
import time
import traceback
from html import escape
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from py_clob_client_v2 import ClobClient, OrderType, Side
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

load_dotenv(".env")

# --- Telegram settings ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
TELEGRAM_MENU_BUTTON_STATS = "Статистика (PAPER)"
STRATEGY_DISPLAY_NAME = "Paper Trader v1 (VIRTUAL)"
STARTING_BALANCE = 100.0
TELEGRAM_MENU_BUTTON_STOP = "stop"

# --- Strategy settings (Те же самые, что в вашем коде) ---
BET_AMOUNT = 1.2
MIN_PRICE = 0.08
MAX_PRICE = 0.25
HISTORY_WINDOW_HOURS = 2.0
DROP_PERCENT_REQUIRED = 0.10
RECOVERY_TARGET_PERCENT = 0.50
MIN_PROFIT_PERCENT = 0.15
STOP_LOSS_MULTIPLIER = 0.50
MAX_HOLD_HOURS = 24.0
EARLY_PROFIT_EXIT_HOURS_BEFORE_MAX_HOLD = 12.0
COOLDOWN_HOURS = 4.0
MARKET_BAN_HOURS = 4.0
MIN_LIQUIDITY = 5000.0
MIN_VOLUME = 20000.0
MIN_DAYS_TO_EXPIRY = 2.0
CHECK_INTERVAL_SECONDS = 30
EXIT_RETRY_SECONDS = 10
EXIT_NO_ORDERBOOK_RETRY_SECONDS = 3600
EXIT_ORDER_VERSION_RETRY_SECONDS = 60
EXIT_ALLOWANCE_RETRY_SECONDS = 60
ENTRY_WINDOW_MINUTES = 10
BUY_COOLDOWN_MINUTES = 15
TX_CONFIRM_TIMEOUT_SECONDS = 90

# --- ИЗОЛИРОВАННЫЕ ФАЙЛЫ ДЛЯ PAPER TRADING ---
ENV_FILE = ".env"
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
DATA_API_URL = "https://data-api.polymarket.com"

LIVE_STATE_FILE = "paper_live_state.json"
LIVE_SYNC_FILE = "paper_live_sync.json"
LIVE_HISTORY_FILE = "paper_live_price_history.json"
PAPER_WALLET_FILE = "paper_wallet.json" # <--- Файл виртуального баланса
USDC_DECIMALS = 1_000_000

http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
})

telegram_state_lock = threading.Lock()
telegram_shared_state = None
telegram_shared_snapshot = {}
execution_lock = threading.Lock()
service_stop_event = threading.Event()
event_sports_cache = {}
sports_tag_ids_cache = None

# --- [ТУТ ОСТАЮТСЯ ВАШИ УТИЛИТЫ: send_telegram_message, log, load_json и т.д.] ---
def log(message, level="INFO", tg=False):
    prefix = "[PAPER] " if level == "INFO" else f"[PAPER {level}] "
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {prefix} {message}")
    if tg and TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS:
        admin_ids = [a.strip() for a in TELEGRAM_ADMIN_IDS.split(",") if a.strip()]
        for chat_id in admin_ids:
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                              json={"chat_id": chat_id, "text": f"<b>{prefix}</b>\n{message}", "parse_mode": "HTML"}, timeout=5)
            except: pass

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)

def safe_float(value, default=0.0):
    try: return float(value)
    except: return default

def fmt_usd(value): return f"${safe_float(value):.2f}"
def fmt_price(value): return f"${safe_float(value):.4f}"

def micro_to_usdc(value): return safe_float(value) / USDC_DECIMALS

def parse_token_ids(market):
    raw = market.get("clobTokenIds", "[]")
    if isinstance(raw, list): return [str(item) for item in raw]
    try: return [str(item) for item in json.loads(raw)]
    except: return []

def parse_outcome_prices(market):
    try: return [float(item) for item in json.loads(market.get("outcomePrices", "[]"))]
    except: return []

def parse_outcomes(market):
    raw = market.get("outcomes", "[]")
    if isinstance(raw, list): return [str(item) for item in raw]
    try: return [str(item) for item in json.loads(raw)]
    except: return []

def get_tick_size(market): return str(market.get("orderPriceMinTickSize") or market.get("priceTickSize") or market.get("minimumTickSize") or "0.01")
def round_price_to_tick(price, tick_size):
    tick = float(tick_size) if float(tick_size) > 0 else 0.01
    return max(round(round(price / tick) * tick, 6), tick)

def default_live_state():
    return {
        "service": "paper_live_service", "active_positions":[], "journal":[], "cooldowns": {}, "market_bans": {},
        "pending_exits": {}, "limit_orders": {}, "transaction": None,
        "stats": {"WON": 0, "TP": 0, "SAFE": 0, "SL": 0, "LOST": 0},
        "recovery": {"last_started_at": None, "last_completed_at": None, "last_status": None}, "last_cycle_at": None,
    }

def ensure_live_state_schema(state):
    base = default_live_state()
    for key, value in base.items():
        if key not in state: state[key] = value
    stats = state.setdefault("stats", {})
    for key in ("WON", "TP", "SAFE", "SL", "LOST"):
        stats.setdefault(key, 0)
    return state

def utc_now(): return datetime.now(timezone.utc)
def parse_datetime(value):
    if not value: return None
    try: parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except: return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def seconds_until_expiry(end_date):
    parsed = parse_datetime(end_date)
    return (parsed - utc_now()).total_seconds() if parsed else None

def normalize_text(value): return str(value or "").strip().lower()

def get_sports_tag_ids(): return set()
def event_is_sports(event): return False
def position_is_sports(position): return bool(position.get("is_sports_market"))

def admin_chat_ids():
    return {int(item.strip()) for item in TELEGRAM_ADMIN_IDS.split(",") if item.strip().isdigit()}

def is_admin_update(update):
    allowed = admin_chat_ids()
    return not allowed or (update.effective_chat and update.effective_chat.id in allowed)

def get_stats_counts(state):
    ensure_live_state_schema(state)
    return {key: int(state.get("stats", {}).get(key, 0)) for key in ("WON", "TP", "SAFE", "SL", "LOST")}

def classify_exit(reason, profit):
    normalized = normalize_text(reason)
    if "stop loss" in normalized:
        return "SL"
    if "dynamic tp" in normalized or normalized == "tp":
        return "TP"
    if "early profit" in normalized or "safe" in normalized:
        return "SAFE"
    if profit > 0:
        return "WON"
    if profit < 0:
        return "LOST"
    return "SAFE"

def record_closed_trade(state, exit_type, profit):
    ensure_live_state_schema(state)
    counts = state["stats"]
    counts[exit_type] = int(counts.get(exit_type, 0)) + 1
    state.setdefault("journal", []).append({
        "action": exit_type,
        "profit": profit,
        "closed_at": utc_now().isoformat(),
    })

def build_stats_message(state):
    state = ensure_live_state_schema(state or load_json(LIVE_STATE_FILE, default_live_state()))
    wallet = load_json(PAPER_WALLET_FILE, {"usdc": STARTING_BALANCE, "positions": {}})
    free_balance = safe_float(wallet.get("usdc"))
    total_assets = safe_float(state.get("portfolio_value"), free_balance)
    locked = 0.0
    net_profit = total_assets - STARTING_BALANCE
    counts = get_stats_counts(state)
    closed_count = sum(counts.values())
    active_count = len(state.get("active_positions", []))
    positive_closed = counts["WON"] + counts["TP"] + counts["SAFE"]
    win_rate = (positive_closed / closed_count * 100.0) if closed_count else 0.0

    return (
        f"💰 Free balance: {fmt_usd(free_balance)}\n"
        f"🔒 Locked: {fmt_usd(locked)}\n"
        f"💵 Total Assets: {fmt_usd(total_assets)}\n"
        f"📈 Net Profit: {fmt_usd(net_profit)}\n\n"
        f"🔄 Active: {active_count} | 📊 Closed: {closed_count}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n"
        f"✅WON:{counts['WON']} | 🤑TP:{counts['TP']} | 🛡️SAFE:{counts['SAFE']} | ⛔️SL:{counts['SL']} | ❌LOST:{counts['LOST']}"
    )

def build_exit_message(position, reason, shares, sell_price, proceeds, profit, state):
    avg_price = safe_float(position.get("avg_price"))
    cost = shares * avg_price
    profit_pct = (profit / cost * 100.0) if cost else 0.0
    question = escape(str(position.get("question", "Unknown market")))
    outcome = escape(str(position.get("outcome", "N/A")))
    total_assets = safe_float(state.get("portfolio_value"), safe_float(state.get("available_balance")))

    return (
        f"✅ Виртуальный экзит: {escape(str(reason))}\n"
        f"📌 {question}\n"
        f"🎯 Outcome: {outcome}\n"
        f"📦 Shares: {shares:.4f}\n"
        f"🛒 Buy: {fmt_price(avg_price)} | 💸 Sell: {fmt_price(sell_price)}\n"
        f"💵 Value: {fmt_usd(proceeds)}\n"
        f"📈 Profit: {fmt_usd(profit)} ({profit_pct:+.2f}%)\n\n"
        f"💰 Free balance: {fmt_usd(state.get('available_balance'))}\n"
        f"💵 Total Assets: {fmt_usd(total_assets)}\n"
        f"🔄 Open positions: {len(state.get('active_positions', []))}"
    )

def set_telegram_snapshot(state):
    global telegram_shared_state, telegram_shared_snapshot
    with telegram_state_lock:
        telegram_shared_state = json.loads(json.dumps(state))
        telegram_shared_snapshot = {
            "updated_at": utc_now().isoformat(),
            "stats": build_stats_message(state),
        }

async def telegram_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    keyboard = ReplyKeyboardMarkup([[TELEGRAM_MENU_BUTTON_STATS]], resize_keyboard=True)
    await update.effective_chat.send_message("Paper menu", reply_markup=keyboard)

async def telegram_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    with telegram_state_lock:
        message = telegram_shared_snapshot.get("stats") or build_stats_message(telegram_shared_state)
    await update.effective_chat.send_message(message)

async def telegram_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_admin_update(update):
        return
    text = normalize_text(update.message.text)
    if text == "menu":
        keyboard = ReplyKeyboardMarkup([[TELEGRAM_MENU_BUTTON_STATS]], resize_keyboard=True)
        await update.message.reply_text("Paper menu", reply_markup=keyboard)
        return
    if text in (normalize_text(TELEGRAM_MENU_BUTTON_STATS), "stats", "статистика"):
        await telegram_stats(update, context)

def run_telegram_bot():
    if not TELEGRAM_BOT_TOKEN:
        return
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("menu", telegram_menu))
        app.add_handler(CommandHandler("stats", telegram_stats))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text))
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False, stop_signals=None)
    except Exception as error:
        log(f"Telegram bot stopped: {error}", level="WARNING")
    finally:
        asyncio.set_event_loop(None)
        if loop is not None and not loop.is_closed():
            loop.close()

def start_telegram_bot_thread():
    if not TELEGRAM_BOT_TOKEN:
        return
    thread = threading.Thread(target=run_telegram_bot, name="paper-telegram", daemon=True)
    thread.start()

# --- ВИРТУАЛЬНЫЙ КЛИЕНТ POLYMARKET (СЕРДЦЕ PAPER TRADING) ---
class PaperPolymarketClient:
    def __init__(self):
        self.host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
        self.chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        self.funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        
        self.client = None
        self.wallet = load_json(PAPER_WALLET_FILE, {"usdc": STARTING_BALANCE, "positions": {}})

    def save_wallet(self):
        save_json(PAPER_WALLET_FILE, self.wallet)

    def initialize(self):
        log("Initializing VIRTUAL (Paper) ClobClient...")
        temp_client = ClobClient(host=self.host, key=self.private_key, chain_id=self.chain_id, funder=self.funder, signature_type=self.signature_type)
        api_creds = temp_client.create_or_derive_api_key()
        self.client = ClobClient(host=self.host, key=self.private_key, chain_id=self.chain_id, creds=api_creds, signature_type=self.signature_type, funder=self.funder)
        log("✅ Virtual Client ready. Balance: $%.2f" % self.wallet['usdc'])
        return api_creds

    def get_open_orders(self): return []
    def get_order_book(self, token_id): return self.client.get_order_book(str(token_id))

    def get_market_execution_price(self, token_id, side, amount):
        """ИСПОЛЬЗУЕТ РЕАЛЬНЫЙ СТАКАН ДЛЯ РАСЧЕТА ВИРТУАЛЬНОЙ ЦЕНЫ"""
        try:
            price = self.client.calculate_market_price(token_id=str(token_id), side=side, amount=float(amount), order_type=OrderType.FAK)
            return safe_float(price, default=None)
        except: return None

    def get_balance_allowance(self):
        return {"balance": int(self.wallet["usdc"] * USDC_DECIMALS)}

    def get_token_balance(self, token_id):
        return float(self.wallet["positions"].get(str(token_id), {}).get("shares", 0.0))

    def get_positions(self):
        """Генерирует фейковый ответ Data API на основе виртуального кошелька"""
        fake_positions = []
        for token_id, data in self.wallet["positions"].items():
            fake_positions.append({
                "asset": str(token_id),
                "size": str(data["shares"]),
                "avgPrice": str(data["avg_price"]),
                "initialValue": str(data["shares"] * data["avg_price"])
            })
        return fake_positions

    def get_total_value(self):
        val = self.wallet["usdc"]
        for token_id, data in self.wallet["positions"].items():
            cur_price = self.get_market_execution_price(token_id, Side.SELL, data["shares"]) or data["avg_price"]
            val += data["shares"] * cur_price
        return val

    # --- ВИРТУАЛЬНОЕ ИСПОЛНЕНИЕ ОРДЕРОВ ---
    def place_market_buy(self, token_id, usdc_amount, tick_size, max_price_slippage=None):
        # 1. Считаем реальную цену из стакана прямо сейчас
        actual_price = self.get_market_execution_price(token_id, Side.BUY, usdc_amount)
        if actual_price is None or actual_price <= 0:
            raise Exception("No orderbook/liquidity to simulate BUY")
            
        # 2. Проверяем Slippage защиту!
        if max_price_slippage and actual_price > max_price_slippage:
            raise Exception(f"Slippage exceeded: actual {actual_price:.4f} > max {max_price_slippage:.4f}")

        if self.wallet["usdc"] < usdc_amount:
            raise Exception("Not enough virtual USDC")

        shares_bought = usdc_amount / actual_price
        self.wallet["usdc"] -= usdc_amount
        
        token_id = str(token_id)
        if token_id not in self.wallet["positions"]:
            self.wallet["positions"][token_id] = {"shares": 0, "avg_price": 0, "invested": 0}
        
        pos = self.wallet["positions"][token_id]
        pos["shares"] += shares_bought
        pos["invested"] += usdc_amount
        pos["avg_price"] = pos["invested"] / pos["shares"]
        
        self.save_wallet()
        log(f"💸 [VIRTUAL BOUGHT] {shares_bought:.2f} shares @ ${actual_price:.4f}")
        return {"orderID": "virtual_buy_" + str(int(time.time()))}

    def place_market_sell(self, token_id, shares, tick_size="0.01", min_price_slippage=None):
        # 1. Считаем цену слива в реальный стакан
        actual_price = self.get_market_execution_price(token_id, Side.SELL, shares)
        if actual_price is None or actual_price <= 0:
            raise Exception("No orderbook/liquidity to simulate SELL")

        # 2. Проверяем Slippage защиту (чтобы не продать за 0.01)
        if min_price_slippage and actual_price < min_price_slippage:
            raise Exception(f"Slippage exceeded: actual {actual_price:.4f} < min {min_price_slippage:.4f}")

        token_id = str(token_id)
        if token_id not in self.wallet["positions"] or self.wallet["positions"][token_id]["shares"] < shares * 0.99:
            raise Exception("Not enough virtual shares")

        proceeds = shares * actual_price
        self.wallet["usdc"] += proceeds
        self.wallet["positions"][token_id]["shares"] -= shares
        
        if self.wallet["positions"][token_id]["shares"] <= 0.0001:
            del self.wallet["positions"][token_id]

        self.save_wallet()
        log(f"💰 [VIRTUAL SOLD] {shares:.2f} shares @ ${actual_price:.4f} (+${proceeds:.2f})")
        return {
            "orderID": "virtual_sell_" + str(int(time.time())),
            "price": actual_price,
            "shares": shares,
            "proceeds": proceeds,
        }

    def place_limit_sell(self, *args, **kwargs): return {"orderID": "mock_limit_123"}
    def cancel_order_by_id(self, order_id): return True


# --- [ТУТ ИДЕТ ВАШ ОСНОВНОЙ БЛОК ЛОГИКИ С ЗАЩИТАМИ] ---

def maintain_price_history(valid_events):
    history = load_json(LIVE_HISTORY_FILE, {})
    now_ts = int(time.time())
    cutoff_ts = now_ts - int(HISTORY_WINDOW_HOURS * 3600)
    candidates = []

    for event in valid_events:
        for market in event.get("markets", []):
            if market.get("closed"): continue
            outcomes = parse_outcomes(market)
            prices = parse_outcome_prices(market)
            token_ids = parse_token_ids(market)
            if len(prices) != len(token_ids): continue

            market_id = str(market["id"])
            market_history = history.setdefault(market_id, {})

            for outcome_index, price in enumerate(prices):
                outcome_key = str(outcome_index)
                outcome_history = [record for record in market_history.setdefault(outcome_key, []) if record[0] >= cutoff_ts]
                outcome_history.append((now_ts, price))
                market_history[outcome_key] = outcome_history

                if len(outcome_history) < 2 or not (MIN_PRICE <= price <= MAX_PRICE): continue
                
                max_recent_price = max(record[1] for record in outcome_history)
                if max_recent_price <= 0: continue

                drop_ratio = (max_recent_price - price) / max_recent_price
                if drop_ratio < DROP_PERCENT_REQUIRED: continue

                candidates.append({
                    "score": float(market.get("volume", 0)) + float(market.get("liquidity", 0)),
                    "event_id": event["id"], "market_id": market["id"],
                    "question": market.get("question", "Unknown"), "outcome": outcomes[outcome_index] if outcome_index < len(outcomes) else "N/A",
                    "outcome_index": outcome_index, "token_id": token_ids[outcome_index],
                    "current_price": price, "max_recent_price": max_recent_price, "tick_size": get_tick_size(market),
                    "end_date": event.get("endDate"), "is_sports_market": False
                })

    save_json(LIVE_HISTORY_FILE, history)
    return candidates

def collect_valid_events():
    valid_events = []
    offset = 0
    while offset < 500:
        response = http_session.get(GAMMA_API_URL, params={"closed": "false", "limit": 100, "offset": offset, "order": "volume", "ascending": "false"}, timeout=20)
        payload = response.json()
        if not payload: break

        for event in payload:
            if not event.get("endDate") or seconds_until_expiry(event.get("endDate") or "") is None or seconds_until_expiry(event.get("endDate") or "") < MIN_DAYS_TO_EXPIRY * 24 * 3600: continue
            
            eligible_markets = [m for m in event.get("markets", []) if not m.get("closed") and float(m.get("liquidity", 0)) >= MIN_LIQUIDITY and len(parse_token_ids(m)) >= 2]
            if eligible_markets:
                event["markets"] = eligible_markets
                valid_events.append(event)
        offset += 100
    return valid_events

# --- Упрощенные вспомогательные функции ---
def cleanup_cooldowns(state): pass # Упрощено для кода
def has_pending_exit(state, token_id=None): return str(token_id) in state.get("pending_exits", {})
def is_market_banned(state, market_id): return False
def has_open_position(state, token_id): return any(str(p.get("asset_id")) == str(token_id) for p in state.get("active_positions",[]))
def begin_transaction(state, action, token_id, market_id, extra=None): pass
def complete_transaction(state, status, error=None): pass
def journal_entry(state, action, payload): pass
def queue_pending_exit(state, position, reason, journal_action, sell_price, sync_snapshot=None):
    state.setdefault("pending_exits", {})[str(position["asset_id"])] = position
    state["pending_exits"][str(position["asset_id"])]["reason"] = reason
    state["pending_exits"][str(position["asset_id"])]["journal_action"] = journal_action
    state["pending_exits"][str(position["asset_id"])]["exit_signal_price"] = sell_price

# --- ВХОД (С ЗАЩИТОЙ) ---
def attempt_entries(execution_client, state, sync_snapshot):
    candidates = maintain_price_history(collect_valid_events())
    candidates.sort(key=lambda x: x["score"], reverse=True)
    
    for candidate in candidates:
        if has_open_position(state, candidate["token_id"]): continue
        
        # 1. Получаем реальную цену стакана
        entry_price = execution_client.get_market_execution_price(candidate["token_id"], Side.BUY, BET_AMOUNT)
        if not entry_price or entry_price > MAX_PRICE: continue
        
        # 2. ЗАЩИТА ОТ СПРЕДА
        gamma_price = candidate["current_price"]
        if gamma_price > 0 and (entry_price - gamma_price) / gamma_price > 0.05:
            log(f"⚠️ Пропуск: огромный спред. Gamma API: ${gamma_price:.3f}, CLOB: ${entry_price:.3f}", level="WARNING")
            continue

        target_price = entry_price + ((candidate["max_recent_price"] - entry_price) * RECOVERY_TARGET_PERCENT)
        if ((target_price - entry_price) / entry_price) < MIN_PROFIT_PERCENT: continue

        # 3. ЗАЩИТА ОТ ПРОСКАЛЬЗЫВАНИЯ
        acceptable_buy_price = round_price_to_tick(entry_price * 1.03, candidate["tick_size"])

        try:
            execution_client.place_market_buy(candidate["token_id"], BET_AMOUNT, candidate["tick_size"], max_price_slippage=acceptable_buy_price)
            
            position = candidate.copy()
            position["asset_id"] = candidate["token_id"]
            position["shares"] = BET_AMOUNT / entry_price
            position["avg_price"] = entry_price
            position["target_price"] = target_price
            position["opened_at"] = utc_now().isoformat()
            
            state.setdefault("active_positions", []).append(position)
            save_json(LIVE_STATE_FILE, state)
            break # 1 вход за цикл
        except Exception as e:
            log(f"Virtual BUY failed: {e}", level="WARNING")

# --- ВЫХОД ИЗ ПЕНДИНГА (С ЗАЩИТОЙ) ---
def attempt_pending_exits(execution_client, state):
    exits = state.get("pending_exits", {})
    for asset_id, pending in list(exits.items()):
        
        # 1. Чекаем реальный стакан
        shares = pending["shares"]
        exec_price = execution_client.get_market_execution_price(asset_id, Side.SELL, shares)
        
        # 2. Защита от пустого стакана
        if exec_price is None or exec_price <= 0.01:
            log(f"⚠️ Отмена виртуального выхода (стакан пуст) для {asset_id}. Ждем.", level="WARNING")
            continue
            
        sell_price = round_price_to_tick(exec_price, pending.get("tick_size", "0.01"))
        
        # 3. Защита проскальзывания вниз (-5%)
        min_sell = max(round_price_to_tick(sell_price * 0.95, pending.get("tick_size", "0.01")), 0.02)
        
        try:
            sell_result = execution_client.place_market_sell(asset_id, shares, pending.get("tick_size", "0.01"), min_price_slippage=min_sell)
            actual_sell_price = safe_float(sell_result.get("price"), exec_price)
            proceeds = safe_float(sell_result.get("proceeds"), shares * actual_sell_price)
            cost = shares * safe_float(pending.get("avg_price"))
            profit = proceeds - cost
            exit_type = classify_exit(pending.get("reason"), profit)
            
            # Удаляем позицию из активных и пендингов
            state["active_positions"] = [p for p in state["active_positions"] if str(p["asset_id"]) != asset_id]
            del exits[asset_id]
            record_closed_trade(state, exit_type, profit)
            sync_exchange(execution_client, state)
            save_json(LIVE_STATE_FILE, state)
            log(build_exit_message(pending, pending["reason"], shares, actual_sell_price, proceeds, profit, state), tg=True)
            
        except Exception as e:
            log(f"Virtual SELL failed: {e}", level="WARNING")

def attempt_exits(execution_client, state):
    now = utc_now()
    for position in state.get("active_positions",[]):
        if has_pending_exit(state, position.get("asset_id")): continue

        avg_price = float(position.get("avg_price", 0.0))
        exec_price = execution_client.get_market_execution_price(position["asset_id"], Side.SELL, position["shares"])
        current_price = exec_price if exec_price else avg_price
        
        opened_at = parse_datetime(position.get("opened_at")) or now
        hours_held = (now - opened_at).total_seconds() / 3600.0
        
        if current_price >= position.get("target_price", 99):
            queue_pending_exit(state, position, "DYNAMIC TP", "TP", current_price)
        elif hours_held >= EARLY_PROFIT_EXIT_HOURS_BEFORE_MAX_HOLD and current_price > avg_price:
            queue_pending_exit(state, position, "EARLY PROFIT", "EP", current_price)
        elif current_price <= avg_price * STOP_LOSS_MULTIPLIER:
            queue_pending_exit(state, position, "STOP LOSS", "SL", current_price)
        elif hours_held >= MAX_HOLD_HOURS:
            queue_pending_exit(state, position, "TIME STOP", "TIME", current_price)

def sync_exchange(client, state):
    state["portfolio_value"] = client.get_total_value()
    state["available_balance"] = client.get_balance_allowance()["balance"] / USDC_DECIMALS
    return state

def main():
    log("=== VIRTUAL PAPER TRADING STARTED ===", tg=True)
    state = ensure_live_state_schema(load_json(LIVE_STATE_FILE, default_live_state()))
    execution_client = PaperPolymarketClient()
    execution_client.initialize()
    start_telegram_bot_thread()

    while True:
        try:
            sync_exchange(execution_client, state)
            set_telegram_snapshot(state)
            attempt_pending_exits(execution_client, state)
            attempt_exits(execution_client, state)
            attempt_entries(execution_client, state, state)
            sync_exchange(execution_client, state)
            set_telegram_snapshot(state)
            save_json(LIVE_STATE_FILE, state)
            
            log(f"Cycle completed. Virtual Balance: ${execution_client.wallet['usdc']:.2f} | Open Positions: {len(state['active_positions'])}")
            time.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            log(f"Error in main loop: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
