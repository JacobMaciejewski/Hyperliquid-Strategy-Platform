"""The plugin contract: what a strategy author implements, and the handle they're given.

A plugin is a class that subclasses `Plugin` and overrides the lifecycle hooks it needs.
It holds its own state as plain instance attributes and talks to the platform *only* through
the `PluginContext` (`ctx`) it receives. It never imports the SDK or touches a service
directly — that is what makes limits, persistence, and isolation non-bypassable.

Threading guarantee: each plugin runs on its own single thread, draining one inbound event at a
time. So your hooks (`on_start`, `on_market_data`, `on_order_update`, `on_stop`) never overlap each
other — the previous call always returns before the next begins — and you can mutate `self.*`
without locks. (Different plugins do run concurrently, but they never share state.)

Minimal example:

    class FlatBuyer(Plugin):
        def on_start(self, ctx):
            self.ctx = ctx
            self.coin = ctx.config["coin"]
            ctx.subscribe(self.coin, "bbo")

        def on_market_data(self, event):
            px = float(event["data"]["bbo"][0]["px"])
            result = self.ctx.submit_order(self.coin, "buy", sz=0.001, limit_px=px * 0.5)
            if result.state is OrderState.REJECTED:
                self.ctx.log(f"rejected: {result.reason}")

        def on_order_update(self, update):
            ...  # react to fills / cancels

        def on_stop(self):
            ...  # cancel/flatten on shutdown
"""

from __future__ import annotations

from typing import Protocol

from .orders import OrderResult, Side, Tif


class PluginContext(Protocol):
    """The platform's mediated handle, passed to a plugin. The only way out.

    `submit_order` returns synchronously: the platform's pre-trade limit check (RiskGuard)
    either accepts the order (result carries a cloid) or rejects it (result carries the
    reason). Fills and cancels arrive later, asynchronously, via `Plugin.on_order_update`.
    """

    config: dict  # this plugin's own config block, independent of global config

    def subscribe(self, coin: str, feed: str) -> None: ...

    def submit_order(
        self,
        coin: str,
        side: Side,
        sz: float,
        limit_px: float,
        tif: Tif = "Gtc",
        reduce_only: bool = False,
    ) -> OrderResult: ...

    def cancel(self, cloid: str) -> None: ...

    def log(self, message: str) -> None: ...


class Plugin:
    """Base class for strategies. Override the hooks you need; all default to no-ops.

    Lifecycle: on_start (once) -> on_market_data / on_order_update (many) -> on_stop (once).
    """

    def on_start(self, ctx: PluginContext) -> None:
        """Called once. Subscribe to feeds and initialize state here."""

    def on_market_data(self, event: dict) -> None:
        """Called on each market-data update for a subscribed feed."""

    def on_order_update(self, update: OrderResult) -> None:
        """Called when one of this plugin's orders fills, cancels, or is rejected async."""

    def on_stop(self) -> None:
        """Called once on platform shutdown. Cancel/flatten here."""
