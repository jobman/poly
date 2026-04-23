import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

# ==========================================
# --- SMART SWING STRATEGY SETTINGS ---
# ==========================================
STARTING_BALANCE = 100.0
BET_AMOUNT = 5.0  # Сумма входа в сделку

# 1. Зона входа (Saturation Zone)
MIN_PRICE = 0.08  # Ниже 0.08 не берем (скорее всего мертвый исход)
MAX_PRICE = 0.25  # Выше 0.25 дорого для отскока

# 2. Поиск экстремумов (Momentum & Drops)
HISTORY_WINDOW_HOURS = 2.0     # Сколько часов назад помним цены
DROP_PERCENT_REQUIRED = 0.10   # Цена должна упасть минимум на 10% от максимума

# 3. Выход из позиции (Dynamic Take-Profit & Risk Management)
RECOVERY_TARGET_PERCENT = 0.50 # Забираем 50% от глубины падения (Dynamic TP)
MIN_PROFIT_PERCENT = 0.15      # Не входим в сделку, если расчетный профит меньше 15%
STOP_LOSS_MULTIPLIER = 0.50    # Режем убыток, если цена упала на 50% от цены покупки
MAX_HOLD_HOURS = 24.0          # Time-stop: продаем, если застряли на сутки
COOLDOWN_HOURS = 4.0           # ВРЕМЯ БЛОКИРОВКИ: не заходим в рынок X часов после Стоп-Лосса

# 4. Качество рынка (High Liquidity ONLY - защита от спреда)
MIN_LIQUIDITY = 5000.0
MIN_VOLUME = 20000.0
MIN_DAYS_TO_EXPIRY = 2.0       # Не лезем в события, которые закончатся сегодня-завтра

# Системные
CHECK_INTERVAL_SECONDS = 120   # Раз в 2 минуты
SCANNER_THRESHOLD = BET_AMOUNT

PORTFOLIO_FILE = "portfolio.json"
HISTORY_FILE = "price_history.json"
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

# ==========================================
# --- UTILS & FILES ---
# ==========================================

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
    return [int(p.strip()) for p in raw_value.split(",") if p.strip().isdigit()]

def load_json(file_path, default_data):
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default_data

def save_json(file_path, data):
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)

def load_portfolio():
    pf = load_json(PORTFOLIO_FILE, {"balance": STARTING_BALANCE, "active_bets": [], "history": [], "cooldowns": {}})
    if "cooldowns" not in pf:
        pf["cooldowns"] = {}
    return pf

def save_portfolio(portfolio):
    save_json(PORTFOLIO_FILE, portfolio)

def load_price_history():
    return load_json(HISTORY_FILE, {})

def save_price_history(history):
    save_json(HISTORY_FILE, history)

# ==========================================
# --- TELEGRAM LOGIC ---
# ==========================================

def build_status_message():
    portfolio = load_portfolio()
    balance = float(portfolio.get("balance", 0.0))
    active_bets = portfolio.get("active_bets", [])
    history = portfolio.get("history", [])

    locked = sum(float(bet.get("current_value", bet.get("cost", 0.0))) for bet in active_bets)
    total = balance + locked
    net_profit = total - STARTING_BALANCE

    won_count = lost_count = tp_count = stop_count = time_stop_count = 0

    for bet in history:
        status = bet.get("status", "")
        if status == "WON": won_count += 1
        elif status == "LOST": lost_count += 1
        elif status == "SOLD_PROFIT": tp_count += 1
        elif status == "SOLD_STOP_LOSS": stop_count += 1
        elif status == "SOLD_TIME_STOP": time_stop_count += 1

    total_closed = len(history)
    profitable_closed = won_count + tp_count
    win_rate = (profitable_closed / total_closed * 100.0) if total_closed else 0.0

    lines = [
        "📊 Swing Trading Status",
        f"🕒 {datetime.now().strftime('%H:%M:%S')}",
        "",
        f"💰 Free balance: ${balance:.2f}",
        f"🔒 Locked (Value): ${locked:.2f}",
        f"💵 Total Assets: ${total:.2f}",
        f"📈 Net Profit: ${net_profit:.2f}",
        "",
        f"🔄 Active: {len(active_bets)} | 📊 Closed: {total_closed}",
        f"🎯 Win Rate: {win_rate:.1f}%",
        f"🤑 TP: {tp_count} | 🛑 SL: {stop_count} | ⏱️ Time-Stop: {time_stop_count}",
        f"✅ Resolve WON: {won_count} | ❌ Resolve LOST: {lost_count}"
    ]
    return "\n".join(lines)

def build_active_bets_message():
    portfolio = load_portfolio()
    active_bets = portfolio.get("active_bets", [])
    now = datetime.now(timezone.utc)

    lines = [f"📂 Active Bets ({len(active_bets)})", f"🕒 {datetime.now().strftime('%H:%M:%S')}\n"]
    
    if not active_bets:
        return "\n".join(lines) + "No active bets."

    for i, bet in enumerate(active_bets, 1):
        q = bet.get("question", "Unknown")[:80]
        buy = float(bet.get("buy_price", 0.0))
        cur = float(bet.get("current_price", buy))
        target = float(bet.get("target_price", buy * 1.15))
        val = float(bet.get("current_value", bet.get("cost", 0.0)))
        
        buy_date = datetime.fromisoformat(bet["date"])
        hours_held = (now - buy_date).total_seconds() / 3600.0
        pnl_pct = ((cur - buy) / buy * 100) if buy else 0

        lines.append(f"{i}. {q}")
        lines.append(f"   {bet['outcome']} | Buy: ${buy:.3f} -> Now: ${cur:.3f} ({pnl_pct:+.1f}%)")
        lines.append(f"   Target: ${target:.3f} | Value: ${val:.2f} | Held: {hours_held:.1f}h / {MAX_HOLD_HOURS}h\n")

    return "\n".join(lines).rstrip()

class TelegramBridge:
    def __init__(self, token, admin_ids):
        self.token = token.strip()
        self.admin_ids = sorted(set(int(x) for x in admin_ids))
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.session = requests.Session()

    def is_enabled(self): return bool(self.token and self.admin_ids)

    def api_request(self, method, params=None, is_post=False):
        try:
            req_method = self.session.post if is_post else self.session.get
            resp = req_method(f"{self.base_url}/{method}", data=params, params=params if not is_post else None, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"): raise RuntimeError(f"TG Error: {data}")
            return data["result"]
        except Exception as e:
            print(f"[TG Error] {e}", file=sys.stderr)
            return None

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=True):
        payload = {"chat_id": str(chat_id), "text": text[:4000], "disable_web_page_preview": "true", "disable_notification": "true" if disable_notification else "false"}
        if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
        self.api_request("sendMessage", payload, is_post=True)

    def notify_admins(self, text, disable_notification=True):
        if self.is_enabled():
            for admin in self.admin_ids:
                self.send_message(admin, text, disable_notification=disable_notification)

    def show_menu(self, chat_id):
        markup = {"keyboard": [[{"text": TELEGRAM_MENU_BUTTON_STATS}, {"text": TELEGRAM_MENU_BUTTON_ACTIVE_BETS}]], "resize_keyboard": True}
        self.send_message(chat_id, "Bot Menu:", reply_markup=markup)

    def poll_loop(self):
        while not self.stop_event.is_set():
            updates = self.api_request("getUpdates", {"timeout": 20, "offset": self.offset})
            if updates:
                for up in updates:
                    self.offset = up["update_id"] + 1
                    msg = up.get("message", {}).get("text", "").strip().lower()
                    chat_id = up.get("message", {}).get("chat", {}).get("id", 0)
                    if chat_id in self.admin_ids:
                        if msg in {"/start", "/menu"}: self.show_menu(chat_id)
                        elif msg in {"/status", TELEGRAM_MENU_BUTTON_STATS.lower()}: self.send_message(chat_id, build_status_message())
                        elif msg in {"/active", TELEGRAM_MENU_BUTTON_ACTIVE_BETS.lower()}: self.send_message(chat_id, build_active_bets_message())
            self.stop_event.wait(3)

    def start(self):
        if self.is_enabled() and not self.thread:
            self.thread = threading.Thread(target=self.poll_loop, daemon=True)
            self.thread.start()
            self.notify_admins("🚀 Swing Bot is online! Risk management & Cooldowns enabled.")

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(2)

def log(message, level="INFO", notify=False):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {message}"
    print(line)
    if (notify or level in IMPORTANT_LOG_LEVELS) and telegram_bridge:
        telegram_bridge.notify_admins(line, disable_notification=(level == "INFO"))

# ==========================================
# --- CORE SWING TRADING LOGIC ---
# ==========================================

def get_balance_snapshot(portfolio, exclude_bet=None):
    """Считает баланс. Если передан exclude_bet, не учитывает его в Locked (для фикса двойного считывания)"""
    locked = 0.0
    for bet in portfolio["active_bets"]:
        if exclude_bet and bet is exclude_bet:
            continue
        locked += bet.get("current_value", bet["cost"])
        
    return f"Free: ${portfolio['balance']:.2f} | Locked: ${locked:.2f} | Total: ${(portfolio['balance'] + locked):.2f}"

def process_trade_closure(portfolio, bet, market, current_price, reason, status):
    payout = bet["shares"] * current_price
    cost = float(bet.get("cost", 0.0))
    profit = payout - cost
    pct = (profit / cost * 100) if cost else 0
    sign = "+" if profit >= 0 else ""

    portfolio["balance"] += payout
    bet["status"] = status
    bet["sell_price"] = current_price
    bet["payout"] = payout
    bet["close_date"] = datetime.now().isoformat()
    portfolio["history"].append(bet)
    
    # 🛑 АКТИВАЦИЯ КУЛДАУНА (Только при Stop-Loss)
    if status == "SOLD_STOP_LOSS":
        cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)).timestamp()
        portfolio["cooldowns"][bet["market_id"]] = cooldown_until
        log(f"Market '{market['question'][:50]}...' added to COOLDOWN for {COOLDOWN_HOURS}h.")
    
    emoji = "✅" if profit >= 0 else "🛑"
    if status == "SOLD_TIME_STOP": emoji = "⏱️"
    
    # Передаем exclude_bet=bet, чтобы вычесть проданную сделку из Locked баланса
    log(
        f"{emoji} {reason} | {bet['outcome']} @ ${current_price:.3f} | {market['question'][:70]}\n"
        f"PnL: {sign}${profit:.2f} ({sign}{pct:.1f}%) | {get_balance_snapshot(portfolio, exclude_bet=bet)}", 
        notify=True
    )

def check_portfolio(portfolio):
    if not portfolio["active_bets"]: return
    log("Checking active positions...")
    
    active_bets = portfolio["active_bets"]
    still_active = []
    now = datetime.now(timezone.utc)

    for bet in active_bets:
        try:
            res = session.get(f"{API_URL}?id={bet['event_id']}", timeout=20)
            if res.status_code == 200 and res.json():
                event = res.json()[0]
                market = next((m for m in event.get("markets", []) if m["id"] == bet["market_id"]), None)

                if market:
                    if market.get("closed"):
                        tokens = market.get("tokensResolved", [])
                        if tokens:
                            if str(tokens[bet["outcome_index"]]) == "1":
                                process_trade_closure(portfolio, bet, market, 1.0, "WON RESOLUTION", "WON")
                            else:
                                process_trade_closure(portfolio, bet, market, 0.0, "LOST RESOLUTION", "LOST")
                            continue
                        else:
                            bet["status"] = "W8_TO_RESOLVE"
                            still_active.append(bet)
                            continue

                    prices = json.loads(market.get("outcomePrices", "[]"))
                    if len(prices) > bet["outcome_index"]:
                        cur_price = float(prices[bet["outcome_index"]])
                        bet["current_price"] = cur_price
                        bet["current_value"] = cur_price * bet["shares"]
                        
                        buy_price = bet["buy_price"]
                        target_price = bet.get("target_price", buy_price * (1.0 + MIN_PROFIT_PERCENT))
                        
                        buy_date = datetime.fromisoformat(bet["date"].replace("Z", "+00:00"))
                        if buy_date.tzinfo is None:
                            buy_date = buy_date.replace(tzinfo=timezone.utc)
                        hours_held = (now - buy_date).total_seconds() / 3600.0

                        # ДИНАМИЧЕСКИЙ TAKE PROFIT
                        if cur_price >= target_price:
                            process_trade_closure(portfolio, bet, market, cur_price, "DYNAMIC TP HIT", "SOLD_PROFIT")
                            continue
                        
                        # STOP LOSS
                        elif cur_price <= buy_price * STOP_LOSS_MULTIPLIER:
                            process_trade_closure(portfolio, bet, market, cur_price, "STOP LOSS HIT", "SOLD_STOP_LOSS")
                            continue
                        
                        # TIME STOP
                        elif hours_held >= MAX_HOLD_HOURS:
                            process_trade_closure(portfolio, bet, market, cur_price, f"TIME STOP ({MAX_HOLD_HOURS}h)", "SOLD_TIME_STOP")
                            continue

            still_active.append(bet)
            time.sleep(0.2)
        except requests.exceptions.RequestException as e:
            still_active.append(bet)
            log(f"Network error checking {bet['market_id']}: {e}", level="WARNING")
        except Exception as e:
            still_active.append(bet)
            log(f"Check failed for {bet['market_id']}: {e}", level="ERROR")

    portfolio["active_bets"] = still_active
    save_portfolio(portfolio)

def maintain_price_history(events_data):
    history = load_price_history()
    now_ts = int(time.time())
    cutoff_ts = now_ts - int(HISTORY_WINDOW_HOURS * 3600)
    
    candidates = []

    for event in events_data:
        for market in event.get("markets", []):
            if market.get("closed"): continue
            
            m_id = market["id"]
            if m_id not in history: history[m_id] = {}
            
            try:
                prices = json.loads(market.get("outcomePrices", "[]"))
                outcomes = market.get("outcomes", [])
                
                for i, p_str in enumerate(prices):
                    p = float(p_str)
                    i_str = str(i)
                    
                    if i_str not in history[m_id]: history[m_id][i_str] = []
                    
                    history[m_id][i_str] = [record for record in history[m_id][i_str] if record[0] >= cutoff_ts]
                    history[m_id][i_str].append((now_ts, p))
                    
                    if len(history[m_id][i_str]) > 1 and MIN_PRICE <= p <= MAX_PRICE:
                        max_p = max(record[1] for record in history[m_id][i_str])
                        
                        if max_p > 0 and (max_p - p) / max_p >= DROP_PERCENT_REQUIRED:
                            target_p = p + ((max_p - p) * RECOVERY_TARGET_PERCENT)
                            expected_profit_pct = (target_p - p) / p
                            
                            if expected_profit_pct >= MIN_PROFIT_PERCENT:
                                score = float(market.get("volume", 0)) + float(market.get("liquidity", 0))
                                candidates.append({
                                    "score": score,
                                    "event": event,
                                    "market": market,
                                    "outcome_index": i,
                                    "outcome": outcomes[i] if i < len(outcomes) else f"Outcome {i}",
                                    "current_price": p,
                                    "max_recent_price": max_p,
                                    "target_price": target_p,
                                    "expected_profit_pct": expected_profit_pct * 100
                                })
            except Exception:
                continue

    save_price_history(history)
    return candidates

def fetch_and_scan_all(portfolio):
    log("Scanning top markets & updating price history...")
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    min_end_date = now + timedelta(days=MIN_DAYS_TO_EXPIRY)
    
    # Очистка старых кулдаунов из памяти
    active_cooldowns = {m_id: ts for m_id, ts in portfolio.get("cooldowns", {}).items() if ts > now_ts}
    portfolio["cooldowns"] = active_cooldowns
    
    existing_markets = {b["market_id"] for b in portfolio["active_bets"]}
    valid_events = []
    
    limit, offset = 100, 0
    while offset < 500:
        res = session.get(API_URL, params={"closed": "false", "limit": limit, "offset": offset, "order": "volume", "ascending": "false"}, timeout=20)
        data = res.json()
        if not data: break
        
        for event in data:
            end_str = event.get("endDate")
            if not end_str: continue
            
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if end_dt < min_end_date: continue
            except: continue
            
            if float(event.get("volume", 0)) < MIN_VOLUME: continue
            
            event["markets"] = [m for m in event.get("markets", []) if float(m.get("liquidity", 0)) >= MIN_LIQUIDITY]
            if event["markets"]:
                valid_events.append(event)
        
        offset += limit
        time.sleep(0.2)

    candidates = maintain_price_history(valid_events)
    
    # ФИЛЬТРУЕМ КАНДИДАТОВ: Убираем тех, кто уже куплен, и тех, кто в КУЛДАУНЕ
    valid_candidates = []
    for c in candidates:
        m_id = c["market"]["id"]
        if m_id in existing_markets: continue
        if m_id in active_cooldowns: continue
        valid_candidates.append(c)
        
    if not valid_candidates:
        log("No swing-trade setups found right now.")
        return

    valid_candidates.sort(key=lambda x: x["score"], reverse=True)

    bought = 0
    for cand in valid_candidates:
        if portfolio["balance"] < BET_AMOUNT: break
        
        p = cand["current_price"]
        m = cand["market"]
        target = cand["target_price"]
        shares = BET_AMOUNT / p
        
        portfolio["balance"] -= BET_AMOUNT
        portfolio["active_bets"].append({
            "event_id": cand["event"]["id"],
            "market_id": m["id"],
            "question": m["question"],
            "outcome": cand["outcome"],
            "outcome_index": cand["outcome_index"],
            "buy_price": p,
            "target_price": target,
            "shares": shares,
            "cost": BET_AMOUNT,
            "current_value": BET_AMOUNT,
            "date": datetime.now().isoformat()
        })
        bought += 1
        
        log(
            f"📉 CAUGHT THE DIP! Bought {cand['outcome']} @ ${p:.3f} (Drop from ${cand['max_recent_price']:.3f})\n"
            f"🎯 Target TP: ${target:.3f} (+{cand['expected_profit_pct']:.1f}%)\n"
            f"Market: {m['question'][:70]}\n"
            f"{get_balance_snapshot(portfolio)}", notify=True
        )

    save_portfolio(portfolio)
    if bought > 0: log(f"Cycle done. Executed {bought} swing trades.")

def main():
    load_env_file()
    print(r"""
      ___        _             ___       _   
     / __|_ __ _(_)_ _  __ _  | _ ) ___| |_  
     \__ \ '  \ | | ' \/ _` | | _ \/ _ \  _| 
     |___/_|_|_|_|_||_\__, | |___/\___/\__| 
                      |___/                  
    """)
    
    global telegram_bridge
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_bridge = TelegramBridge(token, parse_admin_ids())
    telegram_bridge.start()
    
    log("Swing-bot starting. Memory initialized. Press Ctrl+C to stop.", notify=True)

    try:
        while True:
            try:
                log("-" * 40)
                portfolio = load_portfolio()
                check_portfolio(portfolio)
                
                if portfolio["balance"] >= SCANNER_THRESHOLD:
                    fetch_and_scan_all(portfolio)
                else:
                    log("Balance too low for new entries. Managing existing positions...")
                    
                time.sleep(CHECK_INTERVAL_SECONDS)
                
            except requests.exceptions.RequestException as req_err:
                log(f"Network error (skipping cycle): {req_err}", level="WARNING")
                time.sleep(CHECK_INTERVAL_SECONDS)
            except Exception as e:
                err = traceback.format_exc()
                log(f"Unexpected error in cycle:\n{err}", level="ERROR", notify=True)
                time.sleep(CHECK_INTERVAL_SECONDS)
                
    except KeyboardInterrupt:
        log("Bot stopped by user.", notify=True)
        if telegram_bridge: telegram_bridge.stop()

if __name__ == "__main__":
    main()