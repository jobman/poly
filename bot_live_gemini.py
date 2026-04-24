import requests
import json
import time
import os
import traceback
import sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# --- Импорты боевого Polymarket V2 ---
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

# Загружаем ключи из .env файла
load_dotenv()
PK = os.getenv("PK")
HOST = os.getenv("HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", 137))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", 1)) # 0 = EOA (MetaMask), 1/2 = Proxy

if not PK:
    print("❌ ОШИБКА: Не найден приватный ключ (PK) в файле .env!")
    sys.exit(1)

# --- НАСТРОЙКИ СТРАТЕГИИ ---
BET_AMOUNT = 2.5  # Сайз ордера в USDC (Лучше 5+, чтобы не ловить ошибку "Order too small")
MIN_PRICE = 0.005 # Мин шанс 0.5% (в центах 0.5)
MAX_PRICE = 0.02  # Макс шанс 2.0% (в центах 2.0)

# Настройки времени и профита
MAX_DAYS_TO_EXPIRY = 30
TAKE_PROFIT_MULTIPLIER = 2.0 
CHECK_INTERVAL_SECONDS = 60 
SCANNER_THRESHOLD = 5.0 # Сканируем, если есть баланс

# Настройки качества рынка
MIN_LIQUIDITY = 1000.0 
MIN_VOLUME = 5000.0   

# Настройки Сейф-Экзита
SAFE_EXIT_HOURS = 6.0    
MAX_DROP_PERCENT = 0.90  

PORTFOLIO_FILE = 'portfolio.json'
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"

# Инициализация клиента CLOB (Боевой API V2)
try:
    print(f"🔑 Инициализация клиента Polymarket V2 (Signature Type: {SIGNATURE_TYPE})...")
    client = ClobClient(
        HOST, 
        key=PK, 
        chain_id=CHAIN_ID, 
        signature_type=SIGNATURE_TYPE
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    print("✅ Ключи V2 успешно сгенерированы/подключены!")
except Exception as e:
    print(f"❌ Ошибка подключения к кошельку: {e}")
    sys.exit(1)

gamma_session = requests.Session()
gamma_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'
})

def load_json(file_path, default_data):
    if os.path.exists(file_path):
        with open(file_path, 'r') as f: return json.load(f)
    return default_data

def save_json(file_path, data):
    with open(file_path, 'w') as f: json.dump(data, f, indent=4)

def load_portfolio(): return load_json(PORTFOLIO_FILE, {"active_bets": [], "history": []})
def save_portfolio(portfolio): save_json(PORTFOLIO_FILE, portfolio)

def log(msg):
    time_str = datetime.now().strftime("%H:%M:%S")
    print(f"[{time_str}] {msg}")

def get_live_balance():
    """Получает реальный баланс USDC (свободный) из контракта Polymarket"""
    try:
        balance_dict = client.get_balance()
        # В зависимости от кошелька может возвращаться dict или строка
        if isinstance(balance_dict, dict) and 'balance' in balance_dict:
            return float(balance_dict['balance'])
        return float(balance_dict) 
    except Exception as e:
        log(f"⚠️ Ошибка получения баланса: {e}")
        return 0.0

def print_stats(portfolio):
    free_balance = get_live_balance()
    locked = sum(bet['cost'] for bet in portfolio['active_bets'])
    log(f"💰 Баланс: Свободно ${free_balance:.2f} | В позициях (Вложено): ${locked:.2f}")
    log(f"📊 Открытых сделок в базе: {len(portfolio['active_bets'])}")

def check_portfolio(portfolio):
    if not portfolio["active_bets"]:
        return

    log("Проверяем позиции в стакане (CLOB)...")
    active_bets = portfolio["active_bets"]
    still_active = []
    now = datetime.now(timezone.utc)
    
    # Получаем ВСЕ наши активные ордера с биржи ОДНИМ запросом
    try:
        open_orders_response = client.get_orders()
        open_orders = open_orders_response if isinstance(open_orders_response, list) else []
        open_order_ids = [order.get('id') for order in open_orders]
    except Exception as e:
        log(f"⚠️ Ошибка получения ордеров: {e}")
        return # Пропустим цикл, если биржа тупит

    for bet in active_bets:
        try:
            tp_order_id = bet.get('tp_order_id')
            
            # 1. ПРОВЕРКА ТЕЙК-ПРОФИТА 
            # Если ТП ордер был выставлен, но его больше нет на бирже -> ИСПОЛНИЛОСЬ!
            if tp_order_id and tp_order_id not in open_order_ids:
                log(f"🤑 ТЕЙК-ПРОФИТ ИСПОЛНЕН БИРЖЕЙ! ${bet['buy_price']} -> ${bet['target_price']} | {bet['question'][:40]}")
                bet['status'] = 'SOLD_PROFIT'
                bet['payout'] = bet['shares'] * bet['target_price']
                bet['close_date'] = datetime.now().isoformat()
                portfolio["history"].append(bet)
                continue

            # 2. ПРОВЕРКА SAFE-EXIT 
            res = gamma_session.get(f"{GAMMA_API_URL}?id={bet['event_id']}")
            if res.status_code == 200 and len(res.json()) > 0:
                event = res.json()[0]
                market = next((m for m in event.get("markets", []) if m["id"] == bet["market_id"]), None)
                
                if market:
                    if market.get("closed"):
                        log(f"🔒 Рынок закрыт (ждем резолюции биржей) | {market['question'][:40]}")
                        bet['status'] = 'CLOSED_WAITING_RESOLUTION'
                        portfolio["history"].append(bet) 
                        continue
                    
                    event_end_date = None
                    end_date_str = event.get("endDate")
                    if end_date_str:
                        event_end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))

                    if event_end_date:
                        hours_left = (event_end_date - now).total_seconds() / 3600.0
                        
                        if 0 < hours_left <= SAFE_EXIT_HOURS:
                            # Проверяем стакан на наличие покупателей (bids)
                            try:
                                orderbook = client.get_order_book(bet['token_id'])
                                current_bid = float(orderbook.get('bids', [{'price': '0'}])[0]['price'])
                            except:
                                current_bid = 0.0

                            drop_ratio = current_bid / bet['buy_price'] if bet['buy_price'] > 0 else 0
                            
                            if drop_ratio >= (1.0 - MAX_DROP_PERCENT) and current_bid > 0:
                                log(f"🛡️ СЕЙФ-ЭКЗИТ! До конца {hours_left:.1f}ч. Отменяем TP и продаем по {current_bid}")
                                
                                # Отменяем висящий лимитник тейк-профита
                                if tp_order_id in open_order_ids:
                                    client.cancel(tp_order_id)
                                    time.sleep(1.5) # Ждем, пока биржа обработает отмену
                                
                                # Продаем по рынку (лимитный ордер по текущему bid)
                                sell_order = client.create_and_post_order(OrderArgs(
                                    price=current_bid,
                                    size=bet['shares'],
                                    side="SELL",
                                    token_id=bet['token_id']
                                ))
                                
                                bet['status'] = 'SOLD_SAFE'
                                bet['sell_price'] = current_bid
                                bet['close_date'] = datetime.now().isoformat()
                                portfolio["history"].append(bet)
                                continue

            still_active.append(bet)
            time.sleep(0.5) # Бережем лимиты
            
        except Exception as e:
            log(f"Ошибка проверки позиции {bet.get('question')}: {e}")
            still_active.append(bet)

    portfolio["active_bets"] = still_active
    save_portfolio(portfolio)

def get_market_score(market):
    try:
        return float(market.get("volume", 0)) + (float(market.get("liquidity", 0)) * 2)
    except: return 0

def fetch_and_scan_all(portfolio):
    free_balance = get_live_balance()
    if free_balance < BET_AMOUNT:
        log(f"💤 Баланс (${free_balance:.2f}) меньше ставки (${BET_AMOUNT}). Ждем.")
        return

    log("📡 Радар: сканируем Gamma API для поиска целей...")
    limit = 100
    offset = 0
    now = datetime.now(timezone.utc)
    max_end_date = now + timedelta(days=MAX_DAYS_TO_EXPIRY)
    min_end_date = now + timedelta(hours=SAFE_EXIT_HOURS)
    
    existing_market_ids = [b["market_id"] for b in portfolio["active_bets"]]
    candidates = [] 

    while True:
        try:
            response = gamma_session.get(GAMMA_API_URL, params={"closed": "false", "limit": limit, "offset": offset})
            data = response.json()
            if not data: break 
                
            for event in data:
                end_date_str = event.get("endDate")
                if not end_date_str: continue
                try:
                    clean_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    if clean_date <= min_end_date or clean_date > max_end_date: continue 
                except: continue

                for market in event.get("markets", []):
                    if market.get("closed") or market["id"] in existing_market_ids: continue

                    try:
                        if float(market.get("volume", 0)) < MIN_VOLUME or float(market.get("liquidity", 0)) < MIN_LIQUIDITY: 
                            continue 
                    except: continue

                    outcomes = market.get("outcomes", [])
                    try: 
                        prices = json.loads(market.get("outcomePrices", "[]"))
                        token_ids = json.loads(market.get("clobTokenIds", "[]")) 
                    except: continue

                    for i, price_str in enumerate(prices):
                        try: price = float(price_str)
                        except: continue

                        if MIN_PRICE <= price <= MAX_PRICE and i < len(token_ids):
                            candidates.append({
                                "score": get_market_score(market),
                                "event": event,
                                "market": market,
                                "outcome_index": i,
                                "outcome": outcomes[i] if i < len(outcomes) else f"Outcome {i}",
                                "price": price,
                                "token_id": token_ids[i]
                            })

            offset += limit
            sys.stdout.write(f"\rНайдено кандидатов: {len(candidates)}")
            sys.stdout.flush()
            time.sleep(0.2) 
        except Exception as e:
            print(f"\n⚠️ Ошибка парсинга: {e}")
            break
            
    print("\nРадар завершен. Запускаем БОЕВУЮ закупку...")

    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        
        for cand in candidates:
            free_balance = get_live_balance()
            if free_balance < BET_AMOUNT:
                break 
                
            token_id = cand['token_id']
            market = cand['market']
            
            # --- АДАПТАЦИЯ ПОД TICK SIZE (V2) ---
            tick_size_str = market.get("minimumTickSize", "0.01")
            tick_size = float(tick_size_str)
            decimals = len(tick_size_str.split('.')[-1]) if '.' in tick_size_str else 2
            
            # --- 🛡️ СНАЙПЕРСКАЯ ПРОВЕРКА СТАКАНА ---
            try:
                orderbook = client.get_order_book(token_id)
                best_ask = float(orderbook.get('asks', [{'price': '1.0'}])[0]['price'])
            except Exception as e:
                log(f"⚠️ Не удалось получить стакан для {token_id}: {e}")
                continue

            if best_ask > MAX_PRICE:
                log(f"Скип: Радар -> {cand['price']}, но в стакане продают по {best_ask} | {market['question'][:30]}")
                continue

            execution_price = best_ask
            shares = round(BET_AMOUNT / execution_price, 2) 
            
            # Считаем Тейк-Профит строго по шагу цены (Tick Size)
            raw_target_price = execution_price * TAKE_PROFIT_MULTIPLIER
            target_price = round(round(raw_target_price / tick_size) * tick_size, decimals)
            
            if target_price >= 1.0:
                target_price = round(1.0 - tick_size, decimals) 
                
            try:
                log(f"⚡ СНАЙПЕР: Берем {shares} акций по ${execution_price} (Tick: {tick_size}) | {market['question'][:45]}")
                
                # 1. ПОКУПКА
                buy_order = client.create_and_post_order(OrderArgs(
                    price=execution_price,
                    size=shares,
                    side="BUY",
                    token_id=token_id
                ))
                
                time.sleep(2) # Даем бирже переварить сделку
                
                log(f"✅ КУПЛЕНО! Выставляем Тейк-Профит на продажу по ${target_price}...")
                
                # 2. МГНОВЕННЫЙ ТЕЙК-ПРОФИТ
                tp_order = client.create_and_post_order(OrderArgs(
                    price=target_price,
                    size=shares,
                    side="SELL",
                    token_id=token_id
                ))
                
                portfolio["active_bets"].append({
                    "event_id": cand["event"]["id"],
                    "market_id": market["id"],
                    "token_id": token_id,
                    "question": market["question"],
                    "outcome": cand["outcome"],
                    "buy_price": execution_price, 
                    "target_price": target_price,
                    "shares": shares,
                    "cost": BET_AMOUNT,
                    "tp_order_id": tp_order.get('orderID') or tp_order.get('id'), 
                    "date": datetime.now().isoformat()
                })
                save_portfolio(portfolio)
                log(f"🎯 Тейк-Профит ордер выставлен успешно!")

            except Exception as e:
                log(f"❌ Ошибка при выставлении ордеров: {e}")
                
    else:
        log("Новых хороших целей не найдено.")

def run_bot():
    log("-" * 50)
    portfolio = load_portfolio()
    
    check_portfolio(portfolio) 
    print_stats(portfolio)
    
    if get_live_balance() >= SCANNER_THRESHOLD:
        fetch_and_scan_all(portfolio) 
    else:
        log(f"💤 Свободно < ${SCANNER_THRESHOLD}. Ждем профита или пополнения.")
         
    log("-" * 50)

def main():
    print(r"""
     ___      _          ___       _   
    | _ ) ___| |_       | _ ) ___ | |_ 
    | _ \/ _ \  _|  _   | _ \/ _ \|  _|
    |___/\___/\__| (_)  |___/\___/ \__|
    """)
    log("🚀 БОБ v2.0 LIVE: Подключен к стакану Polymarket (V2 CLOB).")
    log("Ctrl+C для выхода.")
    
    try:
        while True:
            run_bot()
            log(f"Сплю {CHECK_INTERVAL_SECONDS} секунд...")
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n")
        log("Остановка. Данные сохранены.")
        sys.exit(0)
    except Exception:
        print("\n[!] КРИТИЧЕСКАЯ ОШИБКА:")
        traceback.print_exc()

if __name__ == "__main__":
    main()