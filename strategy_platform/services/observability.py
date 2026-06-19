"""Observability — positions/P&L projection and a curl-able JSON status endpoint.

Positions and P&L are derived on read from fills + current marks — a pure projection over the stored
fills, so we keep no extra state and need no separate manager to maintain in sync. The `StatusServer`
is a thin stdlib HTTP wrapper that serves a status snapshot as JSON, so
the live state of every plugin (config, positions, orders, P&L, errors) is one `curl` away.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


def positions_and_pnl(filled_orders, mark_fn: Callable[[str], Optional[float]]) -> dict:
    """Per (plugin, coin): net position and total P&L, from filled orders + current marks.

    position = Σ signed fill size (buy +, sell −);  P&L = cash (Σ −signed_size × fill_px) + position × mark.
    Exact total P&L with no cost-basis bookkeeping. P&L is None if the mark is unknown.
    """
    acc: dict[tuple[str, str], list] = {}  # (plugin, coin) -> [position, cash]
    for o in filled_orders:
        signed = o.filled_sz if o.side == "buy" else -o.filled_sz
        px = o.avg_px if o.avg_px is not None else o.limit_px
        bucket = acc.setdefault((o.plugin_id, o.coin), [0.0, 0.0])
        bucket[0] += signed
        bucket[1] += -signed * px

    result: dict[str, dict] = {}
    for (plugin, coin), (position, cash) in acc.items():
        mark = mark_fn(coin)
        pnl = cash + position * mark if mark is not None else None
        result.setdefault(plugin, {})[coin] = {
            "position": round(position, 8),
            "pnl": round(pnl, 4) if pnl is not None else None,
        }
    return result


class StatusServer:
    """Serves `status_fn()` as JSON at GET /status (and /). Runs in a daemon thread."""

    def __init__(self, status_fn: Callable[[], dict], host: str = "127.0.0.1", port: int = 8080) -> None:
        snapshot = status_fn

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/") in ("", "/status"):
                    body = json.dumps(snapshot(), indent=2, default=str).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *args):  # keep the server quiet
                pass

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="status-server", daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
