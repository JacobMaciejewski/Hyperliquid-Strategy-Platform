# Plugin Guide

A plugin is a trading strategy. You write one Python class; the platform runs it. You never open a
websocket, touch the SDK, or route orders yourself — you only use the handle (`ctx`) you're given.
That handle is the single door through which the platform enforces limits, persistence, and isolation.

## The contract

Subclass `Plugin` and override the hooks you need. All default to no-ops, so implement only what you use.

| Hook | When | What to do |
|------|------|-----------|
| `on_start(ctx)` | once, at startup | subscribe to feeds, set up state |
| `on_market_data(event)` | each tick of a feed you subscribed to | react; maybe place orders |
| `on_order_update(update)` | a resting order fills/cancels later (async) | update your view of positions |
| `on_stop()` | once, at shutdown | cancel or flatten |

Hold your state as plain instance attributes (`self.position`, `self.window`, …). Nothing special is needed.

## The `ctx` handle

```python
ctx.config                                  # your config block, a dict
ctx.subscribe(coin, feed)                    # feed is "bbo", "l2Book", or "trades"
ctx.submit_order(coin, side, sz, limit_px,   # side is "buy"/"sell"; returns an OrderResult now
                 tif="Gtc", reduce_only=False)
ctx.cancel(cloid)                            # request a cancel
ctx.log(message)                             # shows up on the status page
```

`submit_order` answers immediately — it does not wait for a fill. The `result` it returns tells you
what happened:

- `result.state` — `resting` (the order is sitting on the book), `filled` (it traded right away), or
  `rejected`.
- `result.reason` — why it was rejected (e.g. a global limit was hit). Set only when rejected.
- `result.cloid` — the order's id. Keep it if you might cancel the order.

If a resting order fills *later* (someone trades against it), you don't hear it from this return
value — it comes through `on_order_update`.

**Reading market data.** An event is the raw feed message, a dict. For a `bbo` feed, the best bid and
ask are at `event["data"]["bbo"]` — a two-item list `[bid, ask]`. Each side is either `None` (that
side of the book is empty) or a small dict with `px` (price), `sz` (size), `n` (number of orders).
So the best bid price is `float(event["data"]["bbo"][0]["px"])` — as in the example below.

## A complete example

```python
# plugins/my_strategy.py
from strategy_platform import Plugin, OrderState

class MyStrategy(Plugin):
    def on_start(self, ctx):
        self.ctx = ctx
        self.coin = ctx.config["coin"]
        self.size = ctx.config.get("size", 0.001)
        ctx.subscribe(self.coin, "bbo")

    def on_market_data(self, event):
        bid, _ask = event["data"]["bbo"]
        if not bid:
            return
        result = self.ctx.submit_order(self.coin, "buy", self.size, float(bid["px"]) * 0.99)
        if result.state is OrderState.REJECTED:
            self.ctx.log(f"rejected: {result.reason}")

    def on_order_update(self, update):
        if update.state is OrderState.FILLED:
            self.ctx.log(f"filled {update.filled_sz} @ {update.avg_px}")

    def on_stop(self):
        pass  # cancel/flatten if you hold positions
```

The shipped examples are fuller: `plugins/mean_reversion.py` and `plugins/market_maker.py`.

## What's guaranteed

- **One thread per plugin.** Your hooks never overlap — the previous call always returns before the
  next starts. So you can mutate `self.*` freely, no locks.
- **Isolation.** If your plugin throws or spins in an infinite loop, the platform and the *other*
  plugins keep running.
- **Clear rejections.** If an order would breach a global limit, `submit_order` returns `rejected`
  with a reason. It never fails silently.
- **Persistence.** Orders the platform placed survive a restart (they're in SQLite).
- **Valid live orders.** In live mode, price and size are rounded to the venue's rules for you.

## What's not guaranteed

- **Positions aren't auto-closed on shutdown.** `on_stop` cancels your *resting orders*; any open
  *position* stays. Close it yourself (the README has a helper to flatten positions).
- **You may miss ticks.** Each plugin has a bounded inbound queue. If your `on_market_data` is slow,
  old ticks are dropped and you get the freshest — don't assume you see every tick.
- **Mock fills are simplified.** In `mock` mode a resting order fills only when the mid crosses its
  price (there's no real counterparty), and partial fills aren't accumulated.
- **Limits are global and first-come.** The orders/sec budget is shared; a busy plugin can use more
  of it than a quiet one.

## Bugs the platform catches — and doesn't

**Catches for you:** exceptions in your hooks (caught, logged, isolated); over-limit orders (rejected
with a reason); invalid live order price/size (rounded to venue rules).

**Does not catch:** your strategy logic (wrong side, bad math — it places exactly what you ask);
positions you forget to close; reacting to a stale tick you should have ignored.

## Config

One TOML file holds the global limits and one block per plugin:

```toml
[execution]
mode = "mock"          # "mock" = simulated fills (no key/funds);  "live" = real testnet orders
[market_data]
mode = "live"          # live testnet market data
[observability]
port = 8080            # status page; 0 disables
[limits]               # GLOBAL — totals across every plugin
max_orders_per_sec = 5
max_open_orders    = 20
max_gross_notional = 50000

[[plugins]]
name   = "my_strat"            # unique id
module = "plugins.my_strategy" # import path under plugins/
class  = "MyStrategy"          # optional; auto-detected if the file has one Plugin subclass
[plugins.config]              # arbitrary keys -> ctx.config
coin = "BTC"
size = 0.001
```

## Add your plugin to the platform

Put your plugin file in `plugins/` at the project root; its `module` path is `plugins.<filename>` (no
`.py`). Add one `[[plugins]]` block per running instance: the same module can appear twice with a
different `name` and config. To build and start the platform, see the [README](README.md).

## Testing your plugin

- **Against live data, no risk.** Leave `execution = "mock"` (the default): your plugin runs on real
  testnet market data with simulated fills, no key or funds. The README shows how to start it.
- **In isolation.** Call your hooks directly with a fake `ctx` that records orders, no platform or
  network needed. See `tests/test_plugins.py` for the pattern.

## Restart and sync

When the platform stops and starts again, here is what your plugin should expect:

- On shutdown, your `on_stop` runs (cancel or flatten), then the platform saves state and exits cleanly.
- On the next run, the platform reloads the orders it placed from SQLite, so it does not lose track of
  them. (Starting clean instead is a run option, covered in the README.)
- Your plugin's **in-memory state resets**: `on_start` runs fresh, so rebuild any rolling state from
  new data. The platform persists its own orders, not your strategy's internals.
- **Positions live on the exchange**, not in the DB, so they survive a restart and even deleting the
  DB. The README has a helper to flatten them.
- **P&L needs a live price.** It is `position × current mark`, and the mark is not persisted. So right
  after a restart P&L reads null until the first price tick arrives (sub-second), then re-marks the
  position at the current price (not the price when you stopped), so the number can differ slightly.
- One honest gap: a resting order from a previous run is recorded in the DB, but the live executor's
  in-memory tracking is not rebuilt yet, so a fresh run will not cancel that specific order for you.
  Exchange reconciliation is future work (see the design document, section 7).
