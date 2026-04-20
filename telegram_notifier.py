import json
import os
import time
from datetime import datetime

import requests


PORTFOLIO_FILE = "portfolio.json"
SUBSCRIBERS_FILE = "telegram_chats.json"
HARDCODED_TELEGRAM_BOT_TOKEN = "8659875562:AAGcfy6ZdtVaJb7nCu-cdCoUmC36hGETG-Y"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
POLL_INTERVAL_SECONDS = int(os.getenv("TELEGRAM_POLL_INTERVAL_SECONDS", "5"))
STATUS_INTERVAL_SECONDS = int(os.getenv("TELEGRAM_STATUS_INTERVAL_SECONDS", "300"))
REQUEST_TIMEOUT_SECONDS = 20


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def load_portfolio():
    return load_json(PORTFOLIO_FILE, {"balance": 0.0, "active_bets": [], "history": []})


def load_subscribers():
    return load_json(SUBSCRIBERS_FILE, {"offset": 0, "chats": {}})


def save_subscribers(data):
    save_json(SUBSCRIBERS_FILE, data)


def build_status_message():
    portfolio = load_portfolio()
    balance = float(portfolio.get("balance", 0.0))
    active_bets = portfolio.get("active_bets", [])
    history = portfolio.get("history", [])

    locked = sum(float(bet.get("cost", 0.0)) for bet in active_bets)
    total = balance + locked
    won_count = sum(1 for bet in history if bet.get("status") == "WON")
    lost_count = sum(1 for bet in history if bet.get("status") == "LOST")
    sold_count = sum(1 for bet in history if bet.get("status") == "SOLD_EARLY")

    lines = [
        "Polymarket status",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Free balance: ${balance:.2f}",
        f"Locked in active bets: ${locked:.2f}",
        f"Total tracked bankroll: ${total:.2f}",
        f"Active bets: {len(active_bets)}",
        f"Closed bets: {len(history)}",
        f"WON: {won_count} | LOST: {lost_count} | SOLD_EARLY: {sold_count}",
    ]

    if active_bets:
        top_bets = active_bets[:5]
        lines.append("")
        lines.append("Recent active bets:")
        for bet in top_bets:
            question = bet.get("question", "Unknown market").replace("\n", " ").strip()
            lines.append(
                f"- ${float(bet.get('buy_price', 0.0)):.4f} | {question[:80]}"
            )

        remaining = len(active_bets) - len(top_bets)
        if remaining > 0:
            lines.append(f"... and {remaining} more")

    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, token):
        self.token = token
        self.base_url = TELEGRAM_API_BASE.format(token=token)
        self.state = load_subscribers()
        self.last_status_sent_at = 0.0

    def api_get(self, method, params=None):
        response = requests.get(
            f"{self.base_url}/{method}",
            params=params or {},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload["result"]

    def api_post(self, method, data=None):
        response = requests.post(
            f"{self.base_url}/{method}",
            data=data or {},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload["result"]

    def ensure_chat_registered(self, chat):
        chat_id = str(chat["id"])
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or chat_id
        if chat_id not in self.state["chats"]:
            self.state["chats"][chat_id] = {
                "title": title,
                "type": chat.get("type", "unknown"),
                "last_sent_status": "",
            }
            save_subscribers(self.state)
            log(f"Registered chat {title} ({chat_id})")

    def remove_chat(self, chat_id):
        if chat_id in self.state["chats"]:
            removed = self.state["chats"].pop(chat_id)
            save_subscribers(self.state)
            log(f"Removed chat {removed.get('title', chat_id)} ({chat_id})")

    def send_message(self, chat_id, text):
        try:
            self.api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": "true",
                },
            )
            return True
        except Exception as error:
            log(f"Send failed for {chat_id}: {error}")
            error_text = str(error)
            if "chat not found" in error_text or "bot was kicked" in error_text:
                self.remove_chat(str(chat_id))
            return False

    def handle_update(self, update):
        message = update.get("message") or update.get("channel_post")
        if not message:
            return

        chat = message.get("chat")
        if not chat:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        lowered = text.lower()
        if lowered in {"/start", "/subscribe", "/start_notifications"}:
            self.ensure_chat_registered(chat)
            self.send_message(
                chat["id"],
                "Notifications enabled. I will send periodic Polymarket status updates here.",
            )
        elif lowered in {"/status", "/balance"}:
            self.ensure_chat_registered(chat)
            self.send_message(chat["id"], build_status_message())
        elif lowered in {"/stop", "/unsubscribe", "/stop_notifications"}:
            self.remove_chat(str(chat["id"]))
            self.send_message(chat["id"], "Notifications disabled for this chat.")

    def poll_updates(self):
        params = {
            "timeout": 25,
            "offset": self.state.get("offset", 0),
            "allowed_updates": json.dumps(["message", "channel_post"]),
        }
        try:
            updates = self.api_get("getUpdates", params)
        except Exception as error:
            log(f"Update polling failed: {error}")
            time.sleep(POLL_INTERVAL_SECONDS)
            return

        for update in updates:
            self.state["offset"] = update["update_id"] + 1
            self.handle_update(update)

        save_subscribers(self.state)

    def broadcast_status_if_due(self):
        now = time.time()
        if now - self.last_status_sent_at < STATUS_INTERVAL_SECONDS:
            return

        chats = list(self.state.get("chats", {}).keys())
        if not chats:
            self.last_status_sent_at = now
            return

        status_message = build_status_message()
        for chat_id in chats:
            chat_state = self.state["chats"].get(chat_id)
            if not chat_state:
                continue

            last_sent_status = chat_state.get("last_sent_status", "")
            if last_sent_status == status_message:
                continue

            if self.send_message(chat_id, status_message):
                chat_state["last_sent_status"] = status_message

        self.last_status_sent_at = now
        save_subscribers(self.state)

    def run(self):
        log("Telegram notifier started")
        while True:
            self.poll_updates()
            self.broadcast_status_if_due()
            time.sleep(POLL_INTERVAL_SECONDS)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or HARDCODED_TELEGRAM_BOT_TOKEN.strip()
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting telegram_notifier.py")

    notifier = TelegramNotifier(token)
    notifier.run()


if __name__ == "__main__":
    main()
