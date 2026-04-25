import json
import os
import sys
import asyncio
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# --- Чистые импорты V2 (py-clob-client-v2) ---
from py_clob_client_v2 import (
    ClobClient,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side
)
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams, OrderPayload
from py_clob_client_v2.exceptions import PolyApiException

# Загружаем переменные окружения здесь, чтобы были доступны токены Telegram
load_dotenv(".env")

# --- Telegram settings ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
TELEGRAM_MENU_BUTTON_STATS = "Статистика"
STRATEGY_DISPLAY_NAME = os.getenv("STRATEGY_DISPLAY_NAME", "Balanced Log Flow v1").strip() or "Balanced Log Flow v1"
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))
TELEGRAM_MENU_BUTTON_STOP = "stop"

# --- Strategy settings ---
BET_AMOUNT = 2.5
MIN_PRICE = 0.08
MAX_PRICE = 0.25
HISTORY_WINDOW_HOURS = 2.0
DROP_PERCENT_REQUIRED = 0.10
RECOVERY_TARGET_PERCENT = 0.50
MIN_PROFIT_PERCENT = 0.15
STOP_LOSS_MULTIPLIER = 0.50
MAX_HOLD_HOURS = 24.0
COOLDOWN_HOURS = 4.0
MARKET_BAN_HOURS = 4.0
MIN_LIQUIDITY = 5000.0
MIN_VOLUME = 20000.0
MIN_DAYS_TO_EXPIRY = 2.0
CHECK_INTERVAL_SECONDS = 30
EXIT_RETRY_SECONDS = 10
ENTRY_WINDOW_MINUTES = 10
BUY_COOLDOWN_MINUTES = 15
TX_CONFIRM_TIMEOUT_SECONDS = 90
LIMIT_ORDER_RETRY_COUNT = 3
LIMIT_ORDER_RETRY_DELAY_SECONDS = 3
LIMIT_ORDER_EXPIRY_BUFFER_SECONDS = 300

ENV_FILE = ".env"
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
DATA_API_URL = "https://data-api.polymarket.com"

LIVE_STATE_FILE = "satt_live_state.json"
LIVE_SYNC_FILE = "satt_live_sync.json"
LIVE_HISTORY_FILE = "satt_live_price_history.json"
USDC_DECIMALS = 1_000_000

http_session = requests.Session()
http_session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
)

telegram_state_lock = threading.Lock()
telegram_shared_state = None
telegram_shared_snapshot = {}
execution_lock = threading.Lock()
service_stop_event = threading.Event()
event_sports_cache = {}
sports_tag_ids_cache = None

def send_telegram_message(text):
    """Функция для отправки уведомлений списку администраторов в Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_IDS:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Разбиваем строку с ID по запятой и убираем лишние пробелы
    admin_ids =[admin_id.strip() for admin_id in TELEGRAM_ADMIN_IDS.split(",") if admin_id.strip()]
    
    for chat_id in admin_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"⚠️ Ошибка отправки в Telegram пользователю {chat_id}: {e}")

def get_admin_ids():
    return [admin_id.strip() for admin_id in TELEGRAM_ADMIN_IDS.split(",") if admin_id.strip()]

def build_reply_keyboard():
    return ReplyKeyboardMarkup(
        [[TELEGRAM_MENU_BUTTON_STATS]],
        resize_keyboard=True,
        is_persistent=True,
    )

def send_telegram_chat_message(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as error:
        print(f"⚠️ Ошибка отправки сообщения в Telegram {chat_id}: {error}")

def log(message, level="INFO", tg=False):
    """tg=True отправит это сообщение еще и всем админам в Telegram"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {message}")
    if tg:
        send_telegram_message(f"<b>[{level}]</b>\n{message}")

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def micro_to_usdc(value):
    return safe_float(value) / USDC_DECIMALS

def parse_token_ids(market):
    raw = market.get("clobTokenIds", "[]")
    if isinstance(raw, list):
        return[str(item) for item in raw]
    try:
        data = json.loads(raw)
        return[str(item) for item in data]
    except Exception:
        return[]

def parse_outcome_prices(market):
    try:
        return[float(item) for item in json.loads(market.get("outcomePrices", "[]"))]
    except Exception:
        return[]

def get_tick_size(market):
    raw = str(
        market.get("orderPriceMinTickSize")
        or market.get("priceTickSize")
        or market.get("minimumTickSize")
        or "0.01"
    )
    return raw

def round_price_to_tick(price, tick_size):
    try:
        tick = float(tick_size)
    except Exception:
        tick = 0.01

    if tick <= 0:
        return round(price, 4)
    rounded = round(round(price / tick) * tick, 6)
    return max(rounded, tick)

def default_live_state():
    return {
        "service": "satt_live_service",
        "active_positions":[],
        "journal":[],
        "cooldowns": {},
        "market_bans": {},
        "pending_exits": {},
        "limit_orders": {},
        "transaction": None,
        "recovery": {
            "last_started_at": None,
            "last_completed_at": None,
            "last_status": None,
        },
        "last_cycle_at": None,
    }

def ensure_live_state_schema(state):
    base = default_live_state()
    for key, value in base.items():
        if key not in state:
            state[key] = value
    if not isinstance(state.get("cooldowns"), dict):
        state["cooldowns"] = {}
    if not isinstance(state.get("market_bans"), dict):
        state["market_bans"] = {}
    if not isinstance(state.get("pending_exits"), dict):
        state["pending_exits"] = {}
    if not isinstance(state.get("limit_orders"), dict):
        state["limit_orders"] = {}
    if not isinstance(state.get("journal"), list):
        state["journal"] = []
    if not isinstance(state.get("active_positions"), list):
        state["active_positions"] = []
    if not isinstance(state.get("recovery"), dict):
        state["recovery"] = base["recovery"]
    return state

def utc_now():
    return datetime.now(timezone.utc)

def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def seconds_until_expiry(end_date):
    parsed = parse_datetime(end_date)
    if parsed is None:
        return None
    return (parsed - utc_now()).total_seconds()

def normalize_text(value):
    return str(value or "").strip().lower()

def get_sports_tag_ids():
    global sports_tag_ids_cache
    if sports_tag_ids_cache is not None:
        return sports_tag_ids_cache

    sports_tag_ids = set()
    try:
        response = http_session.get(f"{GAMMA_API_URL.rsplit('/', 1)[0]}/sports", timeout=20)
        response.raise_for_status()
        payload = response.json() or []
        for item in payload:
            raw_tags = str(item.get("tags") or "")
            for tag_id in raw_tags.split(","):
                tag_id = tag_id.strip()
                if tag_id:
                    sports_tag_ids.add(tag_id)
    except Exception as error:
        log(f"Failed to load sports tag metadata: {error}", level="WARNING")

    sports_tag_ids_cache = sports_tag_ids
    return sports_tag_ids_cache

def event_is_sports(event):
    event_id = str(event.get("id") or "")
    if event_id in event_sports_cache:
        return event_sports_cache[event_id]

    category = normalize_text(event.get("category"))
    subcategory = normalize_text(event.get("subcategory"))
    if category == "sports" or subcategory == "sports":
        event_sports_cache[event_id] = True
        return True

    sports_tag_ids = get_sports_tag_ids()
    if sports_tag_ids and event_id:
        try:
            response = http_session.get(
                f"{GAMMA_API_URL.rsplit('/', 1)[0]}/events/{event_id}/tags",
                timeout=20,
            )
            response.raise_for_status()
            tags = response.json() or []
            for tag in tags:
                tag_id = str(tag.get("id") or "").strip()
                slug = normalize_text(tag.get("slug"))
                label = normalize_text(tag.get("label"))
                if tag_id in sports_tag_ids or slug == "sports" or label == "sports":
                    event_sports_cache[event_id] = True
                    return True
        except Exception as error:
            log(f"Failed to load tags for event {event_id}: {error}", level="WARNING")

    event_sports_cache[event_id] = False
    return False

def position_is_sports(position):
    return bool(position.get("is_sports_market"))

def update_telegram_runtime_state(state, sync_snapshot):
    global telegram_shared_state, telegram_shared_snapshot
    with telegram_state_lock:
        telegram_shared_state = json.loads(json.dumps(state))
        telegram_shared_snapshot = json.loads(json.dumps(sync_snapshot))

def get_telegram_runtime_state():
    with telegram_state_lock:
        state = json.loads(json.dumps(telegram_shared_state or default_live_state()))
        snapshot = json.loads(json.dumps(telegram_shared_snapshot or {}))
    return state, snapshot

def is_telegram_admin(chat_id):
    return str(chat_id) in set(get_admin_ids())

async def telegram_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not is_telegram_admin(update.effective_chat.id):
        return
    await update.effective_chat.send_message(
        "Меню открыто. Используй кнопку «Статистика» ниже.",
        reply_markup=build_reply_keyboard(),
    )

async def telegram_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not is_telegram_admin(update.effective_chat.id):
        return
    state, sync_snapshot = get_telegram_runtime_state()
    await update.effective_chat.send_message(format_statistics_message(state, sync_snapshot))

async def telegram_stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not is_telegram_admin(update.effective_chat.id):
        return
    service_stop_event.set()
    await update.effective_chat.send_message("Stopping bot loop.")

async def telegram_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_message:
        return
    if not is_telegram_admin(update.effective_chat.id):
        return

    text = (update.effective_message.text or "").strip().lower()
    if text == "menu":
        await telegram_menu_handler(update, context)
    elif text == TELEGRAM_MENU_BUTTON_STATS.lower():
        await telegram_stats_handler(update, context)
    elif text == TELEGRAM_MENU_BUTTON_STOP.lower():
        await telegram_stop_handler(update, context)

def start_telegram_bot():
    if not TELEGRAM_BOT_TOKEN:
        return None

    def run_bot():
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
            application.add_handler(CommandHandler("menu", telegram_menu_handler))
            application.add_handler(CommandHandler("stats", telegram_stats_handler))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text_handler))
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False,
                stop_signals=None,
            )
        except Exception as error:
            log(f"Telegram bot stopped: {error}", level="ERROR", tg=True)
        finally:
            asyncio.set_event_loop(None)
            if loop is not None and not loop.is_closed():
                loop.close()

    bot_thread = threading.Thread(target=run_bot, name="telegram-bot", daemon=True)
    bot_thread.start()
    return bot_thread

# --- ИНТЕГРАЦИЯ С POLYMARKET V2 ---
class PolymarketExecutionClient:
    def __init__(self):
        self.host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
        self.chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        self.funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")) 
        
        self.profile_address = self.funder
        self.client = None

    def initialize(self):
        if not self.private_key or not self.funder:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS are required in .env")

        log(f"Initializing V2 ClobClient... (Signature Type: {self.signature_type})")
        
        temp_client = ClobClient(
            host=self.host, 
            key=self.private_key, 
            chain_id=self.chain_id,
            funder=self.funder,
            signature_type=self.signature_type
        )
        api_creds = temp_client.create_or_derive_api_key()
        
        self.client = ClobClient(
            host=self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            creds=api_creds,
            signature_type=self.signature_type,
            funder=self.funder,
        )
        log("✅ Polymarket V2 API Credentials successfully derived and loaded.")
        return api_creds

    def get_open_orders(self):
        try:
            if hasattr(self.client, 'get_orders'):
                return self.client.get_orders() or[]
            elif hasattr(self.client, 'get_open_orders'):
                return self.client.get_open_orders() or[]
            return[]
        except Exception:
            return[]

    def get_order_book(self, token_id):
        try:
            return self.client.get_order_book(str(token_id))
        except Exception as error:
            log(f"Failed to load order book for {token_id}: {error}", level="WARNING")
            return None

    def get_market_execution_price(self, token_id, side, amount):
        try:
            price = self.client.calculate_market_price(
                token_id=str(token_id),
                side=side,
                amount=float(amount),
                order_type=OrderType.FAK,
            )
            return safe_float(price, default=None)
        except Exception as error:
            log(
                f"Failed to calculate market execution price for {token_id} {side} {amount}: {error}",
                level="WARNING",
            )
            return None

    def get_balance_allowance(self):
        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return result
        except Exception as error:
            log(f"Failed to load collateral balance/allowance: {error}", level="WARNING")
            return None

    def get_token_balance(self, token_id):
        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                    signature_type=self.signature_type,
                )
            )
            return micro_to_usdc((result or {}).get("balance"))
        except Exception as error:
            log(f"Failed to load conditional balance for {token_id}: {error}", level="WARNING")
            return None

    def get_positions(self):
        if not self.profile_address:
            return[]
        try:
            response = http_session.get(
                f"{DATA_API_URL}/positions",
                params={"user": self.profile_address},
                timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:
            log(f"Failed to load positions from Data API: {error}", level="WARNING")
            return[]

    def get_total_value(self):
        if not self.profile_address:
            return None
        try:
            response = http_session.get(
                f"{DATA_API_URL}/value",
                params={"user": self.profile_address},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list) and payload:
                return payload[0].get("value")
            if isinstance(payload, dict):
                return payload.get("value")
        except Exception as error:
            log(f"Failed to load portfolio value: {error}", level="WARNING")
        return None

    def place_market_buy(self, token_id, usdc_amount, tick_size):
        try:
            response = self.client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=str(token_id), 
                    amount=float(usdc_amount), 
                    side=Side.BUY, 
                    order_type=OrderType.FAK,
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick_size)),
                order_type=OrderType.FAK
            )
            return response
        except Exception as e:
            raise e

    def place_market_sell(self, token_id, shares, tick_size="0.01"):
        try:
            response = self.client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=str(token_id),
                    amount=float(shares),
                    side=Side.SELL,
                    order_type=OrderType.FAK,
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick_size)),
                order_type=OrderType.FAK,
            )
            return response
        except Exception as e:
            raise e

    def place_limit_sell(self, token_id, shares, price, tick_size="0.01", expiration=None):
        try:
            response = self.client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=str(token_id),
                    price=float(price),
                    size=float(shares),
                    side=Side.SELL,
                    expiration=int(expiration or 0),
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick_size)),
                order_type=OrderType.GTC if not expiration else OrderType.GTD,
            )
            return response
        except Exception as e:
            raise e

    def cancel_order_by_id(self, order_id):
        return self.client.cancel_order(OrderPayload(orderID=str(order_id)))

# --- ЛОГИКА СТРАТЕГИИ ---

def maintain_price_history(valid_events):
    history = load_json(LIVE_HISTORY_FILE, {})
    now_ts = int(time.time())
    cutoff_ts = now_ts - int(HISTORY_WINDOW_HOURS * 3600)
    candidates = []
    stats = {"events": 0, "markets": 0, "outcomes": 0, "too_few_history": 0, "price_out_of_range": 0, "no_drop": 0, "no_profit": 0, "candidates": 0}

    for event in valid_events:
        stats["events"] += 1
        for market in event.get("markets", []):
            stats["markets"] += 1
            if market.get("closed"):
                continue

            outcomes = market.get("outcomes", [])
            prices = parse_outcome_prices(market)
            token_ids = parse_token_ids(market)
            if len(prices) != len(token_ids):
                continue

            market_id = str(market["id"])
            market_history = history.setdefault(market_id, {})

            for outcome_index, price in enumerate(prices):
                stats["outcomes"] += 1
                outcome_key = str(outcome_index)
                outcome_history = market_history.setdefault(outcome_key, [])
                outcome_history = [record for record in outcome_history if record[0] >= cutoff_ts]
                outcome_history.append((now_ts, price))
                market_history[outcome_key] = outcome_history

                if len(outcome_history) < 2:
                    stats["too_few_history"] += 1
                    continue
                if not (MIN_PRICE <= price <= MAX_PRICE):
                    stats["price_out_of_range"] += 1
                    continue

                max_recent_price = max(record[1] for record in outcome_history)
                if max_recent_price <= 0:
                    continue

                drop_ratio = (max_recent_price - price) / max_recent_price
                if drop_ratio < DROP_PERCENT_REQUIRED:
                    stats["no_drop"] += 1
                    continue

                target_price = price + ((max_recent_price - price) * RECOVERY_TARGET_PERCENT)
                expected_profit_pct = (target_price - price) / price
                if expected_profit_pct < MIN_PROFIT_PERCENT:
                    stats["no_profit"] += 1
                    continue

                score = float(market.get("volume", 0)) + float(market.get("liquidity", 0))
                stats["candidates"] += 1
                candidates.append(
                    {
                        "score": score,
                        "event_id": event["id"],
                        "market_id": market["id"],
                        "question": market.get("question", "Unknown market"),
                        "outcome": outcomes[outcome_index] if outcome_index < len(outcomes) else f"Outcome {outcome_index}",
                        "outcome_index": outcome_index,
                        "token_id": token_ids[outcome_index],
                        "current_price": price,
                        "max_recent_price": max_recent_price,
                        "target_price": target_price,
                        "expected_profit_pct": expected_profit_pct * 100.0,
                        "tick_size": get_tick_size(market),
                        "end_date": event.get("endDate"),
                        "is_sports_market": bool(event.get("is_sports_market")),
                    }
                )

    save_json(LIVE_HISTORY_FILE, history)
    log(
        f"[maintain_price_history] events={stats['events']} markets={stats['markets']} outcomes={stats['outcomes']} "
        f"too_few_hist={stats['too_few_history']} price_range={stats['price_out_of_range']} no_drop={stats['no_drop']} "
        f"no_profit={stats['no_profit']} candidates={stats['candidates']}"
    )
    return candidates

def collect_valid_events():
    valid_events = []
    limit = 100
    offset = 0
    stats = {
        "total_events": 0,
        "no_end_date": 0,
        "expired": 0,
        "too_soon": 0,
        "low_volume": 0,
        "no_eligible_markets": 0,
        "accepted": 0,
        "eligible_markets": 0,
    }

    while offset < 500:
        response = http_session.get(
            GAMMA_API_URL,
            params={
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            break

        for event in payload:
            stats["total_events"] += 1
            end_date_str = event.get("endDate")
            if not end_date_str:
                stats["no_end_date"] += 1
                continue

            seconds_left = seconds_until_expiry(end_date_str)
            if seconds_left is None or seconds_left <= 0:
                stats["expired"] += 1
                continue
            if seconds_left < MIN_DAYS_TO_EXPIRY * 24 * 3600:
                stats["too_soon"] += 1
                continue
            if float(event.get("volume", 0)) < MIN_VOLUME:
                stats["low_volume"] += 1
                continue
            event["is_sports_market"] = event_is_sports(event)

            eligible_markets = []
            for market in event.get("markets", []):
                if market.get("closed"):
                    continue
                if float(market.get("liquidity", 0)) < MIN_LIQUIDITY:
                    continue
                if len(parse_token_ids(market)) < 2:
                    continue
                eligible_markets.append(market)

            if eligible_markets:
                event["markets"] = eligible_markets
                valid_events.append(event)
                stats["accepted"] += 1
                stats["eligible_markets"] += len(eligible_markets)
            else:
                stats["no_eligible_markets"] += 1

        offset += limit
        time.sleep(0.2)

    log(
        f"[collect_valid_events] total={stats['total_events']} accepted={stats['accepted']} "
        f"no_end_date={stats['no_end_date']} expired={stats['expired']} too_soon={stats['too_soon']} "
        f"low_volume={stats['low_volume']} no_markets={stats['no_eligible_markets']} markets={stats['eligible_markets']}"
    )
    return valid_events

def cleanup_cooldowns(state):
    now_ts = utc_now().timestamp()
    state["cooldowns"] = {
        market_id: ts
        for market_id, ts in state.get("cooldowns", {}).items()
        if ts > now_ts
    }
    state["market_bans"] = {
        market_id: ts
        for market_id, ts in state.get("market_bans", {}).items()
        if ts > now_ts
    }
    limit_orders = {}
    for asset_id, order in state.get("limit_orders", {}).items():
        expires_at = parse_datetime(order.get("expires_at"))
        if expires_at is None or expires_at > utc_now():
            limit_orders[str(asset_id)] = order
    state["limit_orders"] = limit_orders
    tx = state.get("transaction")
    if tx:
        started_at = parse_datetime(tx.get("started_at"))
        if started_at is None or (utc_now() - started_at).total_seconds() > TX_CONFIRM_TIMEOUT_SECONDS:
            state["transaction"] = None

def get_active_position(state, asset_id):
    asset_id = str(asset_id)
    for position in state.get("active_positions", []):
        if str(position.get("asset_id")) == asset_id:
            return position
    return None

def has_pending_exit(state, token_id=None, market_id=None):
    pending_exits = state.get("pending_exits", {})
    for pending in pending_exits.values():
        if token_id is not None and str(pending.get("asset_id")) == str(token_id):
            return True
        if market_id is not None and str(pending.get("market_id") or "") == str(market_id):
            return True
    return False

def is_market_banned(state, market_id):
    ban_until = state.get("market_bans", {}).get(str(market_id))
    if not ban_until:
        return False
    return ban_until > utc_now().timestamp()

def ban_market(state, market_id, hours=MARKET_BAN_HOURS):
    if not market_id:
        return None
    ban_until = (utc_now() + timedelta(hours=hours)).timestamp()
    state.setdefault("market_bans", {})[str(market_id)] = ban_until
    return ban_until

def get_limit_order(state, asset_id):
    return state.get("limit_orders", {}).get(str(asset_id))

def set_limit_order(state, asset_id, payload):
    state.setdefault("limit_orders", {})[str(asset_id)] = payload

def clear_limit_order(state, asset_id):
    state.setdefault("limit_orders", {}).pop(str(asset_id), None)

def has_limit_order(sync_snapshot, token_id):
    token_id = str(token_id)
    for order in sync_snapshot.get("open_orders", []):
        asset_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or "")
        side = str(order.get("side") or "").upper()
        if asset_id == token_id and side == "SELL":
            return True
    return False

def begin_transaction(state, action, token_id=None, market_id=None, extra=None):
    metadata = {
        "action": action,
        "token_id": str(token_id) if token_id is not None else None,
        "market_id": str(market_id) if market_id is not None else None,
        "started_at": utc_now().isoformat(),
        "status": "pending",
    }
    if extra:
        metadata.update(extra)
    state["transaction"] = metadata
    save_json(LIVE_STATE_FILE, state)
    return metadata

def complete_transaction(state, status, error=None):
    tx = state.get("transaction")
    if tx:
        tx["status"] = status
        tx["finished_at"] = utc_now().isoformat()
        if error:
            tx["error"] = str(error)
    if status in {"confirmed", "failed", "timed_out"}:
        state["transaction"] = None
    else:
        state["transaction"] = tx
    save_json(LIVE_STATE_FILE, state)

def transaction_in_flight(state):
    tx = state.get("transaction")
    if not tx:
        return False
    started_at = parse_datetime(tx.get("started_at"))
    if started_at is None:
        return False
    if (utc_now() - started_at).total_seconds() > TX_CONFIRM_TIMEOUT_SECONDS:
        complete_transaction(state, "timed_out", error="Transaction confirmation timeout")
        return False
    return tx.get("status") in {"pending", "submitted"}

def await_position_sync(execution_client, state, token_id, should_exist, timeout_seconds=TX_CONFIRM_TIMEOUT_SECONDS):
    deadline = time.time() + timeout_seconds
    last_snapshot = None
    while time.time() < deadline:
        last_snapshot = sync_exchange(execution_client, state)
        position_exists = has_open_position(state, token_id)
        if position_exists == should_exist:
            return True, last_snapshot
        time.sleep(3)
    return False, last_snapshot

def resolve_order_id(response):
    if isinstance(response, dict):
        for key in ("orderID", "id", "orderId"):
            if response.get(key):
                return str(response[key])
    return None

def response_has_no_orderbook(error):
    if not isinstance(error, PolyApiException):
        return False
    text = str(error).lower()
    return error.status_code == 404 or "no orderbook" in text or "no match" in text

def queue_pending_exit(state, position, reason, journal_action, sell_price, sync_snapshot=None):
    asset_id = str(position.get("asset_id"))
    pending_exits = state.setdefault("pending_exits", {})
    pending = pending_exits.get(asset_id)
    if pending:
        return pending

    pending = {
        "asset_id": asset_id,
        "market_id": position.get("market_id"),
        "question": position.get("question"),
        "outcome": position.get("outcome"),
        "is_sports_market": bool(position.get("is_sports_market")),
        "reason": reason,
        "journal_action": journal_action,
        "sell_price": sell_price,
        "first_detected_at": datetime.now(timezone.utc).isoformat(),
        "last_attempt_ts": 0.0,
        "attempts": 0,
        "entry_price": safe_float(position.get("avg_price", position.get("buy_price", 0.0))),
        "entry_cost": safe_float(position.get("cost")),
        "initial_shares": safe_float(position.get("shares")),
        "last_known_shares": safe_float(position.get("shares")),
        "available_balance_before_exit": (
            safe_float(sync_snapshot.get("available_balance"), default=None)
            if sync_snapshot is not None and sync_snapshot.get("available_balance") is not None
            else None
        ),
        "completion_recorded": False,
    }
    pending_exits[asset_id] = pending
    save_json(LIVE_STATE_FILE, state)
    return pending

def finalize_completed_pending_exits(state, sync_snapshot=None):
    pending_exits = state.setdefault("pending_exits", {})
    active_asset_ids = {str(position.get("asset_id")) for position in state.get("active_positions", [])}

    for asset_id in list(pending_exits.keys()):
        pending = pending_exits[asset_id]
        if asset_id in active_asset_ids:
            continue

        clear_limit_order(state, asset_id)
        if not pending.get("completion_recorded"):
            available_balance_after = (
                safe_float(sync_snapshot.get("available_balance"), default=None)
                if sync_snapshot is not None and sync_snapshot.get("available_balance") is not None
                else None
            )
            available_balance_before = pending.get("available_balance_before_exit")
            realized_proceeds = None
            if available_balance_before is not None and available_balance_after is not None:
                realized_proceeds = available_balance_after - available_balance_before

            entry_cost = safe_float(pending.get("entry_cost"))
            initial_shares = safe_float(pending.get("initial_shares"))
            actual_sell_price = None
            if realized_proceeds is not None and initial_shares > 0:
                actual_sell_price = realized_proceeds / initial_shares
            if actual_sell_price is None or actual_sell_price <= 0:
                actual_sell_price = safe_float(pending.get("sell_price"), default=None)

            pnl_usdc = None
            pnl_pct = None
            if realized_proceeds is not None and entry_cost > 0:
                pnl_usdc = realized_proceeds - entry_cost
                pnl_pct = (pnl_usdc / entry_cost) * 100.0
                if (
                    pnl_usdc < 0
                    and pending.get("journal_action") == "SELL_STOP_LOSS"
                    and position_is_sports(pending)
                ):
                    ban_market(state, pending.get("market_id"), hours=MARKET_BAN_HOURS)

            journal_entry(
                state,
                pending.get("journal_action", "SELL_COMPLETED"),
                {
                    "asset_id": asset_id,
                    "question": pending.get("question"),
                    "outcome": pending.get("outcome"),
                    "entry_price": pending.get("entry_price"),
                    "entry_cost": entry_cost,
                    "initial_shares": initial_shares,
                    "sell_price": actual_sell_price,
                    "estimated_sell_price": pending.get("sell_price"),
                    "realized_proceeds": realized_proceeds,
                    "pnl_usdc": pnl_usdc,
                    "pnl_pct": pnl_pct,
                    "reason": pending.get("reason"),
                    "attempts": pending.get("attempts", 0),
                    "completed_via": "pending_exit",
                },
            )
            pending["completion_recorded"] = True
            complete_transaction(state, "confirmed")
            summary = [
                f"✅ EXIT COMPLETED after {pending.get('attempts', 0)} attempt(s)",
                f"Buy: ${safe_float(pending.get('entry_price')):.3f} | Sell: ${safe_float(actual_sell_price):.3f}",
            ]
            if pnl_usdc is not None and pnl_pct is not None:
                summary.append(f"PnL: {pnl_usdc:+.3f} USDC ({pnl_pct:+.2f}%)")
            summary.append(f"Market: <i>{str(pending.get('question', 'Unknown market'))[:80]}</i>")
            log("\n".join(summary), tg=True)

        pending_exits.pop(asset_id, None)

    save_json(LIVE_STATE_FILE, state)

def attempt_pending_exits(execution_client, state, force_asset_id=None):
    if transaction_in_flight(state):
        return False

    pending_exits = state.get("pending_exits", {})
    if not pending_exits:
        return False

    now_ts = time.time()
    did_trade = False
    asset_ids = [str(force_asset_id)] if force_asset_id else list(pending_exits.keys())

    for asset_id in asset_ids:
        pending = pending_exits.get(str(asset_id))
        if not pending:
            continue

        if not force_asset_id and now_ts - safe_float(pending.get("last_attempt_ts")) < EXIT_RETRY_SECONDS:
            continue

        position = get_active_position(state, asset_id)
        if not position:
            continue

        shares = safe_float(position.get("shares"))
        if shares <= 0:
            continue

        sell_price = round_price_to_tick(
            safe_float(position.get("current_price", position.get("avg_price", 0.0))),
            position.get("tick_size", "0.01"),
        )
        pending["sell_price"] = sell_price
        pending["last_known_shares"] = shares
        pending["last_attempt_ts"] = now_ts
        pending["attempts"] = int(pending.get("attempts", 0)) + 1

        acquired = execution_lock.acquire(timeout=1)
        if not acquired:
            log("Skipping exit attempt: another transaction is still being processed.", level="WARNING")
            continue

        try:
            begin_transaction(
                state,
                "SELL",
                token_id=position["asset_id"],
                market_id=position.get("market_id"),
                extra={"reason": pending.get("reason", "")[:120]},
            )
            cancel_tracked_limit_order(execution_client, state, asset_id)
            response = execution_client.place_market_sell(
                token_id=position["asset_id"],
                shares=shares,
                tick_size=position.get("tick_size", "0.01"),
            )
            pending["last_response"] = str(response)
            state["pending_exits"][str(asset_id)] = pending
            journal_entry(
                state,
                "SELL_ATTEMPT",
                {
                    "asset_id": position["asset_id"],
                    "question": position.get("question"),
                    "outcome": position.get("outcome"),
                    "shares": shares,
                    "sell_price": sell_price,
                    "reason": pending.get("reason"),
                    "attempt_number": pending["attempts"],
                    "response": response,
                },
            )

            if pending["attempts"] == 1:
                msg = f"{pending['reason']}\nMarket: <i>{position.get('question', 'Unknown market')[:80]}</i>"
            else:
                msg = (
                    f"🔁 EXIT RETRY #{pending['attempts']} @ ${sell_price:.3f}\n"
                    f"Shares left: {shares:.4f}\n"
                    f"Market: <i>{position.get('question', 'Unknown market')[:80]}</i>"
                )
            log(msg, tg=True)
            complete_transaction(state, "submitted")
            did_trade = True
        except Exception as error:
            pending["last_error"] = str(error)
            state["pending_exits"][str(asset_id)] = pending
            journal_entry(
                state,
                "SELL_ATTEMPT_FAILED",
                {
                    "asset_id": position["asset_id"],
                    "question": position.get("question"),
                    "outcome": position.get("outcome"),
                    "shares": shares,
                    "sell_price": sell_price,
                    "reason": pending.get("reason"),
                    "attempt_number": pending["attempts"],
                    "error": str(error),
                },
            )
            log(f"{pending['reason']} failed: {error}", level="ERROR", tg=True)
            complete_transaction(state, "failed", error=error)
        finally:
            if execution_lock.locked():
                execution_lock.release()

        save_json(LIVE_STATE_FILE, state)

    return did_trade

def reconcile_exchange_state(state, sync_snapshot):
    exchange_positions = sync_snapshot.get("exchange_positions",[])
    existing = {str(item.get("asset_id")): item for item in state.get("active_positions",[])}
    reconciled =[]

    for position in exchange_positions:
        asset_id = str(position.get("asset"))
        size = float(position.get("size", 0.0) or 0.0)
        if size <= 0:
            continue

        local = existing.get(asset_id, {})
        opened_at = local.get("opened_at") or datetime.now(timezone.utc).isoformat()
        avg_price = float(position.get("avgPrice", local.get("avg_price", 0.0)) or 0.0)
        max_recent_price = float(local.get("max_recent_price", avg_price))
        target_price = float(local.get("target_price", avg_price * (1.0 + MIN_PROFIT_PERCENT)))

        reconciled.append(
            {
                "asset_id": asset_id,
                "condition_id": position.get("conditionId"),
                "market_id": local.get("market_id"),
                "event_id": local.get("event_id"),
                "question": local.get("question") or position.get("title", "Unknown market"),
                "outcome": local.get("outcome") or position.get("outcome", "Unknown outcome"),
                "outcome_index": local.get("outcome_index", position.get("outcomeIndex")),
                "avg_price": avg_price,
                "buy_price": avg_price,
                "current_price": float(position.get("curPrice", local.get("current_price", avg_price)) or avg_price),
                "current_value": float(position.get("currentValue", local.get("current_value", 0.0)) or 0.0),
                "size": size,
                "shares": size,
                "cost": float(position.get("initialValue", local.get("cost", 0.0)) or local.get("cost", 0.0)),
                "opened_at": opened_at,
                "target_price": target_price,
                "max_recent_price": max_recent_price,
                "tick_size": local.get("tick_size", "0.01"),
                "end_date": local.get("end_date") or position.get("endDate"),
                "is_sports_market": bool(local.get("is_sports_market")),
            }
        )

    state["active_positions"] = reconciled

def journal_entry(state, action, payload):
    state.setdefault("journal",[]).append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "payload": payload,
        }
    )
    state["journal"] = state["journal"][-500:]

def get_positions_current_value(positions):
    total = 0.0
    for position in positions or []:
        total += safe_float(position.get("currentValue"))
    return total

def get_stat_counts(state):
    counts = {
        "WON": 0,
        "TP": 0,
        "SAFE": 0,
        "SL": 0,
        "LOST": 0,
        "CLOSED": 0,
    }

    for entry in state.get("journal", []):
        action = str(entry.get("action") or "")
        if action.endswith("_FAILED"):
            continue

        if action == "SELL_TAKE_PROFIT":
            counts["TP"] += 1
            counts["CLOSED"] += 1
        elif action == "SELL_TIME_STOP":
            counts["SAFE"] += 1
            counts["CLOSED"] += 1
        elif action == "SELL_STOP_LOSS":
            counts["SL"] += 1
            counts["CLOSED"] += 1
        elif action == "SELL_WON":
            counts["WON"] += 1
            counts["CLOSED"] += 1
        elif action == "SELL_LOST":
            counts["LOST"] += 1
            counts["CLOSED"] += 1

    return counts

def format_statistics_message(state, sync_snapshot):
    available_balance = safe_float(sync_snapshot.get("available_balance"))
    locked_value = safe_float(sync_snapshot.get("positions_current_value"))
    total_assets = sync_snapshot.get("portfolio_value")
    if total_assets is None:
        total_assets = available_balance + locked_value
    total_assets = safe_float(total_assets)

    counts = get_stat_counts(state)
    closed_count = counts["CLOSED"]
    active_count = len(state.get("active_positions", []))
    positive_closed = counts["WON"] + counts["TP"] + counts["SAFE"]
    win_rate = (positive_closed / closed_count * 100.0) if closed_count else 0.0
    net_profit = total_assets - STARTING_BALANCE

    return (
        f"📊 {STRATEGY_DISPLAY_NAME}\n"
        f"🕒 {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"💰 Free balance: ${available_balance:.2f}\n"
        f"🔒 Locked: ${locked_value:.2f}\n"
        f"💵 Total Assets: ${total_assets:.2f}\n"
        f"📈 Net Profit: ${net_profit:.2f}\n\n"
        f"🔄 Active: {active_count} | 📊 Closed: {closed_count}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n"
        f"✅WON:{counts['WON']} | 🤑TP:{counts['TP']} | 🛡️SAFE:{counts['SAFE']} | "
        f"⛔SL:{counts['SL']} | ❌LOST:{counts['LOST']}"
    )

def sync_exchange(execution_client, state):
    open_orders = execution_client.get_open_orders()
    positions = execution_client.get_positions()
    portfolio_value_api = execution_client.get_total_value()
    balance_allowance = execution_client.get_balance_allowance()
    
    available_balance = None
    if balance_allowance:
        try:
            balance_raw = balance_allowance.get("balance", 0.0)
            available_balance = micro_to_usdc(balance_raw)
        except Exception:
            available_balance = None

    positions_current_value = get_positions_current_value(positions)
    portfolio_value_dynamic = None
    if available_balance is not None:
        portfolio_value_dynamic = available_balance + positions_current_value

    portfolio_value = portfolio_value_dynamic
    if portfolio_value is None:
        portfolio_value = portfolio_value_api

    sync_snapshot = {
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "open_orders": open_orders,
        "exchange_positions": positions,
        "portfolio_value": portfolio_value,
        "portfolio_value_api": portfolio_value_api,
        "portfolio_value_dynamic": portfolio_value_dynamic,
        "positions_current_value": positions_current_value,
        "balance_allowance": balance_allowance,
        "available_balance": available_balance,
        "derived_api_creds": None,
    }
    save_json(LIVE_SYNC_FILE, sync_snapshot)
    reconcile_exchange_state(state, sync_snapshot)
    save_json(LIVE_STATE_FILE, state)
    return sync_snapshot

def build_limit_order_payload(position, order_id, limit_price):
    expires_at = None
    expiry_seconds = seconds_until_expiry(position.get("end_date"))
    expiration = 0
    if expiry_seconds is not None:
        expiration_dt = utc_now() + timedelta(seconds=max(30, min(expiry_seconds, LIMIT_ORDER_EXPIRY_BUFFER_SECONDS)))
        expires_at = expiration_dt.isoformat()
        expiration = int(expiration_dt.timestamp())
    return {
        "order_id": order_id,
        "asset_id": str(position.get("asset_id")),
        "market_id": str(position.get("market_id") or ""),
        "price": safe_float(limit_price),
        "shares": safe_float(position.get("shares")),
        "placed_at": utc_now().isoformat(),
        "expires_at": expires_at,
        "expiration": expiration,
    }

def ensure_protective_limit_order(execution_client, state, position, sync_snapshot=None, notify_prefix=None):
    asset_id = str(position.get("asset_id"))
    if has_limit_order(sync_snapshot or {}, asset_id) or get_limit_order(state, asset_id):
        return True

    shares = safe_float(position.get("shares"))
    entry_price = safe_float(position.get("avg_price", position.get("buy_price")))
    if shares <= 0 or entry_price <= 0:
        return False

    tick_size = position.get("tick_size", "0.01")
    limit_price = round_price_to_tick(entry_price * STOP_LOSS_MULTIPLIER, tick_size)
    expiry_seconds = seconds_until_expiry(position.get("end_date"))
    expiration = None
    if expiry_seconds is not None:
        expiration = int((utc_now() + timedelta(seconds=max(30, min(expiry_seconds, LIMIT_ORDER_EXPIRY_BUFFER_SECONDS)))).timestamp())

    last_error = None
    for attempt in range(1, LIMIT_ORDER_RETRY_COUNT + 1):
        try:
            response = execution_client.place_limit_sell(
                token_id=asset_id,
                shares=shares,
                price=limit_price,
                tick_size=tick_size,
                expiration=expiration,
            )
            order_id = resolve_order_id(response)
            set_limit_order(state, asset_id, build_limit_order_payload(position, order_id, limit_price))
            journal_entry(
                state,
                "SELL_LIMIT_PLACED",
                {
                    "asset_id": asset_id,
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "outcome": position.get("outcome"),
                    "shares": shares,
                    "limit_price": limit_price,
                    "attempt": attempt,
                    "response": response,
                },
            )
            save_json(LIVE_STATE_FILE, state)
            return True
        except Exception as error:
            last_error = error
            journal_entry(
                state,
                "SELL_LIMIT_FAILED",
                {
                    "asset_id": asset_id,
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "outcome": position.get("outcome"),
                    "shares": shares,
                    "limit_price": limit_price,
                    "attempt": attempt,
                    "error": str(error),
                },
            )
            save_json(LIVE_STATE_FILE, state)
            if response_has_no_orderbook(error):
                log(
                    f"Protective SELL LIMIT skipped for {asset_id}: {error}",
                    level="WARNING",
                    tg=True,
                )
                break
            time.sleep(LIMIT_ORDER_RETRY_DELAY_SECONDS)

    prefix = notify_prefix or "Protective SELL LIMIT failed"
    log(
        f"{prefix} for {position.get('question', 'Unknown market')[:80]}: {last_error}",
        level="ERROR",
        tg=True,
    )
    return False

def cancel_orphan_limit_orders(execution_client, state, sync_snapshot):
    active_asset_ids = {str(position.get("asset_id")) for position in state.get("active_positions", [])}
    open_orders = sync_snapshot.get("open_orders", [])
    changed = False

    for order in open_orders:
        asset_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or "")
        side = str(order.get("side") or "").upper()
        if side != "SELL":
            continue
        if asset_id in active_asset_ids:
            continue
        order_id = order.get("id") or order.get("orderID") or order.get("orderId")
        if not order_id:
            continue
        try:
            execution_client.cancel_order_by_id(order_id)
            changed = True
            journal_entry(state, "ORPHAN_LIMIT_CANCELLED", {"asset_id": asset_id, "order_id": str(order_id)})
        except Exception as error:
            journal_entry(state, "ORPHAN_LIMIT_CANCEL_FAILED", {"asset_id": asset_id, "order_id": str(order_id), "error": str(error)})
            log(f"Failed to cancel orphan limit order {order_id}: {error}", level="WARNING")

    for asset_id in list(state.get("limit_orders", {}).keys()):
        if str(asset_id) not in active_asset_ids:
            clear_limit_order(state, asset_id)
            changed = True

    if changed:
        save_json(LIVE_STATE_FILE, state)
    return changed

def cancel_tracked_limit_order(execution_client, state, asset_id):
    tracked = get_limit_order(state, asset_id)
    if not tracked or not tracked.get("order_id"):
        return False
    try:
        execution_client.cancel_order_by_id(tracked["order_id"])
        journal_entry(state, "SELL_LIMIT_CANCELLED", {"asset_id": str(asset_id), "order_id": str(tracked["order_id"])})
    except Exception as error:
        journal_entry(state, "SELL_LIMIT_CANCEL_FAILED", {"asset_id": str(asset_id), "order_id": str(tracked["order_id"]), "error": str(error)})
        log(f"Failed to cancel protective limit order {tracked['order_id']}: {error}", level="WARNING")
        return False
    clear_limit_order(state, asset_id)
    save_json(LIVE_STATE_FILE, state)
    return True

def recover_state(execution_client, state):
    state["recovery"] = {
        "last_started_at": utc_now().isoformat(),
        "last_completed_at": None,
        "last_status": "running",
    }
    save_json(LIVE_STATE_FILE, state)
    sync_snapshot = sync_exchange(execution_client, state)
    cancel_orphan_limit_orders(execution_client, state, sync_snapshot)
    sync_snapshot = sync_exchange(execution_client, state)
    for position in state.get("active_positions", []):
        ensure_protective_limit_order(
            execution_client,
            state,
            position,
            sync_snapshot=sync_snapshot,
            notify_prefix="Recovery protective SELL LIMIT failed",
        )
    state["recovery"] = {
        "last_started_at": state["recovery"].get("last_started_at"),
        "last_completed_at": utc_now().isoformat(),
        "last_status": "completed",
    }
    save_json(LIVE_STATE_FILE, state)
    return sync_snapshot

def position_is_in_cooldown(state, market_id):
    cooldown_until = state.get("cooldowns", {}).get(str(market_id))
    if not cooldown_until:
        return False
    return cooldown_until > datetime.now(timezone.utc).timestamp()

def has_open_position(state, token_id):
    token_id = str(token_id)
    for position in state.get("active_positions",[]):
        if str(position.get("asset_id")) == token_id and float(position.get("shares", 0.0)) > 0:
            return True
    return False

def has_open_order(sync_snapshot, token_id):
    token_id = str(token_id)
    for order in sync_snapshot.get("open_orders",[]):
        asset_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or "")
        if asset_id == token_id:
            return True
    return False

def has_recent_buy(state, token_id, minutes=BUY_COOLDOWN_MINUTES):
    token_id = str(token_id)
    cutoff = utc_now() - timedelta(minutes=minutes)
    for entry in reversed(state.get("journal", [])):
        if str(entry.get("action")) == "BUY_SUBMITTED":
            payload = entry.get("payload", {})
            if str(payload.get("token_id")) == token_id:
                ts = parse_datetime(entry.get("ts"))
                if ts and ts > cutoff:
                    return True
                # Found matching token but too old — stop searching
                return False
            # Different token — keep looking
            continue
    return False

def attempt_entries(execution_client, state, sync_snapshot):
    if transaction_in_flight(state):
        log("Skipping entries: previous transaction is still awaiting confirmation.", level="WARNING")
        return False

    available_balance = sync_snapshot.get("available_balance")
    
    if available_balance is None:
        return False

    if available_balance < BET_AMOUNT:
        log(f"Available collateral ${available_balance:.2f} is below BET_AMOUNT ${BET_AMOUNT:.2f}. Skipping new entries.")
        return False

    valid_events = collect_valid_events()
    candidates = maintain_price_history(valid_events)
    if not candidates:
        log("No swing-trade setups found right now.")
        return False

    candidates.sort(key=lambda item: item["score"], reverse=True)
    did_trade = False
    stats = {
        "total": len(candidates),
        "too_soon": 0,
        "cooldown": 0,
        "banned": 0,
        "has_position": 0,
        "pending_exit": 0,
        "has_order": 0,
        "balance_err": 0,
        "has_balance": 0,
        "entry_price_err": 0,
        "entry_price_high": 0,
        "no_profit": 0,
    }

    for candidate in candidates:
        seconds_left = seconds_until_expiry(candidate.get("end_date"))
        if seconds_left is None or seconds_left <= 0 or seconds_left < MIN_DAYS_TO_EXPIRY * 24 * 3600:
            stats["too_soon"] += 1
            continue
        if position_is_in_cooldown(state, candidate["market_id"]):
            stats["cooldown"] += 1
            continue
        if is_market_banned(state, candidate["market_id"]):
            stats["banned"] += 1
            continue
        if has_open_position(state, candidate["token_id"]):
            stats["has_position"] += 1
            continue
        if has_recent_buy(state, candidate["token_id"]):
            stats["has_position"] += 1
            continue
        if has_pending_exit(state, token_id=candidate["token_id"], market_id=candidate["market_id"]):
            stats["pending_exit"] += 1
            continue
        if has_open_order(sync_snapshot, candidate["token_id"]):
            stats["has_order"] += 1
            continue
        token_balance = execution_client.get_token_balance(candidate["token_id"])
        if token_balance is None:
            stats["balance_err"] += 1
            log(f"Skipping entry for {candidate['token_id']}: unable to verify token balance.", level="WARNING")
            continue
        if token_balance > 0.01:
            stats["has_balance"] += 1
            log(
                f"Blocking BUY for token {candidate['token_id']}: on-chain/API balance is {token_balance:.4f}.",
                level="INFO",
            )
            continue

        acquired = execution_lock.acquire(timeout=1)
        if not acquired:
            log("Skipping entry: another transaction is still being processed.", level="WARNING")
            return did_trade

        try:
            entry_price = execution_client.get_market_execution_price(
                token_id=candidate["token_id"],
                side=Side.BUY,
                amount=BET_AMOUNT,
            )
            if entry_price is None or entry_price <= 0:
                stats["entry_price_err"] += 1
                continue
            if entry_price > MAX_PRICE:
                stats["entry_price_high"] += 1
                log(
                    f"Skipping entry: order book ask-implied price ${entry_price:.3f} is above MAX_PRICE "
                    f"${MAX_PRICE:.3f} | {candidate['question'][:60]}"
                )
                continue

            max_recent_price = candidate["max_recent_price"]
            target_price = entry_price + ((max_recent_price - entry_price) * RECOVERY_TARGET_PERCENT)
            if target_price <= entry_price:
                stats["no_profit"] += 1
                continue
            expected_profit_pct = ((target_price - entry_price) / entry_price) * 100.0
            if expected_profit_pct < MIN_PROFIT_PERCENT * 100.0:
                stats["no_profit"] += 1
                continue

            begin_transaction(
                state,
                "BUY",
                token_id=candidate["token_id"],
                market_id=candidate["market_id"],
                extra={"question": candidate["question"][:120]},
            )
            response = execution_client.place_market_buy(
                token_id=candidate["token_id"],
                usdc_amount=BET_AMOUNT,
                tick_size=candidate["tick_size"],
            )
            journal_entry(
                state,
                "BUY_SUBMITTED",
                {
                    "market_id": candidate["market_id"],
                    "event_id": candidate["event_id"],
                    "question": candidate["question"],
                    "outcome": candidate["outcome"],
                    "token_id": candidate["token_id"],
                    "current_price": entry_price,
                    "target_price": target_price,
                    "response": response,
                },
            )
            # Отправляем сообщение в Telegram всем админам (tg=True)
            msg = (
                f"📉 <b>LIVE BUY submitted</b>\n"
                f"Outcome: {candidate['outcome']} @ ~${entry_price:.3f}\n"
                f"Target TP: ${target_price:.3f} (+{expected_profit_pct:.1f}%)\n"
                f"Market: <i>{candidate['question'][:80]}</i>"
            )
            log(msg, tg=True)
            
            confirmed, sync_snapshot = await_position_sync(
                execution_client,
                state,
                candidate["token_id"],
                should_exist=True,
            )
            if not confirmed:
                raise TimeoutError(f"BUY confirmation timeout for token {candidate['token_id']}")

            for position in state.get("active_positions",[]):
                if str(position.get("asset_id")) == str(candidate["token_id"]):
                    position["market_id"] = candidate["market_id"]
                    position["event_id"] = candidate["event_id"]
                    position["question"] = candidate["question"]
                    position["outcome"] = candidate["outcome"]
                    position["outcome_index"] = candidate["outcome_index"]
                    position["target_price"] = target_price
                    position["max_recent_price"] = candidate["max_recent_price"]
                    position["tick_size"] = candidate["tick_size"]
                    position["end_date"] = candidate["end_date"]
                    position["is_sports_market"] = bool(candidate.get("is_sports_market"))
                    if not position.get("opened_at"):
                        position["opened_at"] = utc_now().isoformat()
                    ensure_protective_limit_order(
                        execution_client,
                        state,
                        position,
                        sync_snapshot=sync_snapshot,
                        notify_prefix="Immediate SELL LIMIT failed after BUY",
                    )
            save_json(LIVE_STATE_FILE, state)
            complete_transaction(state, "confirmed")
            did_trade = True
            break
        except Exception as error:
            journal_entry(state, "BUY_FAILED", {"market_id": candidate["market_id"], "token_id": candidate["token_id"], "error": str(error)})
            log(f"Live BUY failed: {error}", level="ERROR", tg=True)
            complete_transaction(state, "failed", error=error)
            save_json(LIVE_STATE_FILE, state)
        finally:
            if execution_lock.locked():
                execution_lock.release()

    log(
        f"[attempt_entries] total_candidates={stats['total']} too_soon={stats['too_soon']} "
        f"cooldown={stats['cooldown']} banned={stats['banned']} has_position={stats['has_position']} "
        f"pending_exit={stats['pending_exit']} has_order={stats['has_order']} balance_err={stats['balance_err']} "
        f"has_balance={stats['has_balance']} entry_price_err={stats['entry_price_err']} entry_price_high={stats['entry_price_high']} "
        f"no_profit={stats['no_profit']}"
    )
    return did_trade

def close_position(execution_client, state, position, reason, sell_price, journal_action, sync_snapshot=None):
    queue_pending_exit(state, position, reason, journal_action, sell_price, sync_snapshot=sync_snapshot)
    return attempt_pending_exits(execution_client, state, force_asset_id=position["asset_id"])

def attempt_exits(execution_client, state, sync_snapshot=None):
    did_trade = False
    now = datetime.now(timezone.utc)

    for position in state.get("active_positions",[]):
        if has_pending_exit(state, token_id=position.get("asset_id")):
            continue

        avg_price = float(position.get("avg_price", 0.0) or 0.0)
        shares = safe_float(position.get("shares"))
        executable_sell_price = execution_client.get_market_execution_price(
            token_id=position["asset_id"],
            side=Side.SELL,
            amount=shares,
        )
        current_price = float(
            executable_sell_price
            if executable_sell_price is not None and executable_sell_price > 0
            else position.get("current_price", avg_price) or avg_price
        )
        target_price = float(position.get("target_price", avg_price * (1.0 + MIN_PROFIT_PERCENT)))
        tick_size = position.get("tick_size", "0.01")
        opened_at_raw = position.get("opened_at")

        try:
            opened_at = datetime.fromisoformat(opened_at_raw.replace("Z", "+00:00")) if opened_at_raw else now
        except Exception:
            opened_at = now
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        hours_held = (now - opened_at).total_seconds() / 3600.0

        if avg_price > 0 and current_price >= target_price:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(execution_client, state, position, f"🤑 DYNAMIC TP HIT @ ${sell_price:.3f}", sell_price, "SELL_TAKE_PROFIT", sync_snapshot=sync_snapshot):
                did_trade = True
                break

        if avg_price > 0 and current_price <= avg_price * STOP_LOSS_MULTIPLIER:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(execution_client, state, position, f"⛔ STOP LOSS HIT @ ${sell_price:.3f}", sell_price, "SELL_STOP_LOSS", sync_snapshot=sync_snapshot):
                cooldown_until = (now + timedelta(hours=COOLDOWN_HOURS)).timestamp()
                market_id = str(position.get("market_id") or "")
                if market_id:
                    state.setdefault("cooldowns", {})[market_id] = cooldown_until
                did_trade = True
                break

        if hours_held >= MAX_HOLD_HOURS:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(execution_client, state, position, f"⏱️ TIME STOP ({MAX_HOLD_HOURS}h) @ ${sell_price:.3f}", sell_price, "SELL_TIME_STOP", sync_snapshot=sync_snapshot):
                did_trade = True
                break

    return did_trade

def main():
    log(f"Starting live swing service for Polymarket. Using py-clob-client V2.", tg=True)
    service_stop_event.clear()

    state = ensure_live_state_schema(load_json(LIVE_STATE_FILE, default_live_state()))
    cleanup_cooldowns(state)
    save_json(LIVE_STATE_FILE, state)

    try:
        execution_client = PolymarketExecutionClient()
        execution_client.initialize()
    except Exception as e:
        log(f"Failed to initialize Polymarket client: {e}", "ERROR", tg=True)
        sys.exit(1)

    sync_snapshot = recover_state(execution_client, state)
    save_json(LIVE_SYNC_FILE, sync_snapshot)
    update_telegram_runtime_state(state, sync_snapshot)
    start_telegram_bot()

    val = sync_snapshot.get('portfolio_value')
    val_str = f"${val:.2f}" if val is not None else "Unknown"
    bal = sync_snapshot.get('available_balance')
    bal_str = f"${bal:.2f}" if bal is not None else "Unknown"
    
    log(
        f"Initial sync complete. Positions: {len(sync_snapshot['exchange_positions'])} | "
        f"Portfolio value: {val_str} | Available Balance: {bal_str}", 
        tg=True
    )

    try:
        while not service_stop_event.is_set():
            try:
                cleanup_cooldowns(state)
                sync_snapshot = sync_exchange(execution_client, state)
                finalize_completed_pending_exits(state, sync_snapshot)
                cancel_orphan_limit_orders(execution_client, state, sync_snapshot)
                update_telegram_runtime_state(state, sync_snapshot)
                if attempt_pending_exits(execution_client, state):
                    sync_snapshot = sync_exchange(execution_client, state)
                    finalize_completed_pending_exits(state, sync_snapshot)
                    cancel_orphan_limit_orders(execution_client, state, sync_snapshot)
                    update_telegram_runtime_state(state, sync_snapshot)
                attempt_exits(execution_client, state, sync_snapshot)
                sync_snapshot = sync_exchange(execution_client, state)
                finalize_completed_pending_exits(state, sync_snapshot)
                cancel_orphan_limit_orders(execution_client, state, sync_snapshot)
                update_telegram_runtime_state(state, sync_snapshot)
                attempt_entries(execution_client, state, sync_snapshot)
                state["last_cycle_at"] = utc_now().isoformat()
                save_json(LIVE_STATE_FILE, state)
                update_telegram_runtime_state(state, sync_snapshot)
                sleep_seconds = EXIT_RETRY_SECONDS if state.get("pending_exits") else CHECK_INTERVAL_SECONDS
                service_stop_event.wait(sleep_seconds)
            except requests.exceptions.RequestException as error:
                log(f"Network error: {error}", level="WARNING")
                service_stop_event.wait(EXIT_RETRY_SECONDS if state.get("pending_exits") else CHECK_INTERVAL_SECONDS)
            except Exception:
                err_trace = traceback.format_exc()
                log(f"Cycle failure:\n{err_trace}", level="ERROR", tg=True)
                service_stop_event.wait(EXIT_RETRY_SECONDS if state.get("pending_exits") else CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("Live swing service stopped by user.", tg=True)
    if service_stop_event.is_set():
        log("Live swing service stopped by Telegram command.", tg=True)

if __name__ == "__main__":
    main()
