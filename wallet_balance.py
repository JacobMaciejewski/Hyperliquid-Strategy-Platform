"""Print the testnet wallet's balance — a quick way to see what you have while testing.

Run:  uv run python wallet_balance.py     (reads HL_ADDRESS from .env)
"""

import os

from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
address = os.environ.get("HL_ADDRESS")
if not address:
    raise SystemExit("HL_ADDRESS not set in .env — see README.md")

info = Info(constants.TESTNET_API_URL, skip_ws=True)
state = info.user_state(address)
spot_usdc = next((b["total"] for b in info.spot_user_state(address)["balances"] if b["coin"] == "USDC"), "0")
positions = [(p["position"]["coin"], p["position"]["szi"])
             for p in state["assetPositions"] if float(p["position"]["szi"]) != 0]

print(f"address:             {address}")
print(f"spot USDC:           {spot_usdc}")
print(f"perp account value:  ${state['marginSummary']['accountValue']}")
print(f"withdrawable:        ${state.get('withdrawable', '0')}")
print(f"open positions:      {positions or 'none (flat)'}")
