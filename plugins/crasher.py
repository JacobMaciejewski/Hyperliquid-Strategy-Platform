"""A deliberately broken plugin: raises an uncaught exception on every tick.

Used by failure_demo.py to show the platform isolates a crashing plugin. NOT a real strategy.
"""

from strategy_platform import Plugin


class Crasher(Plugin):
    def on_start(self, ctx):
        ctx.subscribe(ctx.config["coin"], "bbo")

    def on_market_data(self, event):
        raise RuntimeError("intentional crash: this plugin is broken")
