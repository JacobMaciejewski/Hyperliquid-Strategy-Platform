"""Unit tests for the two example plugins, driven by a fake context (no threads, deterministic)."""

from plugins.market_maker import MarketMaker
from plugins.mean_reversion import MeanReversion
from strategy_platform.contract.orders import OrderResult, OrderState


class FakeCtx:
    """Records what the plugin asks the platform to do; returns canned order results."""

    def __init__(self, config, fill=False):
        self.config = config
        self.orders = []
        self.cancels = []
        self.logs = []
        self._n = 0
        self._fill = fill

    def subscribe(self, coin, feed):
        pass

    def submit_order(self, coin, side, sz, limit_px, tif="Gtc", reduce_only=False):
        self._n += 1
        cloid = f"c{self._n}"
        self.orders.append({"coin": coin, "side": side, "sz": sz, "px": limit_px,
                            "reduce_only": reduce_only, "cloid": cloid})
        state = OrderState.FILLED if self._fill else OrderState.RESTING
        return OrderResult(cloid, state, oid=self._n, filled_sz=sz if self._fill else 0.0)

    def cancel(self, cloid):
        self.cancels.append(cloid)
        return True

    def log(self, message):
        self.logs.append(message)


def bbo(px):
    s = str(float(px))
    return {"data": {"bbo": [{"px": s}, {"px": s}]}}  # bid == ask == px, so mid == px


# --- mean reversion ---

def test_mean_reversion_holds_within_band_then_fades_a_spike():
    ctx = FakeCtx({"coin": "BTC", "k_stddev": 2.0, "window_secs": 100, "order_size": 0.001}, fill=True)
    mr = MeanReversion()
    mr.on_start(ctx)
    for px in (100, 101, 99, 100, 101):
        mr.on_market_data(bbo(px))
    assert ctx.orders == []                 # all within the band -> no entry

    mr.on_market_data(bbo(200))             # big spike up -> fade it with a sell
    assert len(ctx.orders) == 1 and ctx.orders[0]["side"] == "sell"


def test_mean_reversion_flattens_with_reduce_only():
    ctx = FakeCtx({"coin": "BTC", "k_stddev": 2.0, "window_secs": 100, "order_size": 0.001,
                   "flatten_ticks": 1}, fill=True)
    mr = MeanReversion()
    mr.on_start(ctx)
    for px in (100, 101, 99, 100, 101):
        mr.on_market_data(bbo(px))
    mr.on_market_data(bbo(200))             # enter short (filled immediately)
    mr.on_market_data(bbo(100))             # back toward mean -> flatten
    assert any(o["reduce_only"] and o["side"] == "buy" for o in ctx.orders)


# --- market maker ---

def test_market_maker_quotes_both_sides_inside_the_spread():
    ctx = FakeCtx({"coin": "ETH", "spread_bps": 10, "order_size": 0.01,
                   "inventory_cap": 0.05, "requote_bps": 2})
    mm = MarketMaker()
    mm.on_start(ctx)
    mm.on_market_data(bbo(100))
    assert {o["side"] for o in ctx.orders} == {"buy", "sell"}
    buy = next(o for o in ctx.orders if o["side"] == "buy")
    sell = next(o for o in ctx.orders if o["side"] == "sell")
    assert buy["px"] < 100 < sell["px"]     # bid below mid, ask above


def test_market_maker_fill_moves_inventory_and_respects_cap():
    ctx = FakeCtx({"coin": "ETH", "spread_bps": 10, "order_size": 0.01,
                   "inventory_cap": 0.005, "requote_bps": 2})
    mm = MarketMaker()
    mm.on_start(ctx)
    mm.on_market_data(bbo(100))
    bid_cloid = mm.quotes["bid"]["cloid"]
    mm.on_order_update(OrderResult(bid_cloid, OrderState.FILLED, filled_sz=0.01))
    assert mm.inventory == 0.01

    ctx.orders.clear()
    mm.on_market_data(bbo(101))             # inventory over cap -> must not add another bid
    assert all(o["side"] != "buy" for o in ctx.orders)
