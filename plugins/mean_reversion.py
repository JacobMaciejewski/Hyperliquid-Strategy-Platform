"""Plugin A — naive mean-reversion on a single symbol.

When the mid moves more than K stddevs from a short rolling mean over the last W seconds, take the
opposite side (bet on reversion). Flatten the position after T seconds, or after M ticks of the mid
moving back toward the mean — whichever comes first.

Config (ctx.config): coin, window_secs (W), k_stddev (K), order_size, flatten_secs (T), flatten_ticks (M).
A demo of the contract, not a tuned strategy.
"""

import statistics
import time
from collections import deque

from strategy_platform import OrderState, Plugin


class MeanReversion(Plugin):
    def on_start(self, ctx):
        self.ctx = ctx
        c = ctx.config
        self.coin = c["coin"]
        self.window_secs = c.get("window_secs", 30)
        self.k = c.get("k_stddev", 2.0)
        self.size = c.get("order_size", 0.001)
        self.flatten_secs = c.get("flatten_secs", 20)
        self.flatten_ticks = c.get("flatten_ticks", 10)
        self.mids: deque = deque()   # (ts, mid) within the rolling window
        self.entry = None            # current position, if any
        ctx.subscribe(self.coin, "bbo")

    def on_market_data(self, event):
        mid = _mid(event)
        if mid is None:
            return
        now = time.monotonic()
        self.mids.append((now, mid))
        while self.mids and now - self.mids[0][0] > self.window_secs:
            self.mids.popleft()

        if self.entry is None:
            self._maybe_enter(mid)
        else:
            self._maybe_flatten(mid, now)

    def _maybe_enter(self, mid):
        if len(self.mids) < 5:
            return
        values = [m for _, m in self.mids]
        mean = statistics.fmean(values)
        sd = statistics.pstdev(values)
        if sd == 0 or abs(mid - mean) < self.k * sd:
            return
        side = "sell" if mid > mean else "buy"   # fade the move
        result = self.ctx.submit_order(self.coin, side, self.size, mid)
        if result.state is OrderState.REJECTED:
            self.ctx.log(f"entry rejected: {result.reason}")
            return
        self.entry = {"side": side, "cloid": result.cloid, "mean": mean,
                      "ts": time.monotonic(), "ticks": 0,
                      "filled": result.state is OrderState.FILLED}

    def _maybe_flatten(self, mid, now):
        e = self.entry
        moved_back = mid <= e["mean"] if e["side"] == "sell" else mid >= e["mean"]
        if moved_back:
            e["ticks"] += 1
        if now - e["ts"] < self.flatten_secs and e["ticks"] < self.flatten_ticks:
            return
        if not e["filled"]:
            self.ctx.cancel(e["cloid"])  # never filled -> just pull the resting order
        else:
            close = "buy" if e["side"] == "sell" else "sell"
            self.ctx.submit_order(self.coin, close, self.size, mid, reduce_only=True)
        self.entry = None

    def on_order_update(self, update):
        if self.entry and update.cloid == self.entry["cloid"] and update.state is OrderState.FILLED:
            self.entry["filled"] = True

    def on_stop(self):
        if self.entry and not self.entry["filled"]:
            self.ctx.cancel(self.entry["cloid"])


def _mid(event):
    bid, ask = event["data"]["bbo"]
    return (float(bid["px"]) + float(ask["px"])) / 2 if bid and ask else None
