"""Plugin B — passive market-maker on a single symbol.

Quote a bid below and an ask above the mid (inside a configured spread). Re-quote a side when the
book has moved beyond a threshold, cancelling the stale quote first. Respect an inventory cap: don't
quote a side that would push inventory past the cap.

Config (ctx.config): coin, spread_bps, order_size, inventory_cap, requote_bps.
A demo of the contract, not a tuned strategy.
"""

from strategy_platform import OrderState, Plugin


class MarketMaker(Plugin):
    def on_start(self, ctx):
        self.ctx = ctx
        c = ctx.config
        self.coin = c["coin"]
        self.spread = c.get("spread_bps", 5) / 10_000
        self.size = c.get("order_size", 0.001)
        self.cap = c.get("inventory_cap", 0.01)
        self.requote = c.get("requote_bps", 2) / 10_000
        self.inventory = 0.0
        self.quotes: dict = {"bid": None, "ask": None}  # side -> {"cloid", "px"}
        ctx.subscribe(self.coin, "bbo")

    def on_market_data(self, event):
        mid = _mid(event)
        if mid is None:
            return
        self._requote("bid", mid)
        self._requote("ask", mid)

    def _requote(self, side, mid):
        # Don't add to a side that would breach the inventory cap.
        if (side == "bid" and self.inventory >= self.cap) or (side == "ask" and self.inventory <= -self.cap):
            self._cancel(side)
            return
        target = mid * (1 - self.spread / 2) if side == "bid" else mid * (1 + self.spread / 2)
        current = self.quotes[side]
        if current and abs(current["px"] - target) / mid < self.requote:
            return  # existing quote still close enough
        self._cancel(side)
        order_side = "buy" if side == "bid" else "sell"
        result = self.ctx.submit_order(self.coin, order_side, self.size, target)
        if result.state is OrderState.REJECTED:
            self.ctx.log(f"{side} quote rejected: {result.reason}")
            return
        self.quotes[side] = {"cloid": result.cloid, "px": target}

    def _cancel(self, side):
        q = self.quotes[side]
        if q:
            self.ctx.cancel(q["cloid"])
            self.quotes[side] = None

    def on_order_update(self, update):
        if update.state is not OrderState.FILLED:
            return
        for side, signed in (("bid", +1), ("ask", -1)):
            q = self.quotes[side]
            if q and update.cloid == q["cloid"]:
                self.inventory += signed * update.filled_sz  # bid fill grows, ask fill shrinks
                self.quotes[side] = None                      # consumed -> re-quote next tick

    def on_stop(self):
        self._cancel("bid")
        self._cancel("ask")


def _mid(event):
    bid, ask = event["data"]["bbo"]
    return (float(bid["px"]) + float(ask["px"])) / 2 if bid and ask else None
