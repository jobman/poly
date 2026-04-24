import os
from dotenv import load_dotenv
from eth_account import Account
from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

load_dotenv()

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
        collateral = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print("\n=== ВАШ БАЛАНС ===")
        print(f"Всего USDC: {float(collateral.get('balance', 0)):.4f}")
        print(f"Разрешено тратить: {float(collateral.get('allowance', 0)):.4f}")
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")

if __name__ == "__main__":
    main()