import json
import math
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests


API_URL = "https://gamma-api.polymarket.com/events"
ENV_FILE = ".env"
LEGACY_PORTFOLIO_FILE = "portfolio.json"
LEGACY_WATCHLIST_FILE = "watchlist.json"
STRATEGY_STORAGE_DIR = "strategy_states"
TELEGRAM_UI_STATE_FILE = "telegram_ui_state.json"

CHECK_INTERVAL_SECONDS = 60
IMPORTANT_LOG_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
TELEGRAM_POLL_TIMEOUT_SECONDS = 20
TELEGRAM_POLL_RETRY_SECONDS = 3
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 20

TELEGRAM_MENU_BUTTON_STATS = "📊 Статистика"
TELEGRAM_MENU_BUTTON_ACTIVE_BETS = "📂 Активные сделки"
TELEGRAM_MENU_BUTTON_ACTIONS = "🧾 Действия"
TELEGRAM_MENU_BUTTON_SELECT_STRATEGY = "🧠 Выбрать стратегию"


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str
    title: str
    starting_balance: float
    bet_amount: float
    min_price: float
    max_price: float
    watchlist_min_price: float
    watchlist_max_price: float
    max_days_to_expiry: int
    take_profit_multiplier: float
    scanner_threshold: float
    min_liquidity: float
    min_volume: float
    safe_exit_minutes: int
    max_drop_percent: float
    max_price_inclusive: bool = True
    stop_loss_ratio: float | None = None
    ranking_mode: str = "volume_plus_liquidity_x2"
    top_capitalization_fraction: float = 1.0


STRATEGY_CONFIGS = [
    StrategyConfig(
        strategy_id="cheap_liquidity_v1",
        title="Cheap Liquidity v1",
        starting_balance=100.0,
        bet_amount=1.0,
        min_price=0.005,
        max_price=0.02,
        max_price_inclusive=True,
        watchlist_min_price=0.021,
        watchlist_max_price=0.08,
        max_days_to_expiry=30,
        take_profit_multiplier=2.0,
        scanner_threshold=5.0,
        min_liquidity=500.0,
        min_volume=1000.0,
        safe_exit_minutes=4,
        max_drop_percent=0.90,
    ),
    StrategyConfig(
        strategy_id="volume_to_cap_top30_v1",
        title="Volume/Cap Top30 v1",
        starting_balance=100.0,
        bet_amount=1.0,
        min_price=0.005,
        max_price=0.02,
        max_price_inclusive=False,
        watchlist_min_price=0.021,
        watchlist_max_price=0.08,
        max_days_to_expiry=30,
        take_profit_multiplier=2.5,
        scanner_threshold=5.0,
        min_liquidity=500.0,
        min_volume=1000.0,
        safe_exit_minutes=4,
        max_drop_percent=0.90,
        stop_loss_ratio=0.5,
        ranking_mode="volume_div_capitalization",
        top_capitalization_fraction=0.30,
    ),
    StrategyConfig(
        strategy_id="volume_liquidity_ratio_v1",
        title="Volume/Liquidity v1",
        starting_balance=100.0,
        bet_amount=1.0,
        min_price=0.005,
        max_price=0.02,
        max_price_inclusive=False,
        watchlist_min_price=0.021,
        watchlist_max_price=0.08,
        max_days_to_expiry=30,
        take_profit_multiplier=2.0,
        scanner_threshold=5.0,
        min_liquidity=500.0,
        min_volume=1000.0,
        safe_exit_minutes=4,
        max_drop_percent=0.90,
        stop_loss_ratio=0.5,
        ranking_mode="volume_div_liquidity",
        top_capitalization_fraction=0.50,
    ),
    StrategyConfig(
        strategy_id="volume_sqrt_liquidity_v1",
        title="Volume/SqrtLiq v1",
        starting_balance=100.0,
        bet_amount=1.0,
        min_price=0.005,
        max_price=0.02,
        max_price_inclusive=False,
        watchlist_min_price=0.021,
        watchlist_max_price=0.08,
        max_days_to_expiry=30,
        take_profit_multiplier=2.0,
        scanner_threshold=5.0,
        min_liquidity=500.0,
        min_volume=1000.0,
        safe_exit_minutes=4,
        max_drop_percent=0.90,
        stop_loss_ratio=0.5,
        ranking_mode="volume_div_sqrt_liquidity",
        top_capitalization_fraction=0.50,
    ),
    StrategyConfig(
        strategy_id="inverse_price_momentum_v1",
        title="Inverse Price Momentum v1",
        starting_balance=100.0,
        bet_amount=1.0,
        min_price=0.005,
        max_price=0.02,
        max_price_inclusive=False,
        watchlist_min_price=0.021,
        watchlist_max_price=0.08,
        max_days_to_expiry=30,
        take_profit_multiplier=2.5,
        scanner_threshold=5.0,
        min_liquidity=500.0,
        min_volume=1000.0,
        safe_exit_minutes=4,
        max_drop_percent=0.90,
        stop_loss_ratio=0.5,
        ranking_mode="volume_x_inverse_price",
        top_capitalization_fraction=0.40,
    ),
    StrategyConfig(
        strategy_id="balanced_log_flow_v1",
        title="Balanced Log Flow v1",
        starting_balance=100.0,
        bet_amount=1.0,
        min_price=0.005,
        max_price=0.02,
        max_price_inclusive=False,
        watchlist_min_price=0.021,
        watchlist_max_price=0.08,
        max_days_to_expiry=30,
        take_profit_multiplier=2.0,
        scanner_threshold=5.0,
        min_liquidity=500.0,
        min_volume=1000.0,
        safe_exit_minutes=4,
        max_drop_percent=0.90,
        stop_loss_ratio=0.5,
        ranking_mode="log_volume_x_log_liquidity",
        top_capitalization_fraction=0.50,
    ),
]
STRATEGY_BY_ID = {config.strategy_id: config for config in STRATEGY_CONFIGS}
DEFAULT_STRATEGY_ID = STRATEGY_CONFIGS[0].strategy_id


session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
)

telegram_bridge = None


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


def parse_admin_ids():
    raw_value = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
    if not raw_value:
        raw_value = os.getenv("ADMIN_ID", "").strip()

    admin_ids = []
    for part in raw_value.split(","):
        clean_part = part.strip()
        if not clean_part:
            continue
        try:
            admin_ids.append(int(clean_part))
        except ValueError:
            print(f"[WARN] Skipping invalid Telegram admin id: {clean_part}", file=sys.stderr)
    return admin_ids


def load_json(file_path, default_data):
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default_data


def save_json(file_path, data):
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def ensure_storage_dir():
    os.makedirs(STRATEGY_STORAGE_DIR, exist_ok=True)


def default_strategy_state(config):
    return {
        "strategy_id": config.strategy_id,
        "title": config.title,
        "balance": config.starting_balance,
        "active_bets": [],
        "history": [],
        "watchlist": [],
    }


def get_strategy_state_path(strategy_id):
    return os.path.join(STRATEGY_STORAGE_DIR, f"{strategy_id}.json")


def migrate_legacy_state_if_needed(config):
    portfolio_exists = os.path.exists(LEGACY_PORTFOLIO_FILE)
    watchlist_exists = os.path.exists(LEGACY_WATCHLIST_FILE)
    if config.strategy_id != DEFAULT_STRATEGY_ID or (not portfolio_exists and not watchlist_exists):
        return default_strategy_state(config)

    state = default_strategy_state(config)
    legacy_portfolio = load_json(
        LEGACY_PORTFOLIO_FILE,
        {
            "balance": config.starting_balance,
            "active_bets": [],
            "history": [],
        },
    )
    state["balance"] = float(legacy_portfolio.get("balance", config.starting_balance))
    state["active_bets"] = legacy_portfolio.get("active_bets", [])
    state["history"] = legacy_portfolio.get("history", [])
    state["watchlist"] = load_json(LEGACY_WATCHLIST_FILE, [])
    return state


def load_strategy_state(config):
    ensure_storage_dir()
    path = get_strategy_state_path(config.strategy_id)
    if os.path.exists(path):
        state = load_json(path, default_strategy_state(config))
    else:
        state = migrate_legacy_state_if_needed(config)
        save_json(path, state)

    state.setdefault("strategy_id", config.strategy_id)
    state.setdefault("title", config.title)
    state.setdefault("balance", config.starting_balance)
    state.setdefault("active_bets", [])
    state.setdefault("history", [])
    state.setdefault("watchlist", [])
    return state


def save_strategy_state(config, state):
    ensure_storage_dir()
    save_json(get_strategy_state_path(config.strategy_id), state)


def load_telegram_ui_state():
    return load_json(TELEGRAM_UI_STATE_FILE, {"selected_strategy_by_chat": {}})


def save_telegram_ui_state(state):
    save_json(TELEGRAM_UI_STATE_FILE, state)


def get_strategy_prefix(config):
    return f"[{config.title}]"


def log(message, level="INFO", notify=False, strategy=None):
    time_str = datetime.now().strftime("%H:%M:%S")
    strategy_prefix = f"{get_strategy_prefix(strategy)} " if strategy else ""
    console_line = f"[{time_str}] [{level}] {strategy_prefix}{message}"
    print(console_line)

    should_notify = notify or level in IMPORTANT_LOG_LEVELS
    if should_notify and telegram_bridge and telegram_bridge.is_enabled():
        try:
            telegram_bridge.notify_admins(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {strategy_prefix}{message}",
                disable_notification=(level == "INFO"),
            )
        except Exception as error:
            print(f"[Telegram] Notification error: {error}", file=sys.stderr)


def get_balance_snapshot(state):
    locked = sum(
        float(bet.get("current_value", bet.get("cost", 0.0)))
        for bet in state.get("active_bets", [])
    )
    free = float(state.get("balance", 0.0))
    total = free + locked
    return f"free ${free:.2f} | locked ${locked:.2f} | total ${total:.2f}"


def get_trade_result_snapshot(cost, payout):
    profit = payout - cost
    profit_percent = (profit / cost * 100.0) if cost else 0.0
    sign = "+" if profit >= 0 else ""
    return (
        f"Invested: ${cost:.2f} | Received: ${payout:.2f} | "
        f"PnL: {sign}${profit:.2f} ({sign}{profit_percent:.1f}%)"
    )


def get_market_capitalization_proxy(market):
    try:
        liquidity = float(market.get("liquidity", 0))
        return liquidity
    except Exception:
        return 0.0


def get_market_score(config, market, price=None):
    try:
        volume = float(market.get("volume", 0))
        liquidity = float(market.get("liquidity", 0))
        capitalization = get_market_capitalization_proxy(market)
        safe_price = max(float(price or 0.0), 0.0001)

        if config.ranking_mode == "volume_div_capitalization":
            if capitalization <= 0:
                return 0.0
            return volume / capitalization
        if config.ranking_mode == "volume_div_liquidity":
            return volume / max(liquidity, 1.0)
        if config.ranking_mode == "volume_div_sqrt_liquidity":
            return volume / max(math.sqrt(liquidity), 1.0)
        if config.ranking_mode == "log_volume_x_log_liquidity":
            return math.log(volume + 1.0) * math.log(liquidity + 1.0)
        if config.ranking_mode == "volume_x_inverse_price":
            return volume * (1.0 - safe_price)
        if config.ranking_mode == "volume_div_price":
            return volume / safe_price
        if config.ranking_mode == "volume_div_liquidity_div_price":
            return (volume / max(liquidity, 1.0)) / safe_price
        if config.ranking_mode == "volume_x_liquidity":
            return volume * liquidity

        return volume + (liquidity * 2.0)
    except Exception:
        return 0.0


def filter_markets_by_capitalization(config, markets):
    if not markets:
        return []

    if config.top_capitalization_fraction >= 1.0:
        return markets

    markets_sorted = sorted(
        markets,
        key=lambda market: get_market_capitalization_proxy(market),
        reverse=True,
    )
    keep_count = max(1, int(len(markets_sorted) * config.top_capitalization_fraction))
    threshold = get_market_capitalization_proxy(markets_sorted[keep_count - 1])
    return [
        market
        for market in markets
        if get_market_capitalization_proxy(market) >= threshold
    ]


def price_matches_strategy(config, price):
    if price < config.min_price:
        return False
    if config.max_price_inclusive:
        return price <= config.max_price
    return price < config.max_price


def action_emoji(status):
    return {
        "WON": "✅",
        "SOLD_PROFIT": "🤑",
        "SOLD_SAFE": "🛡️",
        "LOST": "❌",
        "STOP_LOSS": "⛔",
    }.get(status, "•")


def build_status_message(config, state):
    balance = float(state.get("balance", 0.0))
    active_bets = state.get("active_bets", [])
    history = state.get("history", [])
    locked = sum(float(bet.get("current_value", bet.get("cost", 1.0))) for bet in active_bets)
    total = balance + locked
    net_profit = total - config.starting_balance

    won_count = 0
    lost_count = 0
    tp_count = 0
    safe_count = 0
    stop_loss_count = 0

    for bet in history:
        status = bet.get("status", "")
        if status == "WON":
            won_count += 1
        elif status == "LOST":
            lost_count += 1
        elif status == "SOLD_PROFIT":
            tp_count += 1
        elif status == "SOLD_SAFE":
            safe_count += 1
        elif status == "STOP_LOSS":
            stop_loss_count += 1

    total_closed = len(history)
    profitable_closed = won_count + tp_count
    win_rate = (profitable_closed / total_closed * 100.0) if total_closed else 0.0

    lines = [
        f"📊 {config.title}",
        f"🕒 {datetime.now().strftime('%H:%M:%S')}",
        "",
        f"💰 Free balance: ${balance:.2f}",
        f"🔒 Locked: ${locked:.2f}",
        f"💵 Total Assets: ${total:.2f}",
        f"📈 Net Profit: ${net_profit:.2f}",
        "",
        f"🔄 Active: {len(active_bets)} | 📊 Closed: {total_closed}",
        f"🎯 Win Rate: {win_rate:.1f}%",
        (
            f"✅WON:{won_count} | 🤑TP:{tp_count} | 🛡️SAFE:{safe_count} | "
            f"⛔SL:{stop_loss_count} | ❌LOST:{lost_count}"
        ),
    ]
    return "\n".join(lines)


def build_active_bets_message(config, state):
    active_bets = state.get("active_bets", [])
    lines = [
        f"📂 Active Bets | {config.title}",
        f"🕒 {datetime.now().strftime('%H:%M:%S')}",
        "",
    ]

    if not active_bets:
        lines.append("No active bets.")
        return "\n".join(lines)

    for index, bet in enumerate(active_bets, start=1):
        question = bet.get("question", "Unknown market").replace("\n", " ").strip()
        outcome = bet.get("outcome", "Unknown outcome")
        buy_price = float(bet.get("buy_price", 0.0))
        current_price = float(bet.get("current_price", buy_price))
        shares = float(bet.get("shares", 0.0))
        current_value = float(bet.get("current_value", bet.get("cost", 0.0)))
        status = bet.get("status", "ACTIVE")

        lines.append(f"{index}. {question[:100]}")
        lines.append(
            f"   Outcome: {outcome} | Buy: ${buy_price:.4f} | Now: ${current_price:.4f}"
        )
        lines.append(
            f"   Shares: {shares:.2f} | Value: ${current_value:.2f} | Status: {status}"
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def build_actions_message(config, state, limit=20):
    history = state.get("history", [])
    lines = [
        f"🧾 Actions | {config.title}",
        f"🕒 {datetime.now().strftime('%H:%M:%S')}",
        "",
    ]

    if not history:
        lines.append("No completed actions yet.")
        return "\n".join(lines)

    recent_history = history[-limit:]
    for bet in reversed(recent_history):
        status = bet.get("status", "UNKNOWN")
        question = bet.get("question", "Unknown market").replace("\n", " ").strip()
        payout = float(bet.get("payout", 0.0))
        cost = float(bet.get("cost", 0.0))
        close_date = bet.get("close_date", bet.get("date", ""))
        lines.append(
            f"{action_emoji(status)} {status} | {question[:90]}"
        )
        lines.append(f"   {get_trade_result_snapshot(cost, payout)}")
        if close_date:
            lines.append(f"   Closed: {close_date}")
        lines.append("")

    return "\n".join(lines).rstrip()


def build_strategy_selection_message():
    lines = [
        "🧠 Strategy selection",
        "",
        "Choose the strategy you want to inspect:",
    ]
    for index, config in enumerate(STRATEGY_CONFIGS, start=1):
        lines.append(f"{index}. {config.title}")
    return "\n".join(lines)


class MarketDataProvider:
    def __init__(self, http_session):
        self.http_session = http_session

    def fetch_open_events(self):
        events = []
        seen_event_ids = set()
        limit = 100
        offset = 0

        while True:
            response = self.http_session.get(
                API_URL,
                params={"closed": "false", "limit": limit, "offset": offset},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload:
                break

            for event in payload:
                event_id = event.get("id")
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
                events.append(event)

            offset += limit

        return events

    def fetch_event_by_id(self, event_id):
        response = self.http_session.get(f"{API_URL}?id={event_id}", timeout=30)
        response.raise_for_status()
        payload = response.json()
        return payload[0] if payload else None

    def build_snapshot(self, extra_event_ids=None):
        events = self.fetch_open_events()
        events_by_id = {event["id"]: event for event in events if event.get("id")}

        for event_id in extra_event_ids or []:
            if event_id in events_by_id:
                continue
            try:
                event = self.fetch_event_by_id(event_id)
            except Exception:
                event = None
            if event:
                events_by_id[event_id] = event

        return {"events": events, "events_by_id": events_by_id}


def update_strategy_positions(config, state, snapshot):
    active_bets = state.get("active_bets", [])
    if not active_bets:
        return

    log("Checking positions and updating prices...", strategy=config)
    still_active = []
    now = datetime.now(timezone.utc)
    events_by_id = snapshot["events_by_id"]

    for bet in active_bets:
        try:
            event = events_by_id.get(bet["event_id"])
            if not event:
                still_active.append(bet)
                continue

            market = next(
                (item for item in event.get("markets", []) if item.get("id") == bet["market_id"]),
                None,
            )
            if not market:
                still_active.append(bet)
                continue

            if market.get("closed"):
                tokens_resolved = market.get("tokensResolved", [])
                if not tokens_resolved:
                    bet["status"] = "W8_TO_RESOLVE"
                    still_active.append(bet)
                    continue

                if str(tokens_resolved[bet["outcome_index"]]) == "1":
                    payout = bet["shares"] * 1.0
                    cost = float(bet.get("cost", 0.0))
                    state["balance"] += payout
                    log(
                        (
                            f"✅ WON +${payout:.2f} | {market['question'][:80]}\n"
                            f"{get_trade_result_snapshot(cost, payout)}\n"
                            f"Balance: {get_balance_snapshot(state)}"
                        ),
                        notify=True,
                        strategy=config,
                    )
                    bet["status"] = "WON"
                    bet["payout"] = payout
                else:
                    log(
                        f"❌ LOST | {market['question'][:80]}",
                        level="WARNING",
                        notify=True,
                        strategy=config,
                    )
                    bet["status"] = "LOST"
                    bet["payout"] = 0.0

                bet["close_date"] = datetime.now().isoformat()
                state["history"].append(bet)
                continue

            prices = json.loads(market.get("outcomePrices", "[]"))
            if len(prices) <= bet["outcome_index"]:
                still_active.append(bet)
                continue

            current_price = float(prices[bet["outcome_index"]])
            bet["current_price"] = current_price
            bet["current_value"] = current_price * bet["shares"]

            event_end_date = None
            end_date_str = event.get("endDate")
            if end_date_str:
                event_end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

            if event_end_date and now >= event_end_date:
                bet["status"] = "W8_TO_RESOLVE"
                still_active.append(bet)
                continue

            if config.stop_loss_ratio is not None:
                stop_loss_price = bet["buy_price"] * config.stop_loss_ratio
                if current_price <= stop_loss_price:
                    payout = bet["shares"] * current_price
                    cost = float(bet.get("cost", 0.0))
                    state["balance"] += payout
                    log(
                        (
                            f"⛔ STOP LOSS ${bet['buy_price']:.4f} -> ${current_price:.4f} | "
                            f"{market['question'][:80]}\n"
                            f"{get_trade_result_snapshot(cost, payout)}\n"
                            f"Balance: {get_balance_snapshot(state)}"
                        ),
                        level="WARNING",
                        notify=True,
                        strategy=config,
                    )
                    bet["status"] = "STOP_LOSS"
                    bet["sell_price"] = current_price
                    bet["payout"] = payout
                    bet["close_date"] = datetime.now().isoformat()
                    state["history"].append(bet)
                    continue

            target_price = bet["buy_price"] * config.take_profit_multiplier
            if current_price >= target_price:
                payout = bet["shares"] * current_price
                cost = float(bet.get("cost", 0.0))
                state["balance"] += payout
                log(
                    (
                        f"✅ TAKE PROFIT ${bet['buy_price']:.4f} -> ${current_price:.4f} | "
                        f"{market['question'][:80]}\n"
                        f"{get_trade_result_snapshot(cost, payout)}\n"
                        f"Balance: {get_balance_snapshot(state)}"
                    ),
                    notify=True,
                    strategy=config,
                )
                bet["status"] = "SOLD_PROFIT"
                bet["sell_price"] = current_price
                bet["payout"] = payout
                bet["close_date"] = datetime.now().isoformat()
                state["history"].append(bet)
                continue

            if event_end_date:
                minutes_left = (event_end_date - now).total_seconds() / 60.0
                if 0 < minutes_left <= config.safe_exit_minutes:
                    drop_ratio = current_price / bet["buy_price"]
                    if drop_ratio >= (1.0 - config.max_drop_percent):
                        payout = bet["shares"] * current_price
                        cost = float(bet.get("cost", 0.0))
                        state["balance"] += payout
                        log(
                            (
                                f"🛡️ SAFE EXIT {minutes_left:.1f}m left, refund +${payout:.2f} | "
                                f"{market['question'][:80]}\n"
                                f"{get_trade_result_snapshot(cost, payout)}\n"
                                f"Balance: {get_balance_snapshot(state)}"
                            ),
                            notify=True,
                            strategy=config,
                        )
                        bet["status"] = "SOLD_SAFE"
                        bet["sell_price"] = current_price
                        bet["payout"] = payout
                        bet["close_date"] = datetime.now().isoformat()
                        state["history"].append(bet)
                        continue

            still_active.append(bet)
        except Exception as error:
            still_active.append(bet)
            log(
                f"Position check failed for market {bet.get('market_id')}: {error}",
                level="ERROR",
                strategy=config,
            )

    state["active_bets"] = still_active


def scan_and_execute_strategy(config, state, snapshot):
    log("Scanning shared market snapshot for new candidates...", strategy=config)
    now = datetime.now(timezone.utc)
    max_end_date = now + timedelta(days=config.max_days_to_expiry)
    min_end_date = now + timedelta(minutes=config.safe_exit_minutes)

    existing_market_ids = {
        bet["market_id"] for bet in state.get("active_bets", [])
    } | {
        bet["market_id"] for bet in state.get("history", [])
    }
    watchlist = state.get("watchlist", [])
    candidates = []
    eligible_markets = []

    for event in snapshot["events"]:
        end_date_str = event.get("endDate")
        if not end_date_str:
            continue

        try:
            clean_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        if clean_date <= min_end_date or clean_date > max_end_date:
            continue

        for market in event.get("markets", []):
            if market.get("closed") or market.get("id") in existing_market_ids:
                continue

            try:
                volume = float(market.get("volume", 0))
                liquidity = float(market.get("liquidity", 0))
            except Exception:
                continue

            if volume < config.min_volume or liquidity < config.min_liquidity:
                continue

            eligible_markets.append(market)

    eligible_market_ids = {
        market.get("id")
        for market in filter_markets_by_capitalization(config, eligible_markets)
        if market.get("id")
    }

    for event in snapshot["events"]:
        end_date_str = event.get("endDate")
        if not end_date_str:
            continue

        try:
            clean_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        if clean_date <= min_end_date or clean_date > max_end_date:
            continue

        for market in event.get("markets", []):
            if market.get("closed") or market.get("id") in existing_market_ids:
                continue
            if eligible_market_ids and market.get("id") not in eligible_market_ids:
                continue

            outcomes = market.get("outcomes", [])
            try:
                prices = json.loads(market.get("outcomePrices", "[]"))
            except Exception:
                continue

            for outcome_index, price_str in enumerate(prices):
                try:
                    price = float(price_str)
                except Exception:
                    continue

                if price_matches_strategy(config, price):
                    candidates.append(
                        {
                            "score": get_market_score(config, market, price),
                            "event": event,
                            "market": market,
                            "outcome_index": outcome_index,
                            "outcome": (
                                outcomes[outcome_index]
                                if outcome_index < len(outcomes)
                                else f"Outcome {outcome_index}"
                            ),
                            "price": price,
                        }
                    )
                elif config.watchlist_min_price <= price <= config.watchlist_max_price:
                    if not any(item["market_id"] == market["id"] for item in watchlist):
                        watchlist.append(
                            {
                                "event_id": event["id"],
                                "market_id": market["id"],
                                "question": market.get("question", "Unknown market"),
                                "outcome": (
                                    outcomes[outcome_index]
                                    if outcome_index < len(outcomes)
                                    else f"Outcome {outcome_index}"
                                ),
                                "tracked_price": price,
                                "date_added": datetime.now().isoformat(),
                            }
                        )

    state["watchlist"] = watchlist

    if not candidates:
        log("No suitable candidates found this cycle.", strategy=config)
        return

    candidates.sort(key=lambda item: item["score"], reverse=True)
    bought_count = 0

    for candidate in candidates:
        if state["balance"] < config.bet_amount:
            break

        price = candidate["price"]
        market = candidate["market"]
        shares = config.bet_amount / price

        state["balance"] -= config.bet_amount
        state["active_bets"].append(
            {
                "event_id": candidate["event"]["id"],
                "market_id": market["id"],
                "question": market.get("question", "Unknown market"),
                "outcome": candidate["outcome"],
                "outcome_index": candidate["outcome_index"],
                "buy_price": price,
                "current_price": price,
                "shares": shares,
                "cost": config.bet_amount,
                "current_value": config.bet_amount,
                "status": "ACTIVE",
                "date": datetime.now().isoformat(),
            }
        )
        bought_count += 1
        log(
            (
                f"BOUGHT top candidate (score {candidate['score']:.0f}) at ${price:.4f} | "
                f"{market['question'][:80]}\n"
                f"Balance: {get_balance_snapshot(state)}"
            ),
            notify=True,
            strategy=config,
        )

    log(f"Purchase cycle finished. Bought {bought_count} contracts.", strategy=config)


def print_strategy_stats(config, state):
    log(f"Balance: {get_balance_snapshot(state)}", strategy=config)
    log(f"Open positions: {len(state.get('active_bets', []))}", strategy=config)


def collect_required_event_ids(strategy_states):
    event_ids = set()
    for state in strategy_states.values():
        for bet in state.get("active_bets", []):
            event_ids.add(bet["event_id"])
    return event_ids


class TelegramBridge:
    def __init__(self, token, admin_ids):
        self.token = token.strip()
        self.admin_ids = sorted(set(int(admin_id) for admin_id in admin_ids))
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.ui_state = load_telegram_ui_state()

    def is_enabled(self):
        return bool(self.token and self.admin_ids)

    def api_get(self, method, params=None):
        response = self.session.get(
            f"{self.base_url}/{method}",
            params=params or {},
            timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS + TELEGRAM_POLL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload["result"]

    def api_post(self, method, data=None):
        response = self.session.post(
            f"{self.base_url}/{method}",
            data=data or {},
            timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload["result"]

    def split_message(self, text, limit=4000):
        if len(text) <= limit:
            return [text]

        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        return chunks

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=True):
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true",
            "disable_notification": "true" if disable_notification else "false",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        for chunk in self.split_message(text):
            chunk_payload = dict(payload)
            chunk_payload["text"] = chunk
            self.api_post("sendMessage", chunk_payload)

    def notify_admins(self, text, disable_notification=True):
        for admin_id in self.admin_ids:
            try:
                self.send_message(admin_id, text, disable_notification=disable_notification)
            except Exception as error:
                print(f"[Telegram] Failed to send message to {admin_id}: {error}", file=sys.stderr)

    def get_selected_strategy_id(self, chat_id):
        return self.ui_state.get("selected_strategy_by_chat", {}).get(str(chat_id))

    def set_selected_strategy_id(self, chat_id, strategy_id):
        self.ui_state.setdefault("selected_strategy_by_chat", {})[str(chat_id)] = strategy_id
        save_telegram_ui_state(self.ui_state)

    def get_selected_strategy(self, chat_id):
        strategy_id = self.get_selected_strategy_id(chat_id)
        if strategy_id and strategy_id in STRATEGY_BY_ID:
            return STRATEGY_BY_ID[strategy_id]
        return None

    def build_strategy_keyboard(self):
        keyboard = []
        current_row = []
        for config in STRATEGY_CONFIGS:
            current_row.append({"text": config.title})
            if len(current_row) == 2:
                keyboard.append(current_row)
                current_row = []
        if current_row:
            keyboard.append(current_row)
        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def build_actions_keyboard(self):
        return {
            "keyboard": [
                [{"text": TELEGRAM_MENU_BUTTON_STATS}, {"text": TELEGRAM_MENU_BUTTON_ACTIVE_BETS}],
                [{"text": TELEGRAM_MENU_BUTTON_ACTIONS}],
                [{"text": TELEGRAM_MENU_BUTTON_SELECT_STRATEGY}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def show_strategy_selector(self, chat_id):
        self.send_message(
            chat_id,
            build_strategy_selection_message(),
            reply_markup=self.build_strategy_keyboard(),
            disable_notification=True,
        )

    def show_actions_menu(self, chat_id, config):
        self.send_message(
            chat_id,
            (
                f"Selected strategy: {config.title}\n\n"
                "Now you can request stats, active bets, or completed actions."
            ),
            reply_markup=self.build_actions_keyboard(),
            disable_notification=True,
        )

    def send_strategy_status(self, chat_id, config):
        state = load_strategy_state(config)
        self.send_message(chat_id, build_status_message(config, state), disable_notification=True)

    def send_active_bets(self, chat_id, config):
        state = load_strategy_state(config)
        self.send_message(chat_id, build_active_bets_message(config, state), disable_notification=True)

    def send_actions(self, chat_id, config):
        state = load_strategy_state(config)
        self.send_message(chat_id, build_actions_message(config, state), disable_notification=True)

    def require_strategy_selection(self, chat_id):
        self.send_message(
            chat_id,
            "Choose a strategy first.",
            reply_markup=self.build_strategy_keyboard(),
            disable_notification=True,
        )

    def handle_text_message(self, message):
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if chat_id not in self.admin_ids:
            return

        text = (message.get("text") or "").strip()
        lowered = text.lower()

        selected_config = self.get_selected_strategy(chat_id)
        named_strategy = next(
            (config for config in STRATEGY_CONFIGS if lowered == config.title.lower()),
            None,
        )

        if lowered in {"/start", "/menu", "menu", TELEGRAM_MENU_BUTTON_SELECT_STRATEGY.lower()}:
            self.show_strategy_selector(chat_id)
            return

        if named_strategy:
            self.set_selected_strategy_id(chat_id, named_strategy.strategy_id)
            self.show_actions_menu(chat_id, named_strategy)
            return

        if lowered in {"/help", "help"}:
            self.send_message(
                chat_id,
                (
                    "Flow:\n"
                    "1. Open /menu\n"
                    "2. Choose a strategy\n"
                    "3. Ask for stats, active bets, or actions\n\n"
                    "Commands:\n"
                    "/menu\n/status\n/active\n/actions\n/help"
                ),
                disable_notification=True,
            )
            return

        if not selected_config:
            self.require_strategy_selection(chat_id)
            return

        if lowered in {"/status", "status", TELEGRAM_MENU_BUTTON_STATS.lower()}:
            self.send_strategy_status(chat_id, selected_config)
        elif lowered in {
            "/active",
            "/positions",
            "active",
            "positions",
            TELEGRAM_MENU_BUTTON_ACTIVE_BETS.lower(),
        }:
            self.send_active_bets(chat_id, selected_config)
        elif lowered in {"/actions", "actions", TELEGRAM_MENU_BUTTON_ACTIONS.lower()}:
            self.send_actions(chat_id, selected_config)

    def poll_loop(self):
        while not self.stop_event.is_set():
            try:
                updates = self.api_get(
                    "getUpdates",
                    {
                        "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS,
                        "offset": self.offset,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                for update in updates:
                    self.offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        self.handle_text_message(message)
            except Exception as error:
                print(f"[Telegram] Polling error: {error}", file=sys.stderr)
                self.stop_event.wait(TELEGRAM_POLL_RETRY_SECONDS)

    def start(self):
        if not self.is_enabled() or self.thread is not None:
            return

        self.thread = threading.Thread(target=self.poll_loop, name="telegram-bot", daemon=True)
        self.thread.start()
        self.notify_admins(
            "Polymarket bot is online. Use /menu to choose a strategy.",
            disable_notification=True,
        )

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.thread = None


def run_bot_cycle():
    log("-" * 50)
    strategy_states = {
        config.strategy_id: load_strategy_state(config)
        for config in STRATEGY_CONFIGS
    }

    provider = MarketDataProvider(session)
    active_event_ids = collect_required_event_ids(strategy_states)
    snapshot = provider.build_snapshot(extra_event_ids=active_event_ids)
    log(
        f"Shared market snapshot ready: {len(snapshot['events'])} open events, "
        f"{len(snapshot['events_by_id'])} cached event records."
    )

    for config in STRATEGY_CONFIGS:
        state = strategy_states[config.strategy_id]
        update_strategy_positions(config, state, snapshot)
        print_strategy_stats(config, state)

        if state["balance"] >= config.scanner_threshold:
            scan_and_execute_strategy(config, state, snapshot)
        else:
            log(
                f"Free balance is below ${config.scanner_threshold:.2f}. Waiting for a larger pool.",
                strategy=config,
            )

        save_strategy_state(config, state)

    log("-" * 50)


def configure_telegram():
    global telegram_bridge

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin_ids = parse_admin_ids()

    if not token:
        log("TELEGRAM_BOT_TOKEN is not set. Telegram logging is disabled.")
        return

    if not admin_ids:
        log(
            "Telegram token found, but no admin ids configured. "
            "Set TELEGRAM_ADMIN_IDS=123456789,987654321 in .env",
            level="WARNING",
        )
        return

    telegram_bridge = TelegramBridge(token, admin_ids)
    telegram_bridge.start()
    log(f"Telegram bridge enabled for {len(admin_ids)} admin(s).")


def main():
    load_env_file()

    print(
        r"""
     ___      _          ___       _
    | _ ) ___| |_       | _ ) ___ | |_
    | _ \/ _ \  _|  _   | _ \/ _ \|  _|
    |___/\___/\__| (_)  |___/\___/ \__|
    """
    )

    configure_telegram()
    strategy_titles = ", ".join(config.title for config in STRATEGY_CONFIGS)
    log(f"Starting Polymarket bot with strategies: {strategy_titles}", notify=True)

    try:
        while True:
            run_bot_cycle()
            log(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds...")
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print()
        log("Bot stopped by user.", notify=True)
        if telegram_bridge:
            telegram_bridge.stop()
        sys.exit(0)
    except Exception:
        print("\n[!] ERROR:")
        traceback_text = traceback.format_exc()
        print(traceback_text)
        log(f"Fatal error:\n{traceback_text}", level="CRITICAL", notify=True)
        if telegram_bridge:
            telegram_bridge.stop()
        raise


if __name__ == "__main__":
    main()
