"""Test fixtures for the platform core: a fake feed and three plugins (normal, raising, stuck).

Referenced by config via module path "tests._fixture_plugins", so they load like real plugins.
"""

from strategy_platform import Plugin


class FakeFeed:
    """Scriptable upstream feed (same shape as test_market_data's), driven by tests."""

    def __init__(self):
        self.connected = False
        self.subscribed = []
        self._on_event = None
        self._on_disconnect = None

    def connect(self, on_event, on_disconnect):
        self.connected = True
        self._on_event = on_event
        self._on_disconnect = on_disconnect

    def disconnect(self):
        self.connected = False
        self.subscribed.clear()

    def subscribe(self, coin, feed):
        self.subscribed.append((coin, feed))

    def unsubscribe(self, coin, feed):
        if (coin, feed) in self.subscribed:
            self.subscribed.remove((coin, feed))

    def push(self, coin, feed, message):
        if self._on_event:
            self._on_event(coin, feed, message)


class OrdererPlugin(Plugin):
    """Places one resting buy (below mid) on the first tick it sees."""

    def on_start(self, ctx):
        self.ctx = ctx
        self.coin = ctx.config["coin"]
        self._done = False
        ctx.subscribe(self.coin, "bbo")

    def on_market_data(self, event):
        if self._done:
            return
        bid = event["data"]["bbo"][0]
        if not bid:
            return
        px = float(bid["px"]) * 0.5  # well below mid -> rests
        self.ctx.submit_order(self.coin, "buy", 12 / px, px)  # ~$12 notional, above the $10 min
        self._done = True


class RaiserPlugin(Plugin):
    """Throws on every tick — to prove an exception is isolated from peers."""

    def on_start(self, ctx):
        ctx.subscribe(ctx.config["coin"], "bbo")

    def on_market_data(self, event):
        raise RuntimeError("boom")


class BlockerPlugin(Plugin):
    """Blocks inside on_market_data — to simulate a stuck/looping plugin (isolation + shutdown)."""

    def on_start(self, ctx):
        self.block = ctx.config["block_event"]
        ctx.subscribe(ctx.config["coin"], "bbo")

    def on_market_data(self, event):
        self.block.wait(timeout=5)
