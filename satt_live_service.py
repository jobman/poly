import json
import os
import sys
import time
import traceback
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import requests

CLIENT_BACKEND = None

try:
    from py_clob_client_v2 import (
        ApiCreds as V2ApiCreds,
        ClobClient as V2ClobClient,
        MarketOrderArgs as V2MarketOrderArgs,
        OrderArgs as V2OrderArgs,
        OrderType as V2OrderType,
        PartialCreateOrderOptions as V2PartialCreateOrderOptions,
        Side as V2Side,
    )
    CLIENT_BACKEND = "v2"
except ImportError:  # pragma: no cover
    V2ApiCreds = None
    V2ClobClient = None
    V2MarketOrderArgs = None
    V2OrderArgs = None
    V2OrderType = None
    V2PartialCreateOrderOptions = None
    V2Side = None

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OpenOrderParams,
        OrderArgs,
        OrderType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    if CLIENT_BACKEND is None:
        CLIENT_BACKEND = "legacy"
except ImportError:  # pragma: no cover - handled at runtime
    ClobClient = None
    AssetType = None
    BalanceAllowanceParams = None
    MarketOrderArgs = None
    OpenOrderParams = None
    OrderArgs = None
    OrderType = None
    BUY = None
    SELL = None


# Strategy settings copied from the tested swing bot
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
CHECK_INTERVAL_SECONDS = 60

ENV_FILE = ".env"
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
DATA_API_URL = "https://data-api.polymarket.com"
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137

LIVE_STATE_FILE = "satt_live_state.json"
LIVE_SYNC_FILE = "satt_live_sync.json"
LIVE_HISTORY_FILE = "satt_live_price_history.json"


http_session = requests.Session()
http_session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
)


def load_env_file(path=ENV_FILE):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def log(message, level="INFO"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {message}")


def parse_token_ids(market):
    raw = market.get("clobTokenIds", "[]")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        data = json.loads(raw)
        return [str(item) for item in data]
    except Exception:
        return []


def parse_outcome_prices(market):
    try:
        return [float(item) for item in json.loads(market.get("outcomePrices", "[]"))]
    except Exception:
        return []


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
        "active_positions": [],
        "journal": [],
        "cooldowns": {},
        "last_cycle_at": None,
    }


def default_live_sync():
    return {
        "last_sync_at": None,
        "open_orders": [],
        "exchange_positions": [],
        "portfolio_value": None,
        "balance_allowance": None,
        "available_balance": None,
        "derived_api_creds": None,
    }


def normalize_api_creds(creds):
    if creds is None:
        return None
    if isinstance(creds, dict):
        return creds

    result = {}
    for source_key, target_key in (
        ("api_key", "api_key"),
        ("api_secret", "api_secret"),
        ("api_passphrase", "api_passphrase"),
        ("key", "api_key"),
        ("secret", "api_secret"),
        ("passphrase", "api_passphrase"),
    ):
        value = getattr(creds, source_key, None)
        if value:
            result[target_key] = value
    return result or {"raw": str(creds)}


def build_api_creds_object(raw_creds):
    if raw_creds is None:
        return None
    if all(hasattr(raw_creds, attr) for attr in ("api_key", "api_secret", "api_passphrase")):
        return raw_creds

    if isinstance(raw_creds, dict):
        api_key = raw_creds.get("api_key") or raw_creds.get("key")
        api_secret = raw_creds.get("api_secret") or raw_creds.get("secret")
        api_passphrase = raw_creds.get("api_passphrase") or raw_creds.get("passphrase")
        return SimpleNamespace(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

    return raw_creds


class PolymarketExecutionClient:
    def __init__(self):
        if CLIENT_BACKEND is None:
            raise RuntimeError(
                "No supported Polymarket CLOB client is installed. Install dependencies first."
            )

        self.host = os.getenv("POLYMARKET_CLOB_HOST", DEFAULT_CLOB_HOST).strip()
        self.chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", str(DEFAULT_CHAIN_ID)))
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        self.funder = os.getenv("POLYMARKET_FUNDER", "").strip() or None
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        self.profile_address = (
            os.getenv("POLYMARKET_PROFILE_ADDRESS", "").strip()
            or self.funder
        )
        self.client = None
        self.backend = CLIENT_BACKEND

    def initialize(self):
        if not self.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required in .env")

        api_key = os.getenv("POLYMARKET_CLOB_API_KEY", "").strip()
        api_secret = os.getenv("POLYMARKET_CLOB_SECRET", "").strip()
        api_passphrase = os.getenv("POLYMARKET_CLOB_PASS_PHRASE", "").strip()

        if self.backend == "v2":
            creds = None
            if api_key and api_secret and api_passphrase:
                creds = V2ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                )

            # v2 docs only show host/chain_id/key/creds; keep constructor minimal.
            self.client = V2ClobClient(
                host=self.host,
                chain_id=self.chain_id,
                key=self.private_key,
                creds=creds,
            )

            if creds is None:
                creds = self.client.create_or_derive_api_key()
                log(
                    "Derived L2 API credentials from wallet via py-clob-client-v2. "
                    "Save them to .env for stable reuse."
                )
                self.client = V2ClobClient(
                    host=self.host,
                    chain_id=self.chain_id,
                    key=self.private_key,
                    creds=creds,
                )

            return normalize_api_creds(creds)

        self.client = ClobClient(
            self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            signature_type=self.signature_type,
            funder=self.funder,
        )
        if api_key and api_secret and api_passphrase:
            creds = build_api_creds_object(
                {
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "api_passphrase": api_passphrase,
                }
            )
        else:
            creds = self.client.create_or_derive_api_creds()
            log(
                "Derived L2 API credentials from wallet via legacy py-clob-client. "
                "Save them to .env for stable reuse."
            )

        self.client.set_api_creds(creds)
        return normalize_api_creds(creds)

    def get_open_orders(self):
        if self.backend == "v2":
            try:
                if hasattr(self.client, "get_orders"):
                    return self.client.get_orders() or []
                if hasattr(self.client, "get_open_orders"):
                    return self.client.get_open_orders() or []
            except Exception as error:
                log(f"Failed to load open orders: {error}", level="WARNING")
                return []
            return []

        try:
            return self.client.get_orders(OpenOrderParams()) or []
        except Exception as error:
            log(f"Failed to load open orders: {error}", level="WARNING")
            return []

    def get_balance_allowance(self):
        if self.backend == "v2":
            # v2 official public README currently documents auth and order placement,
            # but not a balance/allowance helper. Keep a graceful fallback while the
            # rest of the service runs on v2.
            try:
                if hasattr(self.client, "get_balance_allowance"):
                    result = self.client.get_balance_allowance()
                    if isinstance(result, dict):
                        return result
                    return normalize_api_creds(result) if result else None
            except Exception as error:
                log(f"Failed to load collateral balance/allowance: {error}", level="WARNING")
            return None

        if not hasattr(self.client, "get_balance_allowance") or BalanceAllowanceParams is None:
            return None

        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=self.signature_type,
        )
        try:
            result = self.client.get_balance_allowance(params=params)
            if isinstance(result, dict):
                return result
            normalized = {}
            for source_key, target_key in (
                ("balance", "balance"),
                ("allowance", "allowance"),
                ("available", "available"),
            ):
                value = getattr(result, source_key, None)
                if value is not None:
                    normalized[target_key] = value
            return normalized or {"raw": str(result)}
        except Exception as error:
            log(f"Failed to load collateral balance/allowance: {error}", level="WARNING")
            return None

    def get_positions(self):
        if not self.profile_address:
            return []

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
            return []

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
        if self.backend == "v2":
            return self.client.create_and_post_market_order(
                order_args=V2MarketOrderArgs(
                    token_id=str(token_id),
                    amount=float(usdc_amount),
                    side=V2Side.BUY,
                    order_type=V2OrderType.FOK,
                ),
                options=V2PartialCreateOrderOptions(tick_size=str(tick_size)),
                order_type=V2OrderType.FOK,
            )

        order = MarketOrderArgs(
            token_id=str(token_id),
            amount=float(usdc_amount),
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(order)
        return self.client.post_order(signed, OrderType.FOK)

    def place_limit_sell(self, token_id, shares, price):
        if self.backend == "v2":
            return self.client.create_and_post_order(
                order_args=V2OrderArgs(
                    token_id=str(token_id),
                    price=float(price),
                    size=float(shares),
                    side=V2Side.SELL,
                ),
                options=V2PartialCreateOrderOptions(tick_size="0.01"),
                order_type=V2OrderType.FAK,
            )

        order = OrderArgs(
            token_id=str(token_id),
            price=float(price),
            size=float(shares),
            side=SELL,
        )
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.FAK)


def maintain_price_history(valid_events):
    history = load_json(LIVE_HISTORY_FILE, {})
    now_ts = int(time.time())
    cutoff_ts = now_ts - int(HISTORY_WINDOW_HOURS * 3600)
    candidates = []

    for event in valid_events:
        for market in event.get("markets", []):
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
                outcome_key = str(outcome_index)
                outcome_history = market_history.setdefault(outcome_key, [])
                outcome_history = [record for record in outcome_history if record[0] >= cutoff_ts]
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
    valid_events = []
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
    exchange_positions = sync_snapshot.get("exchange_positions", [])
    existing = {str(item.get("asset_id")): item for item in state.get("active_positions", [])}
    reconciled = []

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
    state.setdefault("journal", []).append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "payload": payload,
        }
    )
    state["journal"] = state["journal"][-500:]


def sync_exchange(execution_client, state):
    open_orders = execution_client.get_open_orders()
    positions = execution_client.get_positions()
    portfolio_value = execution_client.get_total_value()
    balance_allowance = execution_client.get_balance_allowance()
    available_balance = None
    if balance_allowance:
        try:
            balance = float(balance_allowance.get("balance") or 0.0)
            allowance = float(balance_allowance.get("allowance") or 0.0)
            available_balance = min(balance, allowance)
        except Exception:
            available_balance = None

    sync_snapshot = {
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "open_orders": open_orders,
        "exchange_positions": positions,
        "portfolio_value": portfolio_value,
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
    for position in state.get("active_positions", []):
        if str(position.get("asset_id")) == token_id and float(position.get("shares", 0.0)) > 0:
            return True
    return False


def has_open_order(sync_snapshot, token_id):
    token_id = str(token_id)
    for order in sync_snapshot.get("open_orders", []):
        asset_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or "")
        if asset_id == token_id:
            return True
    return False


def attempt_entries(execution_client, state, sync_snapshot):
    available_balance = sync_snapshot.get("available_balance")
    if available_balance is not None and available_balance < BET_AMOUNT:
        log(
            f"Available collateral ${available_balance:.2f} is below BET_AMOUNT ${BET_AMOUNT:.2f}. "
            "Skipping new entries."
        )
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
            log(
                (
                    f"📉 LIVE BUY submitted | {candidate['outcome']} @ ~${candidate['current_price']:.3f}\n"
                    f"Target TP: ${candidate['target_price']:.3f} (+{candidate['expected_profit_pct']:.1f}%)\n"
                    f"Market: {candidate['question'][:80]}"
                )
            )
            did_trade = True
            sync_snapshot = sync_exchange(execution_client, state)

            for position in state.get("active_positions", []):
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
            journal_entry(
                state,
                "BUY_FAILED",
                {
                    "market_id": candidate["market_id"],
                    "token_id": candidate["token_id"],
                    "error": str(error),
                },
            )
            log(f"Live BUY failed: {error}", level="ERROR")
            save_json(LIVE_STATE_FILE, state)

    return did_trade


def close_position(execution_client, state, position, reason, sell_price, journal_action):
    try:
        response = execution_client.place_limit_sell(
            token_id=position["asset_id"],
            shares=position["shares"],
            price=sell_price,
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
        log(f"{reason} | {position.get('question', 'Unknown market')[:80]}")
        save_json(LIVE_STATE_FILE, state)
        return True
    except Exception as error:
        journal_entry(
            state,
            f"{journal_action}_FAILED",
            {
                "asset_id": position["asset_id"],
                "reason": reason,
                "error": str(error),
            },
        )
        log(f"{reason} failed: {error}", level="ERROR")
        save_json(LIVE_STATE_FILE, state)
        return False


def attempt_exits(execution_client, state):
    did_trade = False
    now = datetime.now(timezone.utc)

    for position in state.get("active_positions", []):
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
            if close_position(
                execution_client,
                state,
                position,
                f"🤑 DYNAMIC TP HIT @ ${sell_price:.3f}",
                sell_price,
                "SELL_TAKE_PROFIT",
            ):
                did_trade = True
                break

        if avg_price > 0 and current_price <= avg_price * STOP_LOSS_MULTIPLIER:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(
                execution_client,
                state,
                position,
                f"⛔ STOP LOSS HIT @ ${sell_price:.3f}",
                sell_price,
                "SELL_STOP_LOSS",
            ):
                cooldown_until = (now + timedelta(hours=COOLDOWN_HOURS)).timestamp()
                market_id = str(position.get("market_id") or "")
                if market_id:
                    state.setdefault("cooldowns", {})[market_id] = cooldown_until
                did_trade = True
                break

        if hours_held >= MAX_HOLD_HOURS:
            sell_price = round_price_to_tick(current_price, tick_size)
            if close_position(
                execution_client,
                state,
                position,
                f"⏱️ TIME STOP ({MAX_HOLD_HOURS}h) @ ${sell_price:.3f}",
                sell_price,
                "SELL_TIME_STOP",
            ):
                did_trade = True
                break

    return did_trade


def ensure_runtime_ready():
    load_env_file()
    if CLIENT_BACKEND is None:
        raise RuntimeError(
            "Missing dependency: install py-clob-client-v2 (preferred) or py-clob-client before running satt_live_service.py"
        )


def main():
    ensure_runtime_ready()
    log(f"Starting live swing service for Polymarket. CLOB backend: {CLIENT_BACKEND}")

    state = load_json(LIVE_STATE_FILE, default_live_state())
    cleanup_cooldowns(state)
    save_json(LIVE_STATE_FILE, state)

    execution_client = PolymarketExecutionClient()
    derived_creds = execution_client.initialize()

    sync_snapshot = sync_exchange(execution_client, state)
    sync_snapshot["derived_api_creds"] = derived_creds if not os.getenv("POLYMARKET_CLOB_API_KEY") else None
    save_json(LIVE_SYNC_FILE, sync_snapshot)

    log(
        f"Initial sync complete. Positions: {len(sync_snapshot['exchange_positions'])} | "
        f"Open orders: {len(sync_snapshot['open_orders'])} | "
        f"Portfolio value: {sync_snapshot['portfolio_value']}"
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
                log(f"Cycle failure:\n{traceback.format_exc()}", level="ERROR")
                time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("Live swing service stopped by user.")


if __name__ == "__main__":
    main()
