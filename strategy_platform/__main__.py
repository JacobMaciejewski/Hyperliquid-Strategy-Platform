"""Run the platform from a TOML config:  uv run python -m strategy_platform [config.toml] [--fresh]

Loads the config, starts every configured plugin, and runs until Ctrl+C — then shuts down cleanly
(stops plugins, joins threads with a timeout, tears down the websocket, closes the store).
"""

import argparse
import logging
import os
import signal
import sys
import threading

from .config import load_config
from .core import Platform
from .services.observability import StatusServer


def main() -> None:
    parser = argparse.ArgumentParser(prog="strategy_platform", description="Run the strategy plugin platform.")
    parser.add_argument("config", nargs="?", default="config.toml",
                        help="path to the TOML config (default: config.toml)")
    parser.add_argument("--fresh", action="store_true", help="delete the state DB first for a clean run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    if args.fresh and os.path.exists(config.db_path):
        os.remove(config.db_path)
        logging.info("removed %s for a fresh start", config.db_path)

    platform = Platform(config)

    # Bind the status port BEFORE starting plugins/websocket, so a port conflict fails fast and
    # cleanly instead of leaving a half-started platform running.
    server = None
    if config.observability_port:
        try:
            server = StatusServer(platform.status, port=config.observability_port)
        except OSError as exc:
            sys.exit(f"could not bind status port {config.observability_port}: {exc}\n"
                     f"Stop the other process, or set [observability] port in {args.config} (0 disables).")

    platform.start()
    if server is not None:
        server.start()
        logging.info("status endpoint: http://127.0.0.1:%d/status", config.observability_port)

    # Block until Ctrl+C. The handler ignores any further SIGINT, so a second Ctrl+C can't interrupt
    # the (multi-second) shutdown and leave websocket threads dangling.
    stop_requested = threading.Event()

    def _on_sigint(_signum, _frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_requested.set()

    signal.signal(signal.SIGINT, _on_sigint)
    stop_requested.wait()

    logging.info("shutting down (further Ctrl+C is ignored)...")
    if server is not None:
        server.stop()
    platform.stop()


if __name__ == "__main__":
    main()
