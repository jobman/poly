import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=os.getenv("POLYMARKET_PRIVATE_KEY"),
)

creds = client.create_or_derive_api_creds()
print(creds)
