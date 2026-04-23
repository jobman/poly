from eth_account import Account
import os
from dotenv import load_dotenv

load_dotenv()
acct = Account.from_key(os.getenv("POLYMARKET_PRIVATE_KEY"))
print(acct.address)
