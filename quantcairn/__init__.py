"""QuantCairn — AI-driven quantitative research platform.

This is the public Python namespace for QuantCairn.
All implementations live in ``src/openalpha/``.  The modules in this
package provide a forward-facing API that re-exports from the internal
implementation modules.

Usage::

    from quantcairn.selector import AIStrategySelector
    from quantcairn.demo_data import get_demo_provider

The ``src.openalpha.*`` import paths continue to work unchanged.
"""

import importlib as _importlib

__version__ = "0.12.0"


def __getattr__(name: str):
    """Lazy top-level re-exports — defers import until attribute access."""
    _MAP = {
        "AIStrategySelector": ("src.openalpha.selector", "AIStrategySelector"),
        "FunnelTracker": ("src.openalpha.funnel_tracker", "FunnelTracker"),
        "DemoDataProvider": ("src.openalpha.demo_data", "DemoDataProvider"),
        "get_demo_provider": ("src.openalpha.demo_data", "get_demo_provider"),
        "run_preflight": ("src.openalpha.preflight", "run_preflight"),
        "PreflightReport": ("src.openalpha.preflight", "PreflightReport"),
        "score_candidate": ("src.openalpha.candidate_ranking", "score_candidate"),
        "evaluate_universe_candidate": ("src.openalpha.universe_filter", "evaluate_universe_candidate"),
        "check_data_availability": (
            "src.openalpha.data_diagnostics",
            "check_data_availability",
        ),
        "load_runtime_settings": ("src.openalpha.settings", "load_runtime_settings"),
    }
    if name in _MAP:
        mod_path, attr = _MAP[name]
        obj = getattr(_importlib.import_module(mod_path), attr)
        globals()[name] = obj  # cache for subsequent access
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
