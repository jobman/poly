import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

# --- Strategy settings ---
STARTING_BALANCE = 100.0
BET_AMOUNT = 1.0
MIN_PRICE = 0.005
MAX_PRICE = 0.02

# Watchlist settings
WATCHLIST_MIN_PRICE = 0.021
WATCHLIST_MAX_PRICE = 0.08

# Time and profit settings
MAX_DAYS_TO_EXPIRY = 30
TAKE_PROFIT_MULTIPLIER = 2.0
CHECK_INTERVAL_SECONDS = 60
SCANNER_THRESHOLD = 5.0

# Market quality settings
MIN_LIQUIDITY = 500.0
MIN_VOLUME = 1000.0

# Safe exit settings
SAFE_EXIT_MINUTES = 4
MAX_DROP_PERCENT = 0.90

PORTFOLIO_FILE = "portfolio.json"
WATCHLIST_FILE = "watchlist.json"
API_URL = "https://gamma-api.polymarket.com/events"
ENV_FILE = ".env"

IMPORTANT_LOG_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
TELEGRAM_POLL_TIMEOUT_SECONDS = 20
TELEGRAM_POLL_RETRY_SECONDS = 3
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 20
TELEGRAM_MENU_BUTTON_STATS = "📊 Статистика"
TELEGRAM_MENU_BUTTON_ACTIVE_BETS = "📂 Активные сделки"

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
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


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
            print(
                f"[WARN] Skipping invalid Telegram admin id: {clean_part}",
                file=sys.stderr,
            )
    return admin_ids


def load_json(file_path, default_data):
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default_data


def save_json(file_path, data):
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def load_portfolio():
    return load_json(
        PORTFOLIO_FILE,
        {"balance": STARTING_BALANCE, "active_bets": [], "history": []},
    )


def save_portfolio(portfolio):
    save_json(PORTFOLIO_FILE, portfolio)


def load_watchlist():
    return load_json(WATCHLIST_FILE, [])


def save_watchlist(watchlist):
    save_json(WATCHLIST_FILE, watchlist)


def build_status_message():
    portfolio = load_portfolio()
    balance = float(portfolio.get("balance", 0.0))
    active_bets = portfolio.get("active_bets", [])
    history = portfolio.get("history", [])

    locked = sum(float(bet.get("current_value", bet.get("cost", 1.0))) for bet in active_bets)
    total = balance + locked
    net_profit = total - STARTING_BALANCE

    won_count = 0
    lost_count = 0
    tp_count = 0
    safe_count = 0

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

    total_closed = len(history)
    profitable_closed = won_count + tp_count
    win_rate = (profitable_closed / total_closed * 100.0) if total_closed else 0.0

    lines = [
        "📊 Polymarket Status",
        f"🕒 {datetime.now().strftime('%H:%M:%S')}",
        "",
        f"💰 Free balance: ${balance:.2f}",
        f"🔒 Locked: ${locked:.2f}",
        f"💵 Total Assets: ${total:.2f}",
        f"📈 Net Profit: ${net_profit:.2f}",
        "",
        f"🔄 Active: {len(active_bets)} | 📊 Closed: {total_closed}",
        f"🎯 Win Rate: {win_rate:.1f}%",
        f"✅WON:{won_count} | 🤑TP:{tp_count} | 🛡️SAFE:{safe_count} | ❌LOST:{lost_count}",
    ]
    return "\n".join(lines)


def build_active_bets_message():
    portfolio = load_portfolio()
    active_bets = portfolio.get("active_bets", [])

    lines = [
        "📂 Active Bets",
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
            (
                f"   Outcome: {outcome} | Buy: ${buy_price:.4f} | "
                f"Now: ${current_price:.4f}"
            )
        )
        lines.append(
            f"   Shares: {shares:.2f} | Value: ${current_value:.2f} | Status: {status}"
        )
        lines.append("")

    return "\n".join(lines).rstrip()


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
        if not self.is_enabled():
            return

        for admin_id in self.admin_ids:
            try:
                self.send_message(
                    admin_id,
                    text,
                    disable_notification=disable_notification,
                )
            except Exception as error:
                print(
                    f"[Telegram] Failed to send message to {admin_id}: {error}",
                    file=sys.stderr,
                )

    def answer_callback(self, callback_query_id, text):
        try:
            self.api_post(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": "false",
                },
            )
        except Exception as error:
            print(f"[Telegram] Failed to answer callback: {error}", file=sys.stderr)

    def show_menu(self, chat_id):
        reply_markup = {
            "keyboard": [
                [
                    {
                        "text": TELEGRAM_MENU_BUTTON_STATS,
                    },
                    {
                        "text": TELEGRAM_MENU_BUTTON_ACTIVE_BETS,
                    }
                ]
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }
        self.send_message(
            chat_id,
            "Bot menu\n\nChoose an action from the keyboard below.",
            reply_markup=reply_markup,
            disable_notification=True,
        )

    def handle_text_message(self, message):
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if chat_id not in self.admin_ids:
            return

        text = (message.get("text") or "").strip().lower()
        if text in {"/start", "/menu", "menu"}:
            self.show_menu(chat_id)
        elif text in {"/status", "status", TELEGRAM_MENU_BUTTON_STATS.lower()}:
            self.send_message(
                chat_id,
                build_status_message(),
                disable_notification=True,
            )
        elif text in {
            "/active",
            "/positions",
            "active",
            "positions",
            TELEGRAM_MENU_BUTTON_ACTIVE_BETS.lower(),
        }:
            self.send_message(
                chat_id,
                build_active_bets_message(),
                disable_notification=True,
            )
        elif text in {"/help", "help"}:
            self.send_message(
                chat_id,
                (
                    "Available commands:\n"
                    "/menu\n"
                    "/status\n"
                    "/active\n"
                    "/help\n\n"
                    f"Buttons: {TELEGRAM_MENU_BUTTON_STATS}, "
                    f"{TELEGRAM_MENU_BUTTON_ACTIVE_BETS}"
                ),
                disable_notification=True,
            )

    def handle_callback_query(self, callback_query):
        user = callback_query.get("from") or {}
        user_id = int(user.get("id", 0))
        if user_id not in self.admin_ids:
            self.answer_callback(callback_query["id"], "Access denied")
            return

        data = callback_query.get("data", "")
        if data == "placeholder_menu":
            self.answer_callback(callback_query["id"], "Menu placeholder")

    def handle_update(self, update):
        message = update.get("message")
        if message:
            self.handle_text_message(message)

        callback_query = update.get("callback_query")
        if callback_query:
            self.handle_callback_query(callback_query)

    def poll_loop(self):
        while not self.stop_event.is_set():
            try:
                updates = self.api_get(
                    "getUpdates",
                    {
                        "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS,
                        "offset": self.offset,
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    },
                )

                for update in updates:
                    self.offset = update["update_id"] + 1
                    self.handle_update(update)
            except Exception as error:
                print(f"[Telegram] Polling error: {error}", file=sys.stderr)
                self.stop_event.wait(TELEGRAM_POLL_RETRY_SECONDS)

    def start(self):
        if not self.is_enabled() or self.thread is not None:
            return

        self.thread = threading.Thread(
            target=self.poll_loop,
            name="telegram-control-bot",
            daemon=True,
        )
        self.thread.start()
        self.notify_admins(
            "Polymarket bot is online. Use /menu to open the control menu.",
            disable_notification=True,
        )

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.thread = None


def log(message, level="INFO", notify=False):
    time_str = datetime.now().strftime("%H:%M:%S")
    console_line = f"[{time_str}] [{level}] {message}"
    print(console_line)

    should_notify = notify or level in IMPORTANT_LOG_LEVELS
    if should_notify and telegram_bridge and telegram_bridge.is_enabled():
        try:
            telegram_bridge.notify_admins(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {message}",
                disable_notification=(level == "INFO"),
            )
        except Exception as error:
            print(f"[Telegram] Notification error: {error}", file=sys.stderr)


def print_stats(portfolio):
    locked = sum(bet.get("current_value", bet["cost"]) for bet in portfolio["active_bets"])
    free = portfolio["balance"]
    total = free + locked
    log(
        f"Balance: free ${free:.2f} | locked (market value) ${locked:.2f} | total ${total:.2f}"
    )
    log(f"Open positions: {len(portfolio['active_bets'])}")


def get_balance_snapshot(portfolio):
    locked = sum(bet.get("current_value", bet["cost"]) for bet in portfolio["active_bets"])
    free = portfolio["balance"]
    total = free + locked
    return (
        f"free ${free:.2f} | locked ${locked:.2f} | total ${total:.2f}"
    )


def check_portfolio(portfolio):
    if not portfolio["active_bets"]:
        return

    log("Checking positions and updating prices...")
    active_bets = portfolio["active_bets"]
    still_active = []
    now = datetime.now(timezone.utc)

    for bet in active_bets:
        try:
            response = session.get(f"{API_URL}?id={bet['event_id']}", timeout=30)
            if response.status_code == 200 and len(response.json()) > 0:
                event = response.json()[0]
                market = next(
                    (item for item in event.get("markets", []) if item["id"] == bet["market_id"]),
                    None,
                )

                if market:
                    if market.get("closed"):
                        tokens_resolved = market.get("tokensResolved", [])
                        if not tokens_resolved:
                            bet["status"] = "W8_TO_RESOLVE"
                            still_active.append(bet)
                            continue

                        if str(tokens_resolved[bet["outcome_index"]]) == "1":
                            payout = bet["shares"] * 1.0
                            portfolio["balance"] += payout
                            log(
                                (
                                    f"✅ WON +${payout:.2f} | {market['question'][:80]}\n"
                                    f"Balance: {get_balance_snapshot(portfolio)}"
                                ),
                                notify=True,
                            )
                            bet["status"] = "WON"
                            bet["payout"] = payout
                        else:
                            log(
                                f"LOST | {market['question'][:80]}",
                                level="WARNING",
                                notify=True,
                            )
                            bet["status"] = "LOST"
                            bet["payout"] = 0

                        bet["close_date"] = datetime.now().isoformat()
                        portfolio["history"].append(bet)
                        continue

                    prices = json.loads(market.get("outcomePrices", "[]"))
                    if len(prices) > bet["outcome_index"]:
                        current_price = float(prices[bet["outcome_index"]])
                        bet["current_price"] = current_price
                        bet["current_value"] = current_price * bet["shares"]

                        event_end_date = None
                        end_date_str = event.get("endDate")
                        if end_date_str:
                            event_end_date = datetime.fromisoformat(
                                end_date_str.replace("Z", "+00:00")
                            )

                        if event_end_date and now >= event_end_date:
                            log(
                                f"Waiting for resolution after expiry | {market['question'][:80]}"
                            )
                            bet["status"] = "W8_TO_RESOLVE"
                            still_active.append(bet)
                            continue

                        target_price = bet["buy_price"] * TAKE_PROFIT_MULTIPLIER
                        if current_price >= target_price:
                            payout = bet["shares"] * current_price
                            portfolio["balance"] += payout
                            log(
                                (
                                    f"✅ TAKE PROFIT ${bet['buy_price']:.4f} -> "
                                    f"${current_price:.4f} | {market['question'][:80]}\n"
                                    f"Balance: {get_balance_snapshot(portfolio)}"
                                ),
                                notify=True,
                            )
                            bet["status"] = "SOLD_PROFIT"
                            bet["sell_price"] = current_price
                            bet["payout"] = payout
                            bet["close_date"] = datetime.now().isoformat()
                            portfolio["history"].append(bet)
                            continue

                        if event_end_date:
                            minutes_left = (event_end_date - now).total_seconds() / 60.0
                            if 0 < minutes_left <= SAFE_EXIT_MINUTES:
                                drop_ratio = current_price / bet["buy_price"]
                                if drop_ratio >= (1.0 - MAX_DROP_PERCENT):
                                    payout = bet["shares"] * current_price
                                    portfolio["balance"] += payout
                                    log(
                                        (
                                            f"✅ SAFE EXIT {minutes_left:.1f}m left, "
                                            f"refund +${payout:.2f} | {market['question'][:80]}\n"
                                            f"Balance: {get_balance_snapshot(portfolio)}"
                                        ),
                                        notify=True,
                                    )
                                    bet["status"] = "SOLD_SAFE"
                                    bet["sell_price"] = current_price
                                    bet["payout"] = payout
                                    bet["close_date"] = datetime.now().isoformat()
                                    portfolio["history"].append(bet)
                                    continue

            still_active.append(bet)
            time.sleep(0.2)
        except Exception as error:
            still_active.append(bet)
            log(
                f"Position check failed for market {bet.get('market_id')}: {error}",
                level="ERROR",
            )
            time.sleep(1.0)

    portfolio["active_bets"] = still_active
    save_portfolio(portfolio)


def get_market_score(market):
    try:
        volume = float(market.get("volume", 0))
        liquidity = float(market.get("liquidity", 0))
        return volume + (liquidity * 2)
    except Exception:
        return 0


def fetch_and_scan_all(portfolio):
    log("Scanning Polymarket for new candidates...")
    watchlist = load_watchlist()
    limit = 100
    offset = 0
    seen_event_ids = set()

    now = datetime.now(timezone.utc)
    max_end_date = now + timedelta(days=MAX_DAYS_TO_EXPIRY)
    min_end_date = now + timedelta(minutes=SAFE_EXIT_MINUTES)

    existing_market_ids = [bet["market_id"] for bet in portfolio["active_bets"]] + [
        bet["market_id"] for bet in portfolio["history"]
    ]

    candidates = []

    while True:
        params = {"closed": "false", "limit": limit, "offset": offset}
        try:
            response = session.get(API_URL, params=params, timeout=30)
            data = response.json()

            if not data:
                break

            for event in data:
                if event["id"] in seen_event_ids:
                    continue
                seen_event_ids.add(event["id"])

                end_date_str = event.get("endDate")
                if not end_date_str:
                    continue

                try:
                    clean_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if clean_date <= min_end_date or clean_date > max_end_date:
                        continue
                except Exception:
                    continue

                for market in event.get("markets", []):
                    if market.get("closed") or market["id"] in existing_market_ids:
                        continue

                    try:
                        volume = float(market.get("volume", 0))
                        liquidity = float(market.get("liquidity", 0))
                        if volume < MIN_VOLUME or liquidity < MIN_LIQUIDITY:
                            continue
                    except Exception:
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

                        if MIN_PRICE <= price <= MAX_PRICE:
                            candidates.append(
                                {
                                    "score": get_market_score(market),
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
                        elif WATCHLIST_MIN_PRICE <= price <= WATCHLIST_MAX_PRICE:
                            if not any(item["market_id"] == market["id"] for item in watchlist):
                                watchlist.append(
                                    {
                                        "event_id": event["id"],
                                        "market_id": market["id"],
                                        "question": market["question"],
                                        "outcome": (
                                            outcomes[outcome_index]
                                            if outcome_index < len(outcomes)
                                            else f"Outcome {outcome_index}"
                                        ),
                                        "tracked_price": price,
                                        "date_added": datetime.now().isoformat(),
                                    }
                                )

            offset += limit
            sys.stdout.write(
                f"\rScanned events: {len(seen_event_ids)} | candidates: {len(candidates)}"
            )
            sys.stdout.flush()
            time.sleep(0.2)
        except Exception as error:
            print(f"\nScan error: {error}")
            log(f"Market scan failed: {error}", level="ERROR")
            break

    print("\nScan complete. Starting purchases...")
    save_watchlist(watchlist)

    if candidates:
        candidates.sort(key=lambda item: item["score"], reverse=True)

        bought_count = 0
        for candidate in candidates:
            if portfolio["balance"] < BET_AMOUNT:
                break

            price = candidate["price"]
            market = candidate["market"]
            shares = BET_AMOUNT / price

            portfolio["balance"] -= BET_AMOUNT
            portfolio["active_bets"].append(
                {
                    "event_id": candidate["event"]["id"],
                    "market_id": market["id"],
                    "question": market["question"],
                    "outcome": candidate["outcome"],
                    "outcome_index": candidate["outcome_index"],
                    "buy_price": price,
                    "shares": shares,
                    "cost": BET_AMOUNT,
                    "current_value": BET_AMOUNT,
                    "date": datetime.now().isoformat(),
                }
            )
            bought_count += 1
            log(
                (
                    f"BOUGHT top candidate (score {candidate['score']:.0f}) "
                    f"at ${price:.4f} | {market['question'][:80]}\n"
                    f"Balance: {get_balance_snapshot(portfolio)}"
                ),
                notify=True,
            )

        save_portfolio(portfolio)
        log(f"Purchase cycle finished. Bought {bought_count} contracts.")
    else:
        log("No suitable candidates found this cycle.")


def run_bot():
    log("-" * 50)
    portfolio = load_portfolio()

    check_portfolio(portfolio)
    print_stats(portfolio)

    if portfolio["balance"] >= SCANNER_THRESHOLD:
        fetch_and_scan_all(portfolio)
    else:
        log(f"Free balance is below ${SCANNER_THRESHOLD:.2f}. Waiting for a larger pool.")

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
    log("Starting Polymarket bot. Press Ctrl+C to stop.", notify=True)

    try:
        while True:
            run_bot()
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
