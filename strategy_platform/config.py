"""Platform configuration: global limits + per-plugin blocks, loaded from TOML.

Uses stdlib `tomllib` (Python 3.11+), so no new dependency. Parsing is separate from wiring so it
can be tested on its own and so the Platform can also be constructed from a config object directly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Optional

from .services.risk_guard import Limits


@dataclass
class PluginSpec:
    name: str                 # unique id for this plugin instance
    module: str               # import path, e.g. "example_plugins.mean_reversion"
    config: dict              # the plugin's own config block (becomes ctx.config)
    cls: Optional[str] = None  # class name; auto-detected if there's a single Plugin subclass


@dataclass
class PlatformConfig:
    limits: Limits
    plugins: list[PluginSpec]
    execution: str = "mock"      # "mock" | "live"
    market_data: str = "live"    # "mock" | "live"
    db_path: str = "platform.db"
    observability_port: int = 8080   # JSON /status endpoint; 0 disables it


def load_config(path: str) -> PlatformConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    lim = data["limits"]
    limits = Limits(
        max_orders_per_sec=int(lim["max_orders_per_sec"]),
        max_open_orders=int(lim["max_open_orders"]),
        max_gross_notional=float(lim["max_gross_notional"]),
    )
    plugins = [
        PluginSpec(name=p["name"], module=p["module"], cls=p.get("class"), config=p.get("config", {}))
        for p in data.get("plugins", [])
    ]
    return PlatformConfig(
        limits=limits,
        plugins=plugins,
        execution=data.get("execution", {}).get("mode", "mock"),
        market_data=data.get("market_data", {}).get("mode", "live"),
        db_path=data.get("storage", {}).get("db_path", "platform.db"),
        observability_port=int(data.get("observability", {}).get("port", 8080)),
    )
