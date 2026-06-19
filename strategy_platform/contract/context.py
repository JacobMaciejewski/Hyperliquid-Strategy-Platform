"""The concrete PluginContext handed to each plugin — its only door to the platform.

It routes `subscribe` to the shared MarketDataService (delivering events to this plugin's own
queue), `submit_order`/`cancel` to the OrderRouter (tagged with this plugin's id), and `log` to the
shared error log the observability surface reads. Plugins never see a service directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .orders import OrderIntent, OrderResult, Side, Tif


class Context:
    def __init__(self, plugin_id, config, market_data, router, enqueue, errors):
        self.plugin_id = plugin_id
        self.config = config
        self._md = market_data
        self._router = router
        self._enqueue = enqueue          # puts an event on this plugin's queue
        self._errors = errors            # shared, bounded log for observability
        self._tokens: list[int] = []

    def subscribe(self, coin: str, feed: str) -> None:
        # Deliver via this plugin's queue so a slow/looping plugin can't stall the websocket thread.
        token = self._md.subscribe(coin, feed, lambda msg: self._enqueue(("market_data", msg)))
        self._tokens.append(token)

    def submit_order(self, coin: str, side: Side, sz: float, limit_px: float,
                     tif: Tif = "Gtc", reduce_only: bool = False) -> OrderResult:
        intent = OrderIntent(self.plugin_id, coin, side, sz, limit_px, tif, reduce_only)
        return self._router.submit(intent)

    def cancel(self, cloid: str) -> bool:
        return self._router.cancel(cloid)

    def log(self, message: str) -> None:
        self._errors.append({"ts": datetime.now(timezone.utc).isoformat(),
                             "plugin": self.plugin_id, "level": "info", "message": message})

    def unsubscribe_all(self) -> None:
        for token in self._tokens:
            self._md.unsubscribe(token)
        self._tokens.clear()
