"""Public API: runtime settings."""
import importlib as _il; _m = _il.import_module("src.openalpha.settings")
load_runtime_settings = _m.load_runtime_settings
get_float_setting = _m.get_float_setting
__all__ = ["load_runtime_settings", "get_float_setting"]
