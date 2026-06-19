"""Execution adapters: the OrderManager interface and its two implementations.

`MockOrderManager` simulates fills against current prices (local testing / demo when no funded
testnet account is available). `LiveOrderManager` sends real orders to testnet via the Hyperliquid SDK.
Both normalize their outcome into the same `OrderResult`, so the rest of the platform doesn't
know or care which is wired in. Neither enforces limits nor persists — those are RiskGuard and
OrderStore, kept as separate concerns from execution.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Callable, Optional, Protocol

from hyperliquid.exchange import Exchange
from hyperliquid.utils.signing import Cloid

from ..contract.orders import OrderIntent, OrderResult, OrderState, new_cloid

MIN_NOTIONAL_USD = 10.0  # Hyperliquid's minimum order value


class OrderManager(Protocol):
    """Execution adapter. Mock and Live implement this identically.

    Responsible only for getting an order to a venue and reporting what happened. `set_event_handler`
    registers a callback the manager invokes for *asynchronous* lifecycle events (resting orders that
    later fill/cancel) — the platform routes those to the OrderStore and the owning plugin.
    """

    def place(self, intent: OrderIntent) -> OrderResult: ...

    def cancel(self, cloid: str) -> bool: ...

    def set_event_handler(self, handler: Callable[[OrderResult], None]) -> None: ...


class PriceSource(Protocol):
    """Supplies the current mid price for a coin, or None if unknown."""

    def mid(self, coin: str) -> Optional[float]: ...


class MockOrderManager:
    """Simulates execution against a PriceSource. Safe for concurrent callers.

    Orders that cross at placement fill immediately (synchronous result). Orders that rest are held
    and filled later by `poll_fills` when the mid crosses their price — emitting an async fill event.
    """

    def __init__(self, prices: PriceSource) -> None:
        self._prices = prices
        self._oids = itertools.count(1)
        self._lock = threading.Lock()
        self._resting: dict[str, tuple[OrderIntent, int]] = {}  # cloid -> (intent, oid)
        self._on_event: Optional[Callable[[OrderResult], None]] = None

    def set_event_handler(self, handler: Callable[[OrderResult], None]) -> None:
        self._on_event = handler

    def place(self, intent: OrderIntent) -> OrderResult:
        cloid = intent.cloid or new_cloid()

        if intent.sz * intent.limit_px < MIN_NOTIONAL_USD:
            return OrderResult(cloid, OrderState.REJECTED,
                               reason=f"Order must have minimum value of ${MIN_NOTIONAL_USD:.0f}.")

        mid = self._prices.mid(intent.coin)
        if mid is None:
            return OrderResult(cloid, OrderState.REJECTED, reason=f"no market data for {intent.coin}")

        with self._lock:
            oid = next(self._oids)

        # A buy crosses when willing to pay at/above mid; a sell when asking at/below mid.
        crosses = intent.limit_px >= mid if intent.side == "buy" else intent.limit_px <= mid
        if crosses:
            return OrderResult(cloid, OrderState.FILLED, oid=oid, filled_sz=intent.sz, avg_px=mid)

        with self._lock:
            self._resting[cloid] = (intent, oid)
        return OrderResult(cloid, OrderState.RESTING, oid=oid)

    def cancel(self, cloid: str) -> bool:
        with self._lock:
            return self._resting.pop(cloid, None) is not None

    def poll_fills(self, coin: str, mid: float) -> None:
        """Fill any resting orders on `coin` that the new mid has crossed, emitting fill events.
        A resting buy fills when the mid falls to it; a resting sell when the mid rises to it."""
        filled = []
        with self._lock:
            for cloid, (intent, oid) in list(self._resting.items()):
                if intent.coin != coin:
                    continue
                crossed = mid <= intent.limit_px if intent.side == "buy" else mid >= intent.limit_px
                if crossed:
                    del self._resting[cloid]
                    filled.append(OrderResult(cloid, OrderState.FILLED, oid=oid,
                                              filled_sz=intent.sz, avg_px=intent.limit_px))
        for result in filled:  # emit outside the lock
            if self._on_event:
                self._on_event(result)


class LiveOrderManager:
    """Sends real orders to Hyperliquid testnet and normalizes the SDK's raw response.

    The Exchange is injected (the platform owns key loading), so this class never touches
    secrets and stays unit-testable via its response parser.
    """

    def __init__(self, exchange: Exchange) -> None:
        self._exchange = exchange
        self._lock = threading.Lock()
        self._coin_by_cloid: dict[str, str] = {}  # resting cloid -> coin, needed to cancel
        self._on_event: Optional[Callable[[OrderResult], None]] = None
        self._info = None  # account-events websocket (started in live mode)
        self._sz_decimals: Optional[dict] = None  # coin -> szDecimals, fetched lazily for rounding

    def set_event_handler(self, handler: Callable[[OrderResult], None]) -> None:
        """Register the async-fill callback; start_event_stream() routes venue fills to it."""
        self._on_event = handler

    def start_event_stream(self, address: str) -> None:
        """Subscribe to the account's fills so a resting order that fills *later* reaches the platform.

        (place/cancel responses are handled synchronously; this catches async fills.) Opens a
        websocket for account events — a separate connection from market-data fan-out.
        """
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        self._info = Info(constants.TESTNET_API_URL)
        self._info.subscribe({"type": "userFills", "user": address}, self._on_user_fills)

    def stop_event_stream(self) -> None:
        if self._info is not None:
            self._info.disconnect_websocket()
            self._info = None

    def _on_user_fills(self, msg: Any) -> None:
        data = msg.get("data", {})
        # The snapshot replays historical fills on subscribe — skip it (startup gaps are
        # reconciliation's job); apply() is idempotent anyway.
        if data.get("isSnapshot") or self._on_event is None:
            return
        for fill in data.get("fills", []):
            result = self._fill_to_result(fill)
            if result is not None:
                self._on_event(result)

    @staticmethod
    def _fill_to_result(fill: dict) -> Optional[OrderResult]:
        """Map a WsFill to an OrderResult, keyed by our cloid (the venue echoes it, 0x-prefixed)."""
        cloid = fill.get("cloid")
        if not cloid:
            return None  # not one of our orders (no client id)
        cloid = cloid[2:] if cloid.startswith("0x") else cloid
        return OrderResult(cloid, OrderState.FILLED, oid=fill.get("oid"),
                           filled_sz=float(fill["sz"]), avg_px=float(fill["px"]))

    def place(self, intent: OrderIntent) -> OrderResult:
        cloid = intent.cloid or new_cloid()
        try:
            # Round to the venue's rules before submitting, or the exchange rejects the order.
            decimals = self._sz_decimals_for(intent.coin)
            limit_px = self._round_price(intent.limit_px, decimals)
            sz = self._round_size(intent.sz, decimals)
            resp = self._exchange.order(
                intent.coin, intent.side == "buy", sz, limit_px,
                {"limit": {"tif": intent.tif}}, reduce_only=intent.reduce_only,
                cloid=Cloid.from_str("0x" + cloid),
            )
        except Exception as exc:  # network/auth/signing/meta failures: surface, never silently drop
            return OrderResult(cloid, OrderState.REJECTED, reason=repr(exc))

        result = self._parse_order_response(cloid, resp)
        if result.state is OrderState.RESTING:
            with self._lock:
                self._coin_by_cloid[cloid] = intent.coin
        return result

    def _sz_decimals_for(self, coin: str) -> int:
        if self._sz_decimals is None:  # one meta fetch, cached
            self._sz_decimals = {a["name"]: a["szDecimals"] for a in self._exchange.info.meta()["universe"]}
        return self._sz_decimals.get(coin, 2)

    @staticmethod
    def _round_price(px: float, sz_decimals: int) -> float:
        # Hyperliquid perps: at most 5 significant figures and (6 - szDecimals) decimal places.
        return round(float(f"{px:.5g}"), 6 - sz_decimals)

    @staticmethod
    def _round_size(sz: float, sz_decimals: int) -> float:
        return round(sz, sz_decimals)

    def cancel(self, cloid: str) -> bool:
        with self._lock:
            coin = self._coin_by_cloid.get(cloid)
        if coin is None:
            return False
        try:
            resp = self._exchange.cancel_by_cloid(coin, Cloid.from_str("0x" + cloid))
        except Exception:
            return False
        ok = resp.get("status") == "ok" and resp["response"]["data"]["statuses"][0] == "success"
        if ok:
            with self._lock:
                self._coin_by_cloid.pop(cloid, None)
        return ok

    @staticmethod
    def _parse_order_response(cloid: str, resp: Any) -> OrderResult:
        """Map the SDK's raw order response to an OrderResult. Response shapes confirmed
        against the live Hyperliquid docs; the SDK itself returns this untyped."""
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            return OrderResult(cloid, OrderState.REJECTED, reason=str(resp))

        status = resp["response"]["data"]["statuses"][0]
        if "error" in status:  # per-order rejection still comes back as top-level "ok"
            return OrderResult(cloid, OrderState.REJECTED, reason=status["error"])
        if "resting" in status:
            return OrderResult(cloid, OrderState.RESTING, oid=status["resting"]["oid"])
        if "filled" in status:
            f = status["filled"]
            return OrderResult(cloid, OrderState.FILLED, oid=f["oid"],
                               filled_sz=float(f["totalSz"]), avg_px=float(f["avgPx"]))
        return OrderResult(cloid, OrderState.REJECTED, reason=f"unrecognized status: {status}")
