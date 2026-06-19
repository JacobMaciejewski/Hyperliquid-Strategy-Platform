"""The platform core — the host process that wires everything and runs the plugins.

It builds the shared services once (they're shared because the limits, persistence, and websocket are
all platform-wide), loads each plugin into its own thread fed by a bounded queue, hands each a
`Context`, and runs the lifecycle. One thread per plugin + a per-hook try/except is what isolates a
crashing or slow/looping plugin: it backs up only its own queue and never touches the platform or the
other plugins. Shutdown signals every plugin, joins with a timeout, tears down subscriptions and the
websocket, and closes the store.
"""

from __future__ import annotations

import importlib
import logging
import queue
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import PlatformConfig, PluginSpec
from .contract.context import Context
from .contract.plugin import Plugin
from .services.market_data import Feed, LiveMarketFeed, MarketDataService
from .services.observability import positions_and_pnl
from .services.order_manager import MockOrderManager
from .services.order_router import OrderRouter
from .services.order_store import OrderStore
from .services.risk_guard import RiskGuard

log = logging.getLogger(__name__)

_STOP = object()  # sentinel pushed to a plugin's queue to end its thread
_QUEUE_MAX = 1000  # per-plugin inbound queue bound; oldest events drop under backpressure


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MidPriceTracker:
    """A PriceSource backed by live bbo: subscribes per coin and caches the mid. Used by the mock
    executor so simulated fills reference real market prices."""

    def __init__(self, market_data: MarketDataService) -> None:
        self._md = market_data
        self._mids: dict[str, float] = {}
        self._tracking: set[str] = set()
        self._tokens: list[int] = []
        self._lock = threading.Lock()
        self.on_mid: Optional[Callable[[str, float], None]] = None  # called after each mid update

    def track(self, coin: str) -> None:
        with self._lock:
            if coin in self._tracking:
                return
            self._tracking.add(coin)
        self._tokens.append(self._md.subscribe(coin, "bbo", lambda msg, c=coin: self._update(c, msg)))

    def _update(self, coin: str, msg: dict) -> None:
        bid, ask = msg["data"]["bbo"]
        if not (bid and ask):
            return
        mid = (float(bid["px"]) + float(ask["px"])) / 2
        with self._lock:
            self._mids[coin] = mid
        if self.on_mid is not None:  # outside the lock: lets the mock fill resting orders
            self.on_mid(coin, mid)

    def mid(self, coin: str) -> Optional[float]:
        with self._lock:
            return self._mids.get(coin)

    def stop(self) -> None:
        for token in self._tokens:
            self._md.unsubscribe(token)
        self._tokens.clear()


class PluginRunner:
    """One plugin, its inbound queue, and the thread that drains it. All hook calls are isolated."""

    def __init__(self, name, plugin, config, market_data, router, errors) -> None:
        self.name = name
        self.plugin = plugin
        self.dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop = threading.Event()
        self._errors = errors
        self.ctx = Context(name, config, market_data, router, self._enqueue, errors)
        self._thread = threading.Thread(target=self._run, name=f"plugin-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._enqueue(_STOP)

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _enqueue(self, event) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:  # slow plugin: drop the oldest event, keep the freshest
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                pass
            self.dropped += 1

    def _run(self) -> None:
        self._safe(self.plugin.on_start, self.ctx)
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is _STOP:
                break
            tag, payload = event
            if tag == "market_data":
                self._safe(self.plugin.on_market_data, payload)
            elif tag == "order_update":
                self._safe(self.plugin.on_order_update, payload)
        self._safe(self.plugin.on_stop)

    def _safe(self, fn, *args) -> None:
        """Run a plugin hook so its failure can never escape into the platform or its peers."""
        try:
            fn(*args)
        except Exception as exc:
            self._errors.append({"ts": _now(), "plugin": self.name, "level": "error",
                                 "message": repr(exc)})
            log.exception("plugin %s hook failed", self.name)


def _load_plugin_class(spec: PluginSpec) -> type:
    module = importlib.import_module(spec.module)
    if spec.cls:
        return getattr(module, spec.cls)
    candidates = [obj for obj in vars(module).values()
                  if isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin]
    if len(candidates) != 1:
        raise ValueError(f"{spec.module}: expected exactly one Plugin subclass, found {len(candidates)}; "
                         "set 'class' in config")
    return candidates[0]


class Platform:
    def __init__(self, config: PlatformConfig, *, feed: Optional[Feed] = None,
                 store: Optional[OrderStore] = None) -> None:
        self.config = config
        self.errors: deque = deque(maxlen=200)
        self.store = store or OrderStore(config.db_path)

        if feed is None:
            if config.market_data != "live":
                raise ValueError("market_data mode 'mock' needs an injected feed")
            feed = LiveMarketFeed()
        self.market_data = MarketDataService(feed)

        self.prices = MidPriceTracker(self.market_data)  # marks: drives mock fills AND P&L in both modes
        self._live_address: Optional[str] = None
        self._executor = self._build_executor()
        self.risk = RiskGuard(self.store, config.limits)
        self.router = OrderRouter(self.risk, self.store, self._executor)
        self.runners: list[PluginRunner] = []
        self._by_name: dict[str, PluginRunner] = {}
        self._executor.set_event_handler(self._on_order_event)  # async fills -> store + owning plugin

    def _build_executor(self):
        if self.config.execution == "mock":
            mock = MockOrderManager(self.prices)
            self.prices.on_mid = mock.poll_fills  # each mid update may fill resting mock orders
            return mock
        if self.config.execution == "live":
            manager, self._live_address = _build_live_executor()
            return manager
        raise ValueError(f"unknown execution mode: {self.config.execution}")

    def _on_order_event(self, result) -> None:
        """An async fill/cancel from the executor: persist it and notify the owning plugin."""
        self.store.apply(result)
        row = self.store.get(result.cloid)
        if row is not None:
            runner = self._by_name.get(row.plugin_id)
            if runner is not None:
                runner._enqueue(("order_update", result))

    def start(self) -> None:
        if self.config.execution == "live":  # capture async fills before any order is placed
            self._executor.start_event_stream(self._live_address)
        for spec in self.config.plugins:  # pre-track coins so marks exist before orders
            coin = spec.config.get("coin")
            if coin:
                self.prices.track(coin)
        for spec in self.config.plugins:
            runner = PluginRunner(spec.name, _load_plugin_class(spec)(), spec.config,
                                  self.market_data, self.router, self.errors)
            self.runners.append(runner)
            self._by_name[spec.name] = runner
            runner.start()
        log.info("platform started with %d plugin(s)", len(self.runners))

    def stop(self, join_timeout: float = 2.0) -> None:
        for runner in self.runners:
            runner.stop()
        for runner in self.runners:
            if not runner.join(join_timeout):
                log.warning("plugin %s did not stop in time (quarantined)", runner.name)
        for runner in self.runners:
            runner.ctx.unsubscribe_all()
        if self.config.execution == "live":
            self._executor.stop_event_stream()
        if self.prices is not None:
            self.prices.stop()
        self.store.close()
        log.info("platform stopped")

    def status(self) -> dict:
        portfolio = positions_and_pnl(self.store.filled_orders(), self.prices.mid)
        return {
            "execution": self.config.execution,
            "limits": vars(self.config.limits),
            "open_orders": self.store.open_count(),
            "gross_notional": self.store.gross_notional(),
            "market_data": self.market_data.stats(),
            "plugins": [{"name": r.name, "alive": r.is_alive(), "dropped_events": r.dropped,
                         "portfolio": portfolio.get(r.name, {})} for r in self.runners],
            "orders": [{"cloid": o.cloid, "plugin": o.plugin_id, "coin": o.coin, "side": o.side,
                        "sz": o.sz, "limit_px": o.limit_px, "state": o.state.value}
                       for o in self.store.open_orders()],
            "errors": list(self.errors),
        }


def _build_live_executor():
    """Build a LiveOrderManager from the testnet key in .env. Imported lazily so mock runs need no key."""
    import os

    from dotenv import load_dotenv
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    from .services.order_manager import LiveOrderManager

    load_dotenv()
    key = os.environ.get("HL_PRIVATE_KEY")
    if not key:
        raise ValueError("execution mode 'live' needs HL_PRIVATE_KEY in .env (see README.md)")
    wallet = Account.from_key(key)
    address = os.environ.get("HL_ADDRESS") or wallet.address
    exchange = Exchange(wallet, constants.TESTNET_API_URL, account_address=address)
    return LiveOrderManager(exchange), address
