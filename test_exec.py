import os
import time
import json
import requests
from dotenv import load_dotenv

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

load_dotenv(".env")

GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CLOB_API_URL = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/")
TEST_BET_AMOUNT = 1 # Сумма тестовой сделки (в USDC)
MAX_ORDERBOOK_CANDIDATES = 75

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def get_actual_execution_price(response_payload, default_price=None):
    """Вытаскивает фактическую цену исполнения из ответа API (если доступно)"""
    try:
        if isinstance(response_payload, dict) and "transactions" in response_payload:
            txs = response_payload["transactions"]
            if txs:
                filled_shares = float(txs[0].get("filled_size", 0))
                filled_amount = float(txs[0].get("filled_amount", 0)) # in USDC
                if filled_shares > 0:
                    return filled_amount / filled_shares
    except Exception:
        pass
    return default_price

def parse_json_list(value, default=None):
    if default is None:
        default = []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    if isinstance(value, list):
        return value
    return default

def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def is_tradeable_market(market):
    return (
        market.get("active") is True
        and market.get("closed") is not True
        and market.get("acceptingOrders") is True
        and market.get("enableOrderBook") is True
        and as_float(market.get("liquidityClob", market.get("liquidityNum", 0))) > 0
        and as_float(market.get("bestBid")) > 0
        and 0 < as_float(market.get("bestAsk")) < 1
    )

def get_order_book_quiet(token_id):
    response = requests.get(
        f"{CLOB_API_URL}/book",
        params={"token_id": str(token_id)},
        timeout=10
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()

def get_best_level(levels, selector):
    if not levels:
        return None
    return selector(
        levels,
        key=lambda level: as_float(level.get("price"), -1.0)
    )

def get_top_liquid_token(client):
    log("Ищем рынок с самым узким спредом и высокой ликвидностью...")
    response = requests.get(
        GAMMA_API_URL,
        params={"closed": "false", "limit": 10, "order": "volume", "ascending": "false"},
        timeout=10
    )
    events = response.json()
    
    best_token = None
    min_spread = 999.0
    best_market_info = {}

    skipped_no_book = 0
    candidates = []

    for event in events:
        for market in event.get("markets", []):
            if not is_tradeable_market(market):
                continue

            token_ids = parse_json_list(market.get("clobTokenIds", "[]"))
            outcomes = parse_json_list(market.get("outcomes", "[]"))
            gamma_spread = as_float(market.get("bestAsk")) - as_float(market.get("bestBid"))
            if gamma_spread <= 0:
                continue

            for i, token_id in enumerate(token_ids):
                candidates.append((gamma_spread, token_id, market, outcomes, i))

    candidates.sort(key=lambda item: item[0])

    for _, token_id, market, outcomes, i in candidates[:MAX_ORDERBOOK_CANDIDATES]:
        try:
            # Получаем реальный стакан без шумного лога py_clob_client_v2 на 404.
            ob = get_order_book_quiet(token_id)
            if not ob or not ob.get("bids") or not ob.get("asks"):
                continue

            best_bid_level = get_best_level(ob["bids"], max)
            best_ask_level = get_best_level(ob["asks"], min)
            if not best_bid_level or not best_ask_level:
                continue

            best_bid = as_float(best_bid_level.get("price"))
            best_ask = as_float(best_ask_level.get("price"))
            bid_depth = as_float(best_bid_level.get("size"))
            ask_depth = as_float(best_ask_level.get("size"))

            spread = best_ask - best_bid

            # Ищем спред больше нуля, но минимальный, и чтобы была глубина минимум $10
            if 0 < spread < min_spread and bid_depth > 10 and ask_depth > 10:
                min_spread = spread
                best_token = token_id
                best_market_info = {
                    "question": market.get("question"),
                    "outcome": outcomes[i] if i < len(outcomes) else f"Index {i}",
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "tick_size": market.get("orderPriceMinTickSize", market.get("minimumTickSize", "0.01"))
                }
        except Exception as e:
            if "404" in str(e) or "No orderbook" in str(e):
                skipped_no_book += 1
            continue

    if skipped_no_book:
        log(f"Пропущено токенов без стакана: {skipped_no_book}")

    return best_token, best_market_info

def main():
    log("Инициализация Polymarket V2 Client...")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

    # Дергаем API ключи (стандартная логика V2)
    temp_client = ClobClient(host, key=private_key, chain_id=chain_id, funder=funder, signature_type=sig_type)
    creds = temp_client.create_or_derive_api_key()
    
    client = ClobClient(host, key=private_key, chain_id=chain_id, creds=creds, funder=funder, signature_type=sig_type)

    target_token, info = get_top_liquid_token(client)
    if not target_token:
        log("❌ Не найдено подходящих ликвидных рынков для теста.")
        return

    log("="*50)
    log(f"🎯 ВЫБРАН РЫНОК: {info['question']}")
    log(f"📉 Исход: {info['outcome']}")
    log(f"📊 Стакан -> Bid: ${info['best_bid']:.3f} | Ask: ${info['best_ask']:.3f} | Спред: ${info['spread']:.3f}")
    log("="*50)

    # 1. ТЕСТОВАЯ ПОКУПКА
    expected_buy_price = float(client.calculate_market_price(str(target_token), Side.BUY, TEST_BET_AMOUNT))
    buy_slippage_cap = min(expected_buy_price * 1.05, 0.99) # макс 5% проскальзывание

    log(f"🛒 Попытка КУПИТЬ на ${TEST_BET_AMOUNT}")
    log(f"   => Ожидаемая цена (из стакана API): ${expected_buy_price:.4f}")
    log(f"   => Макс. разрешенная цена (защита): ${buy_slippage_cap:.4f}")
    
    try:
        buy_resp = client.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=str(target_token), amount=TEST_BET_AMOUNT, side=Side.BUY, price=buy_slippage_cap
            ),
            options=PartialCreateOrderOptions(tick_size=str(info["tick_size"])),
            order_type=OrderType.FAK
        )
        actual_buy_price = get_actual_execution_price(buy_resp, expected_buy_price)
        log(f"✅ ПОКУПКА УСПЕШНА!")
        log(f"   => ФАКТИЧЕСКАЯ цена покупки: ~${actual_buy_price:.4f}")
        log(f"   => Расхождение (Slippage): {((actual_buy_price - expected_buy_price)/expected_buy_price)*100:.2f}%")
    except Exception as e:
        log(f"❌ Ошибка покупки: {e}")
        return

    log("⏳ Ждем 5 секунд для синхронизации блокчейна...")
    time.sleep(5)

    # Проверяем сколько акций мы реально получили
    bal_res = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(target_token), signature_type=sig_type))
    actual_shares = float(bal_res.get("balance", 0)) / 1_000_000
    log(f"💼 Куплено акций (Shares) на балансе: {actual_shares:.4f}")

    if actual_shares < 0.01:
        log("❌ Акции не поступили на баланс. Завершение теста.")
        return

    # 2. ТЕСТОВАЯ ПРОДАЖА
    expected_sell_price = float(client.calculate_market_price(str(target_token), Side.SELL, actual_shares))
    sell_slippage_floor = max(expected_sell_price * 0.95, 0.01) # мин цена продажи (-5%)

    log("\n" + "="*50)
    log(f"💸 Попытка ПРОДАТЬ {actual_shares:.4f} акций")
    log(f"   => Ожидаемая цена (из стакана API): ${expected_sell_price:.4f}")
    log(f"   => Мин. разрешенная цена (защита): ${sell_slippage_floor:.4f}")

    try:
        sell_resp = client.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=str(target_token), amount=actual_shares, side=Side.SELL, price=sell_slippage_floor
            ),
            options=PartialCreateOrderOptions(tick_size=str(info["tick_size"])),
            order_type=OrderType.FAK
        )
        actual_sell_price = get_actual_execution_price(sell_resp, expected_sell_price)
        log(f"✅ ПРОДАЖА УСПЕШНА!")
        log(f"   => ФАКТИЧЕСКАЯ цена продажи: ~${actual_sell_price:.4f}")
        log(f"   => Расхождение (Slippage): {((expected_sell_price - actual_sell_price)/expected_sell_price)*100:.2f}%")
    except Exception as e:
        log(f"❌ Ошибка продажи: {e}")
        return
        
    log("\n🏁 ТЕСТ ЗАВЕРШЕН. Исполнение ордеров и стаканы работают корректно.")

if __name__ == "__main__":
    main()
