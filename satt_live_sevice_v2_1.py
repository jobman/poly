import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import requests

# --- Чистые импорты V2 (py-clob-client-v2) ---
from py_clob_client_v2 import (
    ClobClient,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side
)
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

# Загружаем переменные окружения здесь, чтобы были доступны токены Telegram
load_dotenv(".env")

# --- Telegram settings ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()

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
MIN_LIQUIDITY = 5000.0
MIN_VOLUME = 20000.0
MIN_DAYS_TO_EXPIRY = 2.0
CHECK_INTERVAL_SECONDS = 30

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
        "last_cycle_at": None,
    }

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

    def get_balance_allowance(self):
        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return result
        except Exception as error:
            log(f"Failed to load collateral balance/allowance: {error}", level="WARNING")
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
                    order_type=OrderType.FOK,
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick_size)),
                order_type=OrderType.FOK
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
                    order_type=OrderType.FOK,
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick_size)),
                order_type=OrderType.FOK,
            )
            return response
        except Exception as e:
            raise e

# --- ЛОГИКА СТРАТЕГИИ ---

def maintain_price_history(valid_events):
    history = load_json(LIVE_HISTORY_FILE, {})
    now_ts = int(time.time())
    cutoff_ts = now_ts - int(HISTORY_WINDOW_HOURS * 3600)
    candidates =[]

    for event in valid_events:
        for market in event.get("markets",[]):
            if market.get("closed"):
                continue

            outcomes = market.get("outcomes",[])
            prices = parse_outcome_prices(market)
            token_ids = parse_token_ids(market)
            if len(prices) != len(token_ids):
                continue

            market_id = str(market["id"])
            market_history = history.setdefault(market_id, {})

            for outcome_index, price in enumerate(prices):
                outcome_key = str(outcome_index)
                outcome_history = market_history.setdefault(outcome_key,[])
                outcome_history =[record for record in outcome_history if record[0] >= cutoff_ts]
                outcome_history.append((now_ts, price))
                market_history[outcome_key] = outcome_history

                if len(outcome_history) < 2 or not (MIN_PRICE <= price <= MAX_PRICE):
                    continue

                max_recent_price = max(record[1] for record in outcome_history)
                if max_recent_price <= 0:
                    continue

                drop_ratio = (max_recent_price - price) / max_recent_price
                if drop_ratio < DROP_PERCENT_REQUIRED:
                    continue

                target_price = price + ((max_recent_price - price) * RECOVERY_TARGET_PERCENT)
                expected_profit_pct = (target_price - price) / price
                if expected_profit_pct < MIN_PROFIT_PERCENT:
                    continue

                score = float(market.get("volume", 0)) + float(market.get("liquidity", 0))
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
                    }
                )

    save_json(LIVE_HISTORY_FILE, history)
    return candidates

def collect_valid_events():
    valid_events =[]
    limit = 100
    offset = 0
    min_end_date = datetime.now(timezone.utc) + timedelta(days=MIN_DAYS_TO_EXPIRY)

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
            end_date_str = event.get("endDate")
            if not end_date_str:
                continue

            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except Exception:
                continue

            if end_date < min_end_date:
                continue
            if float(event.get("volume", 0)) < MIN_VOLUME:
                continue

            eligible_markets =[]
            for market in event.get("markets",[]):
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

        offset += limit
        time.sleep(0.2)

    return valid_events

def cleanup_cooldowns(state):
    now_ts = datetime.now(timezone.utc).timestamp()
    state["cooldowns"] = {
        market_id: ts
        for market_id, ts in state.get("cooldowns", {}).items()
        if ts > now_ts
    }

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

def attempt_entries(execution_client, state, sync_snapshot):
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

    for candidate in candidates:
        if position_is_in_cooldown(state, candidate["market_id"]):
            continue
        if has_open_position(state, candidate["token_id"]):
            continue
        if has_open_order(sync_snapshot, candidate["token_id"]):
            continue

        try:
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
                    "current_price": candidate["current_price"],
                    "target_price": candidate["target_price"],
                    "response": response,
                },
            )
            # Отправляем сообщение в Telegram всем админам (tg=True)
            msg = (
                f"📉 <b>LIVE BUY submitted</b>\n"
                f"Outcome: {candidate['outcome']} @ ~${candidate['current_price']:.3f}\n"
                f"Target TP: ${candidate['target_price']:.3f} (+{candidate['expected_profit_pct']:.1f}%)\n"
                f"Market: <i>{candidate['question'][:80]}</i>"
            )
            log(msg, tg=True)
            
            did_trade = True
            sync_snapshot = sync_exchange(execution_client, state)

            for position in state.get("active_positions",[]):
                if str(position.get("asset_id")) == str(candidate["token_id"]):
                    position["market_id"] = candidate["market_id"]
                    position["event_id"] = candidate["event_id"]
                    position["question"] = candidate["question"]
                    position["outcome"] = candidate["outcome"]
                    position["outcome_index"] = candidate["outcome_index"]
                    position["target_price"] = candidate["target_price"]
                    position["max_recent_price"] = candidate["max_recent_price"]
                    position["tick_size"] = candidate["tick_size"]
                    position["end_date"] = candidate["end_date"]
                    if not position.get("opened_at"):
                        position["opened_at"] = datetime.now(timezone.utc).isoformat()
            save_json(LIVE_STATE_FILE, state)
            break
        except Exception as error:
            journal_entry(state, "BUY_FAILED", {"market_id": candidate["market_id"], "token_id": candidate["token_id"], "error": str(error)})
            log(f"Live BUY failed: {error}", level="ERROR", tg=True)
            save_json(LIVE_STATE_FILE, state)

    return did_trade

def close_position(execution_client, state, position, reason, sell_price, journal_action):
    try:
        response = execution_client.place_market_sell(
            token_id=position["asset_id"],
            shares=position["shares"],
            tick_size=position.get("tick_size", "0.01")
        )
        journal_entry(
            state,
            journal_action,
            {
                "asset_id": position["asset_id"],
                "question": position.get("question"),
                "outcome": position.get("outcome"),
                "shares": position.get("shares"),
                "sell_price": sell_price,
                "reason": reason,
                "response": response,
            },
        )
        # Отправляем сообщение о продаже всем админам
        msg = f"{reason}\nMarket: <i>{position.get('question', 'Unknown market')[:80]}</i>"
        log(msg, tg=True)
        save_json(LIVE_STATE_FILE, state)
        return True
    except Exception as error:
        journal_entry(state, f"{journal_action}_FAILED", {"asset_id": position["asset_id"], "reason": reason, "error": str(error)})
        log(f"{reason} failed: {error}", level="ERROR", tg=True)
        save_json(LIVE_STATE_FILE, state)
        return False

def attempt_exits(execution_client, state):
    did_trade = False
    now = datetime.now(timezone.utc)

    for position in state.get("active_positions",[]):
        avg_price = float(position.get("avg_price", 0.0) or 0.0)
        current_price = float(position.get("current_price", avg_price) or avg_price)
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
            if close_position(execution_client, state, position, f"🤑 DYNAMIC TP HIT @ ${sell_price:.3f}", sell_price, "SELL_TAKE_PROFIT"):
                did_trade = True
                break

        if avg_price > 0 and current_price <= avg_price * STOP_LOSS_MULTIPLIER:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(execution_client, state, position, f"⛔ STOP LOSS HIT @ ${sell_price:.3f}", sell_price, "SELL_STOP_LOSS"):
                cooldown_until = (now + timedelta(hours=COOLDOWN_HOURS)).timestamp()
                market_id = str(position.get("market_id") or "")
                if market_id:
                    state.setdefault("cooldowns", {})[market_id] = cooldown_until
                did_trade = True
                break

        if hours_held >= MAX_HOLD_HOURS:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(execution_client, state, position, f"⏱️ TIME STOP ({MAX_HOLD_HOURS}h) @ ${sell_price:.3f}", sell_price, "SELL_TIME_STOP"):
                did_trade = True
                break

    return did_trade

def main():
    log(f"Starting live swing service for Polymarket. Using py-clob-client V2.", tg=True)

    state = load_json(LIVE_STATE_FILE, default_live_state())
    cleanup_cooldowns(state)
    save_json(LIVE_STATE_FILE, state)

    try:
        execution_client = PolymarketExecutionClient()
        execution_client.initialize()
    except Exception as e:
        log(f"Failed to initialize Polymarket client: {e}", "ERROR", tg=True)
        sys.exit(1)

    sync_snapshot = sync_exchange(execution_client, state)
    save_json(LIVE_SYNC_FILE, sync_snapshot)

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
        while True:
            try:
                cleanup_cooldowns(state)
                sync_snapshot = sync_exchange(execution_client, state)
                attempt_exits(execution_client, state)
                sync_snapshot = sync_exchange(execution_client, state)
                attempt_entries(execution_client, state, sync_snapshot)
                state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
                save_json(LIVE_STATE_FILE, state)
                time.sleep(CHECK_INTERVAL_SECONDS)
            except requests.exceptions.RequestException as error:
                log(f"Network error: {error}", level="WARNING")
                time.sleep(CHECK_INTERVAL_SECONDS)
            except Exception:
                err_trace = traceback.format_exc()
                log(f"Cycle failure:\n{err_trace}", level="ERROR", tg=True)
                time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("Live swing service stopped by user.", tg=True)

if __name__ == "__main__":
    main()
