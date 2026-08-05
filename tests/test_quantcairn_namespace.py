"""Tests for the quantcairn namespace compatibility layer.

Verifies:
  1. quantcairn imports work alongside src.openalpha imports
  2. Both reference the same implementation objects
  3. Existing src/openalpha/ imports are unbroken
  4. The version string matches the installed package
"""

from __future__ import annotations

import pytest


class TestQuantcairnNamespace:
    """Verifies quantcairn namespace imports against src.openalpha originals."""

    # ── identity checks: same class, just different import path ──────────

    def test_selector_identity(self):
        from quantcairn.selector import AIStrategySelector
        from src.openalpha.selector import AIStrategySelector as AIS
        assert AIStrategySelector is AIS

    def test_funnel_tracker_identity(self):
        from quantcairn.funnel_tracker import FunnelTracker
        from src.openalpha.funnel_tracker import FunnelTracker as FT
        assert FunnelTracker is FT

    def test_demo_provider_identity(self):
        from quantcairn.demo_data import DemoDataProvider, DEMO_SYMBOLS
        from src.openalpha.demo_data import DemoDataProvider as DP, DEMO_SYMBOLS as DS
        assert DemoDataProvider is DP
        assert DEMO_SYMBOLS == DS

    def test_preflight_identity(self):
        from quantcairn.preflight import PreflightReport
        from src.openalpha.preflight import PreflightReport as PR
        assert PreflightReport is PR

    def test_candidate_ranking_identity(self):
        from quantcairn.candidate_ranking import score_candidate
        from src.openalpha.candidate_ranking import score_candidate as SC
        assert score_candidate is SC

    def test_universe_filter_identity(self):
        from quantcairn.universe_filter import evaluate_universe_candidate
        from src.openalpha.universe_filter import evaluate_universe_candidate as EV
        assert evaluate_universe_candidate is EV

    def test_settings_identity(self):
        from quantcairn.settings import load_runtime_settings
        from src.openalpha.settings import load_runtime_settings as LR
        assert load_runtime_settings is LR

    # ── top-level package features ───────────────────────────────────────

    def test_version(self):
        import quantcairn
        assert quantcairn.__version__ == "0.15.0"

    def test_lazy_top_level_import(self):
        import quantcairn
        # Access a lazy attribute — should resolve and cache
        provider = quantcairn.DemoDataProvider()
        from src.openalpha.demo_data import DemoDataProvider
        assert isinstance(provider, DemoDataProvider)
        # Second access should use cached value
        provider2 = quantcairn.DemoDataProvider()
        assert isinstance(provider2, DemoDataProvider)

    def test_lazy_import_unknown_attr_raises(self):
        import quantcairn
        with pytest.raises(AttributeError):
            _ = quantcairn.NONEXISTENT_NAME

    # ── existing imports unbroken ────────────────────────────────────────

    def test_src_openalpha_still_works(self):
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha.demo_data import DemoDataProvider
        from src.openalpha.funnel_tracker import FunnelTracker
        from src.openalpha.preflight import PreflightReport
        from src.openalpha.data_diagnostics import check_data_availability
        from src.openalpha.candidate_ranking import score_candidate
        from src.openalpha.universe_filter import evaluate_universe_candidate
        from src.openalpha.settings import load_runtime_settings
        # Just verify they all import
        assert AIStrategySelector is not None
        assert DemoDataProvider is not None
        assert FunnelTracker is not None
        assert PreflightReport is not None
        assert check_data_availability is not None
        assert score_candidate is not None
        assert evaluate_universe_candidate is not None
        assert load_runtime_settings is not None

    # ── module-level constants ───────────────────────────────────────────

    def test_demo_constants(self):
        from quantcairn.demo_data import DEMO_SYMBOLS, DEMO_HISTORY_ROWS
        assert isinstance(DEMO_SYMBOLS, list)
        assert len(DEMO_SYMBOLS) == 5
        assert DEMO_HISTORY_ROWS == 252

    def test_selector_functions(self):
        from quantcairn.selector import apply_quality_filters
        assert callable(apply_quality_filters)
