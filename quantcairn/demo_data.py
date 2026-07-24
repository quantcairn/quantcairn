"""Public API: demo data provider."""
import importlib as _il; _m = _il.import_module("src.openalpha.demo_data")
DEMO_SYMBOLS = _m.DEMO_SYMBOLS
DEMO_HISTORY_ROWS = _m.DEMO_HISTORY_ROWS
DemoDataProvider = _m.DemoDataProvider
get_demo_provider = _m.get_demo_provider
__all__ = ["DEMO_SYMBOLS", "DEMO_HISTORY_ROWS", "DemoDataProvider", "get_demo_provider"]
