"""Tests for MarketDataService: dedupe, lazy connect, fan-out isolation, refcount, reconnect."""

from strategy_platform.services.market_data import MarketDataService


class FakeFeed:
    """A scriptable upstream: records subscribe/unsubscribe and lets tests push events / drops."""

    def __init__(self):
        self.connected = False
        self.connect_calls = 0
        self.subscribed: list[tuple[str, str]] = []
        self._on_event = None
        self._on_disconnect = None

    def connect(self, on_event, on_disconnect):
        self.connected = True
        self.connect_calls += 1
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

    # test helpers
    def push(self, coin, feed, message):
        self._on_event(coin, feed, message)

    def drop(self):
        self.connected = False
        self.subscribed.clear()       # a dropped connection loses its upstream subs
        self._on_disconnect()


def service():
    feed = FakeFeed()
    return MarketDataService(feed, sleep=lambda _: None), feed


def test_lazy_connect_on_first_subscribe():
    svc, feed = service()
    assert feed.connected is False
    svc.subscribe("BTC", "bbo", lambda m: None)
    assert feed.connected is True
    assert feed.subscribed == [("BTC", "bbo")]


def test_dedupes_upstream_subscription():
    svc, feed = service()
    got_a, got_b = [], []
    svc.subscribe("BTC", "bbo", got_a.append)
    svc.subscribe("BTC", "bbo", got_b.append)
    assert feed.subscribed == [("BTC", "bbo")]   # one upstream sub for two plugins
    feed.push("BTC", "bbo", {"px": 1})
    assert got_a == [{"px": 1}] and got_b == [{"px": 1}]  # both fanned out to


def test_unsubscribe_refcount_and_lazy_disconnect():
    svc, feed = service()
    t1 = svc.subscribe("BTC", "bbo", lambda m: None)
    t2 = svc.subscribe("BTC", "bbo", lambda m: None)
    svc.unsubscribe(t1)
    assert feed.subscribed == [("BTC", "bbo")]   # still one subscriber -> stays up
    svc.unsubscribe(t2)
    assert feed.subscribed == []                 # last gone -> upstream unsubscribe
    assert feed.connected is False               # nothing left -> websocket dropped


def test_bad_callback_does_not_break_others():
    svc, feed = service()
    delivered = []

    def boom(_):
        raise RuntimeError("plugin bug")

    svc.subscribe("BTC", "bbo", boom)
    svc.subscribe("BTC", "bbo", delivered.append)
    feed.push("BTC", "bbo", {"px": 1})
    assert delivered == [{"px": 1}]              # good subscriber still got it
    assert svc.stats()["dispatch_errors"] == 1


def test_reconnect_resubscribes_active_feeds():
    svc, feed = service()
    svc.subscribe("BTC", "bbo", lambda m: None)
    svc.subscribe("ETH", "trades", lambda m: None)
    assert feed.connect_calls == 1

    feed.drop()  # simulate a websocket failure

    assert feed.connect_calls == 2               # reconnected
    assert set(feed.subscribed) == {("BTC", "bbo"), ("ETH", "trades")}  # resubscribed from registry
    assert svc.stats()["connected"] is True


def test_unsubscribe_unknown_token_is_noop():
    svc, _ = service()
    svc.unsubscribe(999)  # no error
