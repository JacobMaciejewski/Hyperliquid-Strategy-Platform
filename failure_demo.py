"""Failure-scenario demo (deliverable #4).

Runs the platform from demo_config.toml — a healthy market-maker plus two deliberately broken plugins
(crasher: raises every tick; looper: infinite loop) against live testnet market data (mock execution,
no key/funds). It shows the crash and the loop stay isolated: the healthy plugin keeps trading, the
platform stays up, and shutdown still completes. The setup is the config; see demo_config.toml.

While it runs, the live state is served at the config's observability port (default :8080).

Run:  uv run python failure_demo.py [--runtime SECONDS] [--config FILE]
"""

import argparse
import logging
import os
import shutil
import tempfile
import time

from strategy_platform.config import load_config
from strategy_platform.core import Platform
from strategy_platform.services.observability import StatusServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Failure demo: a crashing/looping plugin doesn't affect others.")
    parser.add_argument("--runtime", type=int, default=20, help="seconds to run before reporting (default: 20)")
    parser.add_argument("--config", default="demo_config.toml", help="demo config (default: demo_config.toml)")
    args = parser.parse_args()

    logging.getLogger("strategy_platform").setLevel(logging.CRITICAL)  # demo prints are the output

    cfg = load_config(args.config)
    # Fresh temp DB per run: guarantees a from-scratch start and leaves no files behind.
    cfg.db_path = os.path.join(tempfile.mkdtemp(prefix="failure_demo_"), "platform.db")
    db_dir = os.path.dirname(cfg.db_path)
    port = cfg.observability_port

    platform = Platform(cfg)
    try:
        server = StatusServer(platform.status, port=port) if port else None
    except OSError:
        server = None
        print(f"(port {port} busy — /status disabled for this run)")

    print(f"Running {args.config} for {args.runtime}s against live testnet data.")
    if server:
        print(f"Watch live: open http://127.0.0.1:{port}/status in your browser (refresh to update)\n")
    platform.start()
    if server:
        server.start()

    try:
        time.sleep(args.runtime)
        _report(platform, args.runtime)
    finally:
        if server:
            server.stop()
        t0 = time.monotonic()
        platform.stop(join_timeout=2.0)
        print(f"\nShutdown in {time.monotonic() - t0:.1f}s (it did not hang on the stuck looper):")
        for r in platform.runners:
            print(f"  {r.name:<11} " +
                  ("quarantined — still running, abandoned as a daemon" if r.is_alive()
                   else "stopped cleanly"))
        shutil.rmtree(db_dir, ignore_errors=True)


def _report(platform: Platform, run_secs: int) -> None:
    status = platform.status()
    by_name = {p["name"]: p for p in status["plugins"]}
    crashes = sum(1 for e in status["errors"] if e["plugin"] == "crasher")
    healthy_orders = sum(1 for o in status["orders"] if o["plugin"] == "healthy_mm")

    healthy_portfolio = by_name["healthy_mm"]["portfolio"] or "(no fills yet)"
    print(f"\nPlatform still UP after {run_secs}s — each plugin runs on its own thread:")
    print(f"  healthy_mm  alive={by_name['healthy_mm']['alive']}  working — {healthy_orders} open order(s), "
          f"position/P&L {healthy_portfolio}")
    print(f"  crasher     alive={by_name['crasher']['alive']}  {crashes} crash(es) caught and ignored")
    print(f"  looper      alive={by_name['looper']['alive']}  stuck in an infinite loop, placed nothing")

    print("\nWhy the broken plugins don't take the platform down:")
    print("  each plugin runs on its own thread — an uncaught exception is caught per callback, and a")
    print("  CPU loop only blocks that one thread; the GIL keeps scheduling the others.")
    print("\nTrade-off of the thread model: a runaway thread can't be force-killed. The looper keeps")
    print("  burning a core (degrading throughput) until the process exits, and shutdown can only")
    print("  quarantine it, not reclaim it. Force-kill would need subprocess isolation instead of threads.")


if __name__ == "__main__":
    main()
