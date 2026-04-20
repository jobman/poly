import requests
import json
import time
import os
import traceback
import sys
from datetime import datetime, timedelta, timezone

# --- НАСТРОЙКИ СТРАТЕГИИ ---
STARTING_BALANCE = 100.0
BET_AMOUNT = 1.0  
MIN_PRICE = 0.005 # Мин шанс 0.5%
MAX_PRICE = 0.02  # Макс шанс 2.0%

# Настройки Вотчлиста
WATCHLIST_MIN_PRICE = 0.021
WATCHLIST_MAX_PRICE = 0.08  

# Настройки времени и профита
MAX_DAYS_TO_EXPIRY = 30
TAKE_PROFIT_MULTIPLIER = 2.0 
CHECK_INTERVAL_SECONDS = 60 
SCANNER_THRESHOLD = 5.0 # Сканируем рынок, только если есть свободные $5

# Настройки качества рынка
MIN_LIQUIDITY = 500.0 
MIN_VOLUME = 1000.0   

# Настройки Сейф-Экзита (Безопасный выход)
SAFE_EXIT_HOURS = 6.0    # За сколько часов до конца начинаем эвакуацию
MAX_DROP_PERCENT = 0.90  # Если цена упала на 90%+ (осталось <10% стоимости), НЕ продаем, ждем чуда

PORTFOLIO_FILE = 'portfolio.json'
WATCHLIST_FILE = 'watchlist.json'
API_URL = "https://gamma-api.polymarket.com/events"

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json'
})

def load_json(file_path, default_data):
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f)
    return default_data

def save_json(file_path, data):
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

def load_portfolio(): return load_json(PORTFOLIO_FILE, {"balance": STARTING_BALANCE, "active_bets": [], "history": []})
def save_portfolio(portfolio): save_json(PORTFOLIO_FILE, portfolio)
def load_watchlist(): return load_json(WATCHLIST_FILE, [])
def save_watchlist(watchlist): save_json(WATCHLIST_FILE, watchlist)

def log(msg):
    time_str = datetime.now().strftime("%H:%M:%S")
    print(f"[{time_str}] {msg}")

def print_stats(portfolio):
    # Теперь Локед баланс считается по актуальной рыночной стоимости!
    locked = sum(bet.get('current_value', bet['cost']) for bet in portfolio['active_bets'])
    free = portfolio['balance']
    total = free + locked
    log(f"💰 Баланс: Свободно ${free:.2f} | В ордерах (Market Value): ${locked:.2f} | Всего активов: ${total:.2f}")
    log(f"📊 Открытых позиций: {len(portfolio['active_bets'])}")

def check_portfolio(portfolio):
    if not portfolio["active_bets"]:
        return

    log("Проверяем позиции и обновляем цены...")
    active_bets = portfolio["active_bets"]
    still_active = []
    now = datetime.now(timezone.utc)

    for bet in active_bets:
        try:
            res = session.get(f"{API_URL}?id={bet['event_id']}")
            if res.status_code == 200 and len(res.json()) > 0:
                event = res.json()[0]
                market = next((m for m in event.get("markets", []) if m["id"] == bet["market_id"]), None)
                
                if market:
                    # 1. Проверка на официальное закрытие (Resolution)
                    if market.get("closed"):
                        tokens_resolved = market.get("tokensResolved", [])
                        if not tokens_resolved:
                            # Маркет закрыт, но выплаты еще не распределены
                            bet['status'] = 'W8_TO_RESOLVE'
                            still_active.append(bet)
                            continue

                        # Если мы угадали:
                        if str(tokens_resolved[bet['outcome_index']]) == "1":
                            payout = bet['shares'] * 1.0 
                            portfolio["balance"] += payout
                            log(f"🔥 БИНГО! Сыграло! +${payout:.2f} | {market['question'][:40]}")
                            bet['status'] = 'WON'
                            bet['payout'] = payout
                        else:
                            log(f"❌ ЛОСС: Исход не сыграл | {market['question'][:40]}")
                            bet['status'] = 'LOST'
                            bet['payout'] = 0
                            
                        bet['close_date'] = datetime.now().isoformat()
                        portfolio["history"].append(bet)
                        continue 

                    # Получаем текущую цену для обновления баланса
                    prices = json.loads(market.get("outcomePrices", "[]"))
                    if len(prices) > bet['outcome_index']:
                        current_price = float(prices[bet['outcome_index']])
                        bet['current_price'] = current_price
                        bet['current_value'] = current_price * bet['shares'] # Обновляем реальную стоимость позы
                        
                        # Парсим дату конца
                        event_end_date = None
                        end_date_str = event.get("endDate")
                        if end_date_str:
                            clean_date_str = end_date_str.replace('Z', '+00:00')
                            event_end_date = datetime.fromisoformat(clean_date_str)

                        # 2. Если дата уже прошла, но не closed - ждем резолюции, НЕ продаем
                        if event_end_date and now >= event_end_date:
                            log(f"⏳ ОЖИДАНИЕ РЕЗОЛЮЦИИ (Экспирация прошла) | {market['question'][:40]}")
                            bet['status'] = 'W8_TO_RESOLVE'
                            still_active.append(bet)
                            continue

                        # 3. Проверка ТЕЙК-ПРОФИТА
                        target_price = bet['buy_price'] * TAKE_PROFIT_MULTIPLIER
                        if current_price >= target_price:
                            payout = bet['shares'] * current_price
                            portfolio["balance"] += payout
                            log(f"🤑 ТЕЙК-ПРОФИТ! ${bet['buy_price']} -> ${current_price:.4f}! | {market['question'][:40]}")
                            bet['status'] = 'SOLD_PROFIT'
                            bet['sell_price'] = current_price
                            bet['payout'] = payout
                            bet['close_date'] = datetime.now().isoformat()
                            portfolio["history"].append(bet)
                            continue 
                            
                        # 4. Проверка СЕЙФ-ЭКЗИТА (Безопасный выход перед концом)
                        if event_end_date:
                            hours_left = (event_end_date - now).total_seconds() / 3600.0
                            if 0 < hours_left <= SAFE_EXIT_HOURS:
                                drop_ratio = current_price / bet['buy_price']
                                # Выходим только если сохранили хотя бы >10% стоимости
                                if drop_ratio >= (1.0 - MAX_DROP_PERCENT):
                                    payout = bet['shares'] * current_price
                                    portfolio["balance"] += payout
                                    log(f"🛡️ СЕЙФ-ЭКЗИТ! До конца {hours_left:.1f}ч. Кэшбек: +${payout:.2f} (куплено за {bet['buy_price']}, сейчас {current_price}) | {market['question'][:30]}")
                                    bet['status'] = 'SOLD_SAFE'
                                    bet['sell_price'] = current_price
                                    bet['payout'] = payout
                                    bet['close_date'] = datetime.now().isoformat()
                                    portfolio["history"].append(bet)
                                    continue

            still_active.append(bet)
            time.sleep(0.2) 
        except Exception as e:
            still_active.append(bet)
            time.sleep(1.0) 

    portfolio["active_bets"] = still_active
    save_portfolio(portfolio)

def get_market_score(market):
    try:
        vol = float(market.get("volume", 0))
        liq = float(market.get("liquidity", 0))
        return vol + (liq * 2) 
    except:
        return 0

def fetch_and_scan_all(portfolio):
    log("📡 Радар: сканируем всю биржу для поиска лучших целей...")
    watchlist = load_watchlist()
    limit = 100
    offset = 0
    seen_event_ids = set()
    
    now = datetime.now(timezone.utc)
    max_end_date = now + timedelta(days=MAX_DAYS_TO_EXPIRY)
    min_end_date = now + timedelta(hours=SAFE_EXIT_HOURS) # !!! ВАЖНО: Не берем события, которые скоро закончатся
    
    existing_market_ids = [b["market_id"] for b in portfolio["active_bets"]] + \
                          [b["market_id"] for b in portfolio["history"]]

    candidates = [] 

    while True:
        params = {"closed": "false", "limit": limit, "offset": offset}
        try:
            response = session.get(API_URL, params=params)
            data = response.json()
            
            if not data or len(data) == 0:
                break 
                
            for event in data:
                if event["id"] in seen_event_ids: continue
                seen_event_ids.add(event["id"])

                end_date_str = event.get("endDate")
                if end_date_str:
                    try:
                        clean_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                        # Игнорируем то, что скоро закончится, чтобы избежать мгновенного Сейф-Экзита
                        if clean_date <= min_end_date or clean_date > max_end_date: continue 
                    except: pass
                else: continue

                for market in event.get("markets", []):
                    if market.get("closed") or market["id"] in existing_market_ids: continue

                    try:
                        volume = float(market.get("volume", 0))
                        liquidity = float(market.get("liquidity", 0))
                        if volume < MIN_VOLUME or liquidity < MIN_LIQUIDITY: continue 
                    except: continue

                    outcomes = market.get("outcomes", [])
                    try: prices = json.loads(market.get("outcomePrices", "[]"))
                    except: continue

                    for i, price_str in enumerate(prices):
                        try: price = float(price_str)
                        except: continue

                        if MIN_PRICE <= price <= MAX_PRICE:
                            candidates.append({
                                "score": get_market_score(market),
                                "event": event,
                                "market": market,
                                "outcome_index": i,
                                "outcome": outcomes[i] if i < len(outcomes) else f"Outcome {i}",
                                "price": price
                            })
                        elif WATCHLIST_MIN_PRICE <= price <= WATCHLIST_MAX_PRICE:
                            if not any(w["market_id"] == market["id"] for w in watchlist):
                                watchlist.append({
                                    "event_id": event["id"],
                                    "market_id": market["id"],
                                    "question": market["question"],
                                    "outcome": outcomes[i] if i < len(outcomes) else f"Outcome {i}",
                                    "tracked_price": price,
                                    "date_added": datetime.now().isoformat()
                                })

            offset += limit
            sys.stdout.write(f"\rСобрано рынков: {len(seen_event_ids)}... Найдено кандидатов: {len(candidates)}")
            sys.stdout.flush()
            time.sleep(0.2) 
        except Exception as e:
            print(f"\n⚠️ Ошибка парсинга: {e}")
            break
            
    print("\nРадар завершен. Запускаем умную закупку...")
    save_watchlist(watchlist)

    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        
        bought_count = 0
        for cand in candidates:
            if portfolio["balance"] < BET_AMOUNT:
                break 
                
            price = cand["price"]
            market = cand["market"]
            
            shares = BET_AMOUNT / price
            portfolio["balance"] -= BET_AMOUNT
            
            portfolio["active_bets"].append({
                "event_id": cand["event"]["id"],
                "market_id": market["id"],
                "question": market["question"],
                "outcome": cand["outcome"],
                "outcome_index": cand["outcome_index"],
                "buy_price": price,
                "shares": shares,
                "cost": BET_AMOUNT,
                "current_value": BET_AMOUNT, # На старте Value = Cost
                "date": datetime.now().isoformat()
            })
            bought_count += 1
            log(f"⭐ ТОП-КУПЛЕНО (Score: {cand['score']:.0f}): ${price} | {market['question'][:45]}...")
            
        save_portfolio(portfolio)
        log(f"Закупка завершена. Куплено {bought_count} лучших контрактов.")
    else:
        log("Новых хороших целей не найдено.")

def run_bot():
    log("-" * 50)
    portfolio = load_portfolio()
    
    # Сначала проверяем портфель (оно же обновит цены и закроет сделки)
    check_portfolio(portfolio) 
    
    # Теперь печатаем стату с УЖЕ актуальным балансом
    print_stats(portfolio)
    
    if portfolio["balance"] >= SCANNER_THRESHOLD:
        fetch_and_scan_all(portfolio) 
    else:
        log(f"💤 Свободно < ${SCANNER_THRESHOLD}. Копим пул для умной закупки.")
         
    log("-" * 50)

def main():
    print(r"""
     ___      _          ___       _   
    | _ ) ___| |_       | _ ) ___ | |_ 
    | _ \/ _ \  _|  _   | _ \/ _ \|  _|
    |___/\___/\__| (_)  |___/\___/ \__|
    """)
    log("Запуск БОБА (v1.9: Фикс Балансов, Экспираций и Аналитики). Ctrl+C для выхода.")
    
    try:
        while True:
            run_bot()
            log(f"Сплю {CHECK_INTERVAL_SECONDS // 60} минут...")
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n")
        log("Остановка. Данные сохранены.")
        sys.exit(0)
    except Exception:
        print("\n[!] ОШИБКА:")
        traceback.print_exc()

if __name__ == "__main__":
    main()