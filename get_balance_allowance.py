import os
import requests
from dotenv import load_dotenv
from eth_account import Account
from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

load_dotenv()

USDC_DECIMALS = 1_000_000
DATA_API_URL = "https://data-api.polymarket.com"

def as_int(value):
    try:
        return int(str(value))
    except Exception:
        return 0

def micro_to_usdc(value):
    return as_int(value) / USDC_DECIMALS

def summarize_allowances(collateral):
    allowances = collateral.get("allowances") or {}
    if not isinstance(allowances, dict):
        allowances = {}

    normalized = []
    for spender, raw_value in allowances.items():
        raw_int = as_int(raw_value)
        if raw_int >= 10**30:
            display_value = "unlimited"
        else:
            display_value = f"{micro_to_usdc(raw_value):.6f} USDC"
        normalized.append((spender, raw_int, display_value))

    normalized.sort(key=lambda item: item[1], reverse=True)
    return normalized

def safe_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0

def get_positions(profile_address):
    try:
        response = requests.get(
            f"{DATA_API_URL}/positions",
            params={"user": profile_address},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except Exception as error:
        print(f"Ошибка загрузки positions: {error}")
        return []

def get_portfolio_value(profile_address):
    try:
        response = requests.get(
            f"{DATA_API_URL}/value",
            params={"user": profile_address},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list) and payload:
            return safe_float(payload[0].get("value"))
        if isinstance(payload, dict):
            return safe_float(payload.get("value"))
    except Exception as error:
        print(f"Ошибка загрузки portfolio value: {error}")
    return None

def summarize_positions(positions):
    total_current_value = 0.0
    total_initial_value = 0.0
    normalized = []

    for position in positions:
        size = safe_float(position.get("size"))
        current_value = safe_float(position.get("currentValue"))
        initial_value = safe_float(position.get("initialValue"))

        if size <= 0 and current_value <= 0 and initial_value <= 0:
            continue

        total_current_value += current_value
        total_initial_value += initial_value
        normalized.append(
            {
                "title": position.get("title", "Unknown market"),
                "outcome": position.get("outcome", "Unknown outcome"),
                "size": size,
                "avg_price": safe_float(position.get("avgPrice")),
                "cur_price": safe_float(position.get("curPrice")),
                "current_value": current_value,
                "initial_value": initial_value,
            }
        )

    return normalized, total_current_value, total_initial_value

def main():
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    # Тот самый адрес, на котором лежат деньги
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS") 
    
    signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", 2)) 
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", 137))
    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")

    if not pk or not funder:
        print("Ошибка: Убедитесь, что POLYMARKET_PRIVATE_KEY и POLYMARKET_FUNDER_ADDRESS заполнены в .env")
        return

    account = Account.from_key(pk)
    print(f"Ваш базовый адрес от ключа (Owner): {account.address}")
    print(f"Ваш адрес с деньгами (Proxy/Funder):  {funder}")
    print(f"Тип подписи: {signature_type}")

    print("\nАвторизация Proxy-кошелька...")
    
    # ВАЖНО: Мы обязаны передать funder и signature_type при генерации ключей,
    # иначе ключи сгенерируются для пустого Owner кошелька!
    try:
        temp_client = ClobClient(
            host=host, 
            chain_id=chain_id, 
            key=pk, 
            funder=funder, 
            signature_type=signature_type
        )
        user_creds = temp_client.create_or_derive_api_key()
        print("✅ Торговые L2 API Ключи для Proxy успешно получены!")
    except Exception as e:
        print(f"❌ Ошибка генерации ключей: {e}")
        # Если выдаст ошибку, попробуйте в .env поставить POLYMARKET_SIGNATURE_TYPE=1
        return

    # Инициализируем полноценный боевой клиент
    client = ClobClient(
        host=host, 
        chain_id=chain_id, 
        key=pk, 
        creds=user_creds, 
        funder=funder,
        signature_type=signature_type
    )

    # Проверяем баланс
    try:
        collateral = client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=signature_type,
            )
        )

        balance_raw = collateral.get("balance", 0)
        balance_usdc = micro_to_usdc(balance_raw)
        allowances = summarize_allowances(collateral)
        positions = get_positions(funder)
        normalized_positions, positions_current_value, positions_initial_value = summarize_positions(positions)
        portfolio_value_api = get_portfolio_value(funder)
        portfolio_value_dynamic = balance_usdc + positions_current_value

        print("\n=== ВАШ БАЛАНС ===")
        print(f"Сырой ответ API: {collateral}")
        print(f"Balance raw: {balance_raw}")
        print(f"Available to trade (USDC): {balance_usdc:.6f}")
        print(f"Portfolio value via API /value: {portfolio_value_api:.6f}" if portfolio_value_api is not None else "Portfolio value via API /value: недоступно")
        print(f"Portfolio value dynamic: {portfolio_value_dynamic:.6f}")
        print(f"Positions current value total: {positions_current_value:.6f}")
        print(f"Positions initial value total: {positions_initial_value:.6f}")

        if allowances:
            print("\nAllowances:")
            for spender, _, display_value in allowances:
                print(f"  {spender}: {display_value}")
        else:
            print("\nAllowances: не найдены")

        if normalized_positions:
            print("\nOpen positions:")
            for item in normalized_positions:
                print(
                    f"  {item['outcome']} | size={item['size']:.4f} | "
                    f"avg=${item['avg_price']:.4f} | now=${item['cur_price']:.4f} | "
                    f"value=${item['current_value']:.4f} | {item['title'][:80]}"
                )
        else:
            print("\nOpen positions: не найдены")
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")

if __name__ == "__main__":
    main()
