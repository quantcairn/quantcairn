"""QuantCairn — AI-driven quantitative research platform.

Stable public API for the QuantCairn AI selection pipeline.

Quick start::

    from quantcairn import AIStrategySelector, DemoDataProvider

    # Run pipeline with demo data (no API keys required)
    provider = DemoDataProvider()
    print(provider.symbols)  # ['AAPL', 'MSFT', 'NVDA', 'SPY', 'TSLA']

For module-level imports::

    from quantcairn.selector import AIStrategySelector
    from quantcairn.demo_data import DemoDataProvider, get_demo_provider

The internal ``src.openalpha.*`` namespace continues to work but is
considered unstable and may change without notice.
"""

import importlib as _importlib

__version__ = "0.13.0"

# ── Public API ───────────────────────────────────────────────────────────────
# These symbols form the stable public surface.  Use them from top-level or
# from sub-module paths; both are supported and provide the same object.

__all__ = [
    # Selection pipeline
    "AIStrategySelector",
    # Pipeline audit & diagnostics
    "FunnelTracker",
    "FunnelStageRecord",
    # Demo / evaluation
    "DemoDataProvider",
    "get_demo_provider",
    "DEMO_SYMBOLS",
    # Preflight market checks
    "PreflightReport",
    "run_preflight",
    # Data quality diagnostics
    "check_data_availability",
    "diagnose_market_data_drops",
    # Candidate ranking
    "score_candidate",
    "score_candidates",
    # Universe filtering
    "UniverseEvaluation",
    "UniverseRule",
    "evaluate_universe_candidate",
    "filter_universe_candidates",
    "infer_asset_type",
    "load_universe_rules",
    # Runtime settings
    "load_runtime_settings",
    "get_float_setting",
]


# ── Lazy top-level re-exports ───────────────────────────────────────────────
# Attribute access triggers importlib.import_module + caching.

def __getattr__(name: str):
    _LAZY = {
        "AIStrategySelector":            ("src.openalpha.selector",                   "AIStrategySelector"),
        "FunnelTracker":                 ("src.openalpha.funnel_tracker",              "FunnelTracker"),
        "FunnelStageRecord":             ("src.openalpha.funnel_tracker",              "FunnelStageRecord"),
        "DemoDataProvider":              ("src.openalpha.demo_data",                   "DemoDataProvider"),
        "get_demo_provider":             ("src.openalpha.demo_data",                   "get_demo_provider"),
        "DEMO_SYMBOLS":                  ("src.openalpha.demo_data",                   "DEMO_SYMBOLS"),
        "PreflightReport":               ("src.openalpha.preflight",                   "PreflightReport"),
        "run_preflight":                 ("src.openalpha.preflight",                   "run_preflight"),
        "check_data_availability":       ("src.openalpha.data_diagnostics",            "check_data_availability"),
        "diagnose_market_data_drops":    ("src.openalpha.data_diagnostics",            "diagnose_market_data_drops"),
        "score_candidate":               ("src.openalpha.candidate_ranking",           "score_candidate"),
        "score_candidates":              ("src.openalpha.candidate_ranking",           "score_candidates"),
        "UniverseEvaluation":            ("src.openalpha.universe_filter",             "UniverseEvaluation"),
        "UniverseRule":                  ("src.openalpha.universe_filter",             "UniverseRule"),
        "evaluate_universe_candidate":   ("src.openalpha.universe_filter",             "evaluate_universe_candidate"),
        "filter_universe_candidates":    ("src.openalpha.universe_filter",             "filter_universe_candidates"),
        "infer_asset_type":              ("src.openalpha.universe_filter",             "infer_asset_type"),
        "load_universe_rules":           ("src.openalpha.universe_filter",             "load_universe_rules"),
        "load_runtime_settings":         ("src.openalpha.settings",                    "load_runtime_settings"),
        "get_float_setting":             ("src.openalpha.settings",                    "get_float_setting"),
    }
    if name in _LAZY:
        mod_path, attr = _LAZY[name]
        obj = getattr(_importlib.import_module(mod_path), attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return all public names for IDE auto-completion."""
    return sorted(set(list(__all__) + ["__version__"]))
