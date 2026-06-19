"""Close all open perp positions on testnet (market orders) — reset to a flat portfolio for testing.

Pairs with wallet_balance.py. Talks to the exchange directly (positions live there, not in the
platform DB). Run:
    uv run python wallet_liquidate.py
"""

import os

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
address = os.environ.get("HL_ADDRESS")
if not address:
    raise SystemExit("HL_ADDRESS not set in .env — see README.md")

info = Info(constants.TESTNET_API_URL, skip_ws=True)
exchange = Exchange(Account.from_key(os.environ["HL_PRIVATE_KEY"]), constants.TESTNET_API_URL,
                    account_address=address)

positions = [p["position"] for p in info.user_state(address)["assetPositions"]
             if float(p["position"]["szi"]) != 0]

if not positions:
    print("no open positions — already flat")
else:
    for pos in positions:
        coin = pos["coin"]
        print(f"closing {coin} (szi={pos['szi']}) ...")
        result = exchange.market_close(coin)
        status = result.get("status") if isinstance(result, dict) else result
        print(f"  -> {status}")
