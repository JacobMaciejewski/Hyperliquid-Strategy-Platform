"""Offline tests for LiveOrderManager's SDK wiring: request construction, response parsing of a
place() call, the exception path, and cancel(). Uses a stub exchange — no network."""

from hyperliquid.utils.signing import Cloid

from strategy_platform.contract.orders import OrderIntent, OrderState
from strategy_platform.services.order_manager import LiveOrderManager

RESTING_RESP = {"status": "ok", "response": {"type": "order",
                "data": {"statuses": [{"resting": {"oid": 5}}]}}}
CANCEL_OK_RESP = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}


class StubInfo:
    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]}


class StubExchange:
    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []
        self.info = StubInfo()

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
        self.calls.append({"name": name, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
                           "order_type": order_type, "reduce_only": reduce_only, "cloid": cloid})
        if self.raises:
            raise self.raises
        return self.response

    def cancel_by_cloid(self, coin, cloid):
        self.calls.append({"cancel": coin, "cloid": cloid})
        return self.response


def intent(cloid="a" * 32, side="buy", coin="BTC"):
    return OrderIntent(plugin_id="p", coin=coin, side=side, sz=1.0, limit_px=50.0, cloid=cloid)


def test_place_builds_request_and_parses_resting():
    stub = StubExchange(response=RESTING_RESP)
    result = LiveOrderManager(stub).place(intent())
    assert result.state is OrderState.RESTING and result.oid == 5

    call = stub.calls[0]
    assert call["name"] == "BTC" and call["is_buy"] is True
    assert call["order_type"] == {"limit": {"tif": "Gtc"}}
    assert call["reduce_only"] is False
    assert isinstance(call["cloid"], Cloid)  # our hex cloid wrapped into the SDK type


def test_sell_maps_to_is_buy_false():
    stub = StubExchange(response=RESTING_RESP)
    LiveOrderManager(stub).place(intent(side="sell"))
    assert stub.calls[0]["is_buy"] is False


def test_place_catches_exchange_exception():
    stub = StubExchange(raises=RuntimeError("network down"))
    result = LiveOrderManager(stub).place(intent())
    assert result.state is OrderState.REJECTED
    assert "network down" in result.reason  # surfaced, not swallowed or crashed


def test_cancel_resting_order():
    mgr = LiveOrderManager(StubExchange(response=RESTING_RESP))
    mgr.place(intent(cloid="c" * 32))            # registers cloid -> coin for cancel
    mgr._exchange.response = CANCEL_OK_RESP
    assert mgr.cancel("c" * 32) is True


def test_cancel_unknown_cloid_returns_false():
    mgr = LiveOrderManager(StubExchange(response=RESTING_RESP))
    assert mgr.cancel("d" * 32) is False         # never placed -> no coin mapping, no SDK call


# --- async fills (userFills) ---

WS_FILL = {"coin": "BTC", "px": "50000", "sz": "0.01", "side": "Buy",
           "oid": 123, "cloid": "0x" + "a" * 32, "tid": 999}


def test_fill_to_result_maps_and_strips_cloid():
    r = LiveOrderManager._fill_to_result(WS_FILL)
    assert r.state is OrderState.FILLED
    assert r.cloid == "a" * 32            # 0x stripped to match our internal cloid
    assert r.oid == 123 and r.filled_sz == 0.01 and r.avg_px == 50000.0


def test_fill_to_result_ignores_foreign_order():
    assert LiveOrderManager._fill_to_result({"coin": "BTC", "px": "1", "sz": "1"}) is None  # no cloid


def test_user_fills_routes_to_handler_and_skips_snapshot():
    mgr = LiveOrderManager(StubExchange())
    got = []
    mgr.set_event_handler(got.append)
    mgr._on_user_fills({"data": {"isSnapshot": True, "fills": [WS_FILL]}})    # snapshot -> ignored
    assert got == []
    mgr._on_user_fills({"data": {"isSnapshot": False, "fills": [WS_FILL]}})   # live -> routed
    assert len(got) == 1 and got[0].cloid == "a" * 32 and got[0].state is OrderState.FILLED


# --- venue price/size rounding ---

def test_round_price_and_size():
    assert LiveOrderManager._round_price(62543.78, 5) == 62544.0    # 5 significant figures
    assert LiveOrderManager._round_price(1712.59325, 4) == 1712.6   # 5 sig figs, <= 2 decimals
    assert LiveOrderManager._round_size(0.0123456, 4) == 0.0123     # rounded to szDecimals


def test_place_rounds_to_venue_rules_before_submit():
    stub = StubExchange(response=RESTING_RESP)
    LiveOrderManager(stub).place(OrderIntent("p", "ETH", "buy", 0.0123456, 1712.59325, cloid="e" * 32))
    call = stub.calls[0]
    assert call["limit_px"] == 1712.6   # rounded price reaches the venue
    assert call["sz"] == 0.0123         # rounded to ETH's szDecimals (4)
