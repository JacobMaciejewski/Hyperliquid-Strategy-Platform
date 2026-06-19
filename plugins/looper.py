"""A deliberately broken plugin: spins forever on its first tick.

Used by failure_demo.py to show the platform isolates an infinite-looping plugin. NOT a real strategy.
"""

from strategy_platform import Plugin


class Looper(Plugin):
    def on_start(self, ctx):
        ctx.subscribe(ctx.config["coin"], "bbo")

    def on_market_data(self, event):
        while True:  # never returns — simulates a hung / runaway plugin
            pass
