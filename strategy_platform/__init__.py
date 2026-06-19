"""Strategy plugin platform.

Public API for plugin authors — import these from the top level so plugins don't depend on internal
layout:

    from strategy_platform import Plugin, OrderState
"""

from .contract.orders import OrderIntent, OrderResult, OrderState, Side, Tif, new_cloid
from .contract.plugin import Plugin, PluginContext

__all__ = [
    "Plugin", "PluginContext",
    "OrderIntent", "OrderResult", "OrderState", "Side", "Tif", "new_cloid",
]
