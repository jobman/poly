import os

from dotenv import load_dotenv

load_dotenv()


def main():
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is not set in .env")

    host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

    try:
        from py_clob_client_v2 import ClobClient

        client = ClobClient(host=host, chain_id=chain_id, key=private_key)
        creds = client.create_or_derive_api_key()
        print("Using py-clob-client-v2")
        print("POLYMARKET_CLOB_API_KEY=" + getattr(creds, "api_key", ""))
        print("POLYMARKET_CLOB_SECRET=" + getattr(creds, "api_secret", ""))
        print("POLYMARKET_CLOB_PASS_PHRASE=" + getattr(creds, "api_passphrase", ""))
        return
    except ImportError:
        pass

    from py_clob_client.client import ClobClient

    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=private_key,
    )
    creds = client.create_or_derive_api_creds()
    print("Using legacy py-clob-client")
    print("POLYMARKET_CLOB_API_KEY=" + getattr(creds, "api_key", ""))
    print("POLYMARKET_CLOB_SECRET=" + getattr(creds, "api_secret", ""))
    print("POLYMARKET_CLOB_PASS_PHRASE=" + getattr(creds, "api_passphrase", ""))


if __name__ == "__main__":
    main()
