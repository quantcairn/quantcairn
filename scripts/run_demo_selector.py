#!/usr/bin/env python3
"""Run the QuantCairn selection pipeline in DEMO mode.

Uses deterministic synthetic OHLCV data — no API keys, no network,
no broker connection required.  Results are fully reproducible.

Usage::

    .venv/bin/python scripts/run_demo_selector.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.openalpha.demo_data import get_demo_provider, DemoDataProvider
from src.openalpha.selector import AIStrategySelector
from src.openalpha.funnel_tracker import FunnelTracker


def _patch_scorer_for_demo(selector: AIStrategySelector, provider: DemoDataProvider) -> None:
    """Replace the scorer's _load_history with demo data provider.

    This ensures the scorer receives deterministic OHLCV data instead of
    trying to fetch from Yahoo Finance / PriceFetcher.
    """
    import pandas as pd

    original_load_history = selector.scorer._load_history

    def _demo_load_history(symbol: str) -> pd.DataFrame:
        try:
            return provider.get_ohlcv(symbol)
        except KeyError:
            # Symbol not in demo universe — return empty, fallback will handle it
            return pd.DataFrame()

    selector.scorer._load_history = _demo_load_history
    # Store reference for cleanup (not strictly needed but good practice)
    selector._original_load_history = original_load_history  # type: ignore[attr-defined]


def _patch_quality_context_for_demo(selector_module, provider: DemoDataProvider):
    """Replace _QualityFilterContext with a demo version that returns
    good-quality data for all demo symbols."""
    from src.openalpha.selector import _QualityFilterContext

    original_qfc = selector_module._QualityFilterContext

    class DemoQualityContext:
        """Returns synthetic but valid quality metrics for demo symbols."""

        def __init__(self):
            self._queries = 0

        def history_metrics(self, symbol: str):
            """Return (avg_volume_10, three_day_change_pct, current_price)."""
            upper = str(symbol).strip().upper()
            df = provider.get_ohlcv(upper) if upper in provider.symbols else None
            if df is not None and len(df) >= 10:
                avg_vol = float(df["Volume"].tail(10).mean())
                closes = df["Close"].values
                if len(closes) >= 4 and closes[-4] > 0:
                    change = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0
                else:
                    change = 0.0
                price = float(closes[-1])
                return (avg_vol, change, price)
            return (2_000_000, 0.0, 50.0)

        def quote_metrics(self, symbol: str):
            """Return (price, bid, ask, bid_ask_confirmed) with tight spread."""
            upper = str(symbol).strip().upper()
            price = provider.price_at(upper) if upper in provider.symbols else None
            price = price or 50.0
            # Tight 0.02% spread — well within 0.5% threshold
            spread = price * 0.0002
            return (price, price - spread, price + spread, True)

        def close(self):
            pass

    selector_module._QualityFilterContext = DemoQualityContext
    return original_qfc


def main() -> None:
    print("=" * 60)
    print("  QuantCairn Demo Selection")
    print("  (deterministic synthetic data — no API keys required)")
    print("=" * 60)
    print()

    # ── Setup ────────────────────────────────────────────────────────────
    # Force demo run_mode so quality filters stay relaxed
    os.environ["OPENALPHA_LIVE_DATA"] = "0"
    os.environ["OPENALPHA_UNIVERSE"] = "sample"
    os.environ["OPENALPHA_TOP_K"] = "5"
    os.environ["OPENALPHA_MAX_SYMBOLS"] = "10"
    os.environ["OPENALPHA_FETCH_NEWS"] = "0"
    os.environ["OPENALPHA_FAST_START_ONLY"] = "1"

    provider = get_demo_provider()
    demo_symbols = provider.symbols

    print(f"Demo Universe: {', '.join(demo_symbols)}")
    print(f"Data rows per symbol: 252 (~1 year of daily bars)")
    print()

    # ── Monkey-patch preflight to return DEMO mode ───────────────────────
    import src.openalpha.preflight as preflight_module

    _original_run_preflight = preflight_module.run_preflight

    def _demo_preflight(dry_run: bool = True):
        return type("DemoPF", (), {
            "to_dict": lambda self: {
                "market_state": "DEMO",
                "run_mode": "DEMO",
                "is_trading_day": True,
                "session_label": "DEMO",
                "session_reason": "demo_mode",
                "current_session_date": "2026-07-25",
                "previous_session_date": "2026-07-24",
                "symbols_checked": 5,
                "quotes_available": 5,
                "ohlcv_available": 5,
                "quote_coverage_pct": 100.0,
                "ohlcv_coverage_pct": 100.0,
            },
        })()

    preflight_module.run_preflight = _demo_preflight  # type: ignore[attr-access]

    # ── Create selector with mocked universe ─────────────────────────────
    import src.openalpha.selector as selector_module

    original_qfc = _patch_quality_context_for_demo(selector_module, provider)

    selector = AIStrategySelector()
    selector.selection_size = 5
    selector.universe._load_local_snapshot = lambda: list(demo_symbols)
    selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}

    # Patch scorer to use demo data
    _patch_scorer_for_demo(selector, provider)

    # ── Run selection ────────────────────────────────────────────────────
    print("Running 9-stage pipeline with demo data...")
    print()

    result = selector.run_selection(write_configs=False)

    # ── Print results ────────────────────────────────────────────────────
    print("=" * 60)
    print("  DEMO SELECTION RESULTS")
    print("=" * 60)
    print(f"  Run Mode:      {result['run_mode']}")
    print(f"  Candidate Type: {result['candidate_type']}")
    print(f"  Formal Candidates: {len(result['formal_candidates'])}")
    print(f"  Preview Candidates: {len(result['preview_candidates'])}")
    print(f"  Quality Fallback:  {result['quality_fallback_active']}")
    print()

    top_items = result.get("top5", [])
    if top_items:
        print("  TOP Candidates:")
        for i, item in enumerate(top_items, 1):
            ticker = item.get("ticker", "?")
            score_val = item.get("score") or item.get("base_score", 0)
            sector_val = item.get("sector", "?")
            source = item.get("data_source", "demo")
            print(f"    {i}. {ticker:<6s}  score={float(score_val):.1f}  "
                  f"sector={sector_val}  source={source}")
    else:
        print("  No TOP candidates produced.")
        print("  (This is expected if strict quality checks reject all demo data.)")

    print()
    print("─" * 60)
    print("  Demo mode: no API keys required.")
    print("  Full pipeline output → artifacts/selection/funnel_debug.json")
    print("=" * 60)

    # ── Cleanup ──────────────────────────────────────────────────────────
    preflight_module.run_preflight = _original_run_preflight  # type: ignore[attr-access]
    selector_module._QualityFilterContext = original_qfc


if __name__ == "__main__":
    main()
