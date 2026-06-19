# Strategy Plugin Platform

A single host process that runs independently-authored trading strategies as **plugins** against
Hyperliquid testnet. It owns the exchange connections, mediates each plugin's market data and order
placement, enforces global limits, persists the orders it places, and exposes one status page.

This README is how to **run** it. To **write a plugin** see [PLUGIN_GUIDE.md](PLUGIN_GUIDE.md); to
**understand the design** see [DESIGN_DOCUMENT.md](DESIGN_DOCUMENT.md).

## Build (once)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv (once per machine)
uv sync                                            # build the environment from the lockfile
```

`uv run ...` always uses this environment; you never activate anything.

## Run the platform

```bash
uv run python -m strategy_platform                 # uses config.toml; Ctrl+C to stop
```

The shipped `config.toml` runs in **mock** mode: live testnet market data, simulated fills, no key or
funds needed. Watch live state at `http://127.0.0.1:8080/status` (open in a browser and refresh).

Two start modes, and an optional config path:

- `uv run python -m strategy_platform` resumes: it reloads the orders it placed last run from the DB.
- `uv run python -m strategy_platform --fresh` starts clean: it deletes the DB first, so it forgets
  last run's orders and starts with an empty book.
- `uv run python -m strategy_platform other.toml` uses a different config file.

## Run live (real testnet orders)

Set `mode = "live"` under `[execution]` in `config.toml`, and create a `.env` file in the project
root. The repo does **not** ship `.env` (it holds a private key, so it is gitignored), so you make
your own:

```
HL_ADDRESS=0x...        # your testnet account address
HL_PRIVATE_KEY=0x...    # your testnet key (a trade-only API wallet key is safest)
```

To get these: open https://app.hyperliquid-testnet.xyz, connect a wallet, toggle the network to
**Testnet**, and copy your address. Claim test USDC at `/drip`. Use your wallet key, or create a
trade-only API wallet at `/API`. The smallest order the venue accepts is **$10 notional**.

## Run the failure demo

```bash
uv run python failure_demo.py --runtime 30   # --runtime is optional (default 20s)
```

It runs a healthy market-maker alongside a `crasher` (raises every tick) and a `looper` (infinite
loop), then reports that the healthy plugin kept trading and the platform stayed up. Watch it live at
`http://127.0.0.1:8080/status`. No key or funds needed.

## Inspect orders and your wallet

- **Order history.** `/status` shows only open orders. Every order the platform placed, with its final
  state, is in the SQLite DB (one `orders` table keyed by `cloid`). Read it any time, even while the
  platform runs, with the bundled `litecli`:
  ```bash
  uv run litecli platform.db        # interactive; then e.g.  SELECT * FROM orders;   (\q to quit)
  ```
  Or print the full history as a table in one shot:
  ```bash
  uv run litecli -t platform.db -e "SELECT created_at, plugin_id, coin, side, sz, limit_px, state FROM orders ORDER BY created_at"
  ```
  Rows from earlier runs are still there: this is the proof that the platform's orders persist across a
  restart. (Start clean with `--fresh`, which wipes the DB.)
- **Wallet (live mode).** Two helpers read your key from `.env`:
  ```bash
  uv run python wallet_balance.py     # spot USDC, perp account value, open positions
  uv run python wallet_liquidate.py   # market-close every perp position (reset to a flat portfolio)
  ```
  Positions live on the **exchange**, not in the platform DB, so they survive a restart and even
  deleting the DB. Run `wallet_liquidate.py` to start a test flat, and `wallet_balance.py` to confirm.

## Test

```bash
uv run pytest                  # offline suite (fast, no network)
uv run pytest -m integration   # live-testnet tests (network, slow)
rm -rf .venv && uv sync        # clean rebuild if the environment breaks
```
