"""MarketDataService — one shared websocket, fanned out to many plugins.

The brief forbids plugins opening their own websockets, so this service owns the single SDK
connection and multiplexes every plugin's subscriptions over it. It:

  * **dedupes upstream**: the first subscriber to a (coin, feed) triggers one upstream subscribe;
    the last to leave triggers the upstream unsubscribe — we fan out to all subscribers ourselves;
  * **connects lazily**: opens the websocket on the first subscription, drops it when the last
    subscription goes away ("connected only when needed");
  * **isolates fan-out**: a subscriber callback that throws is caught and logged; the others still
    get the event. Subscribers must be cheap/non-blocking (the plugin host enqueues to each plugin's
    own thread), so one slow plugin can't stall the websocket thread;
  * **recovers itself**: the SDK does not reconnect or resubscribe on a drop, so on disconnect this
    service reconnects with backoff and resubscribes every active feed from its own registry.

Market data is deliberately not persisted: on a full restart, plugins re-subscribe in `on_start`.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Callable, Optional, Protocol

from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger(__name__)

# A subscriber callback receives one market-data message (the raw SDK event dict).
Subscriber = Callable[[dict], None]


class Feed(Protocol):
    """The upstream connection. LiveMarketFeed wraps the SDK; tests use a fake."""

    def connect(self, on_event: Callable[[str, str, dict], None], on_disconnect: Callable[[], None]) -> None: ...
    def disconnect(self) -> None: ...
    def subscribe(self, coin: str, feed: str) -> None: ...
    def unsubscribe(self, coin: str, feed: str) -> None: ...


class MarketDataService:
    def __init__(self, feed: Feed, sleep: Callable[[float], None] = time.sleep,
                 max_reconnect_delay: float = 30.0) -> None:
        self._feed = feed
        self._sleep = sleep
        self._max_delay = max_reconnect_delay
        self._lock = threading.Lock()
        self._subs: dict[tuple[str, str], dict[int, Subscriber]] = {}  # (coin, feed) -> {token: cb}
        self._token_key: dict[int, tuple[str, str]] = {}
        self._next_token = itertools.count(1)
        self._connected = False
        self._dispatch_errors = 0

    def subscribe(self, coin: str, feed: str, callback: Subscriber) -> int:
        """Register interest in a feed. Returns a token to unsubscribe with."""
        key = (coin, feed)
        with self._lock:
            if not self._connected:
                self._feed.connect(self._on_event, self._on_disconnect)  # lazy: first subscription
                self._connected = True
            if key not in self._subs:
                self._subs[key] = {}
                self._feed.subscribe(coin, feed)  # first subscriber for this feed -> upstream subscribe
            token = next(self._next_token)
            self._subs[key][token] = callback
            self._token_key[token] = key
            return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            key = self._token_key.pop(token, None)
            if key is None:
                return
            subscribers = self._subs.get(key)
            if subscribers is not None:
                subscribers.pop(token, None)
                if not subscribers:
                    del self._subs[key]
                    self._feed.unsubscribe(*key)  # last subscriber gone -> upstream unsubscribe
            if not self._subs and self._connected:
                self._feed.disconnect()  # nothing left -> drop the websocket
                self._connected = False

    def stats(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "feeds": sorted(f"{coin}:{feed}" for coin, feed in self._subs),
                "dispatch_errors": self._dispatch_errors,
            }

    def _on_event(self, coin: str, feed: str, message: dict) -> None:
        with self._lock:
            callbacks = list(self._subs.get((coin, feed), {}).values())  # snapshot, then dispatch unlocked
        for callback in callbacks:
            try:
                callback(message)
            except Exception:
                with self._lock:
                    self._dispatch_errors += 1
                log.exception("market-data subscriber callback failed for %s:%s", coin, feed)

    def _on_disconnect(self) -> None:
        """Driven by the feed when the connection drops. Reconnect (with backoff) and resubscribe."""
        with self._lock:
            self._connected = False
            keys = list(self._subs.keys())
        if not keys:
            return
        delay = 1.0
        while True:
            try:
                self._feed.connect(self._on_event, self._on_disconnect)
                for coin, feed in keys:
                    self._feed.subscribe(coin, feed)
                with self._lock:
                    self._connected = True
                log.info("market data reconnected; resubscribed %d feed(s)", len(keys))
                return
            except Exception:
                log.exception("market data reconnect failed; retrying in %.0fs", delay)
                self._sleep(delay)
                delay = min(delay * 2, self._max_delay)


class LiveMarketFeed:
    """Upstream feed backed by the SDK's shared websocket (one connection, multiplexed).

    The SDK leaves the socket's close/error handlers unset and never reconnects, so we hook them to
    detect drops and let MarketDataService drive recovery. Reaching into `info.ws_manager.ws`
    couples us to SDK internals — a known fragility, acceptable for a prototype.
    """

    def __init__(self, base_url: str = constants.TESTNET_API_URL) -> None:
        self._base_url = base_url
        self._info: Optional[Info] = None
        self._on_event: Optional[Callable[[str, str, dict], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None
        self._sids: dict[tuple[str, str], int] = {}
        self._closing = False

    def connect(self, on_event, on_disconnect) -> None:
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._sids.clear()
        self._closing = False
        self._info = Info(self._base_url)  # opens the shared websocket
        ws = self._info.ws_manager.ws
        ws.on_close = lambda *_: self._notify_disconnect()
        ws.on_error = lambda *_: self._notify_disconnect()

    def disconnect(self) -> None:
        self._closing = True
        if self._info is not None:
            self._info.disconnect_websocket()
            self._info = None

    def subscribe(self, coin: str, feed: str) -> None:
        sub = {"type": feed, "coin": coin}
        self._sids[(coin, feed)] = self._info.subscribe(sub, lambda msg, c=coin, f=feed: self._on_event(c, f, msg))

    def unsubscribe(self, coin: str, feed: str) -> None:
        sid = self._sids.pop((coin, feed), None)
        if sid is not None and self._info is not None:
            self._info.unsubscribe({"type": feed, "coin": coin}, sid)

    def _notify_disconnect(self) -> None:
        if not self._closing and self._on_disconnect is not None:
            self._on_disconnect()
