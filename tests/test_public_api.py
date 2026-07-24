"""Verify the quantcairn public API surface.

Tests that:
  1. All __all__ entries exist and are importable
  2. Top-level and sub-module paths return identical objects
  3. Version is accessible
  4. Old src.openalpha imports remain compatible
  5. New imports do not break when accessed from top-level
"""

from __future__ import annotations

import pytest


class TestPublicApiAll:
    """Every name in quantcairn.__all__ must resolve to a real object."""

    @pytest.fixture(autouse=True)
    def _import(self):
        import quantcairn
        self.q = quantcairn

    def test_all_names_resolve(self):
        for name in self.q.__all__:
            obj = getattr(self.q, name, None)
            assert obj is not None, f"__all__ entry '{name}' is None"

    def test_all_names_are_callable_or_classes(self):
        callables_or_classes = 0
        for name in self.q.__all__:
            obj = getattr(self.q, name)
            if callable(obj) or isinstance(obj, type):
                callables_or_classes += 1
        # Most should be callable; some may be list constants
        assert callables_or_classes >= 15, (
            f"Expected ≥15 callable/class items in __all__, got {callables_or_classes}"
        )

    def test_demo_constants_are_accessible(self):
        assert self.q.DEMO_SYMBOLS == ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]

    def test_version(self):
        assert self.q.__version__ == "0.12.0"


class TestTopLevelVsSubModule:
    """Top-level imports and sub-module imports must return the same object."""

    def test_selector(self):
        from quantcairn import AIStrategySelector
        from quantcairn.selector import AIStrategySelector as AIS2
        assert AIStrategySelector is AIS2

    def test_funnel_tracker(self):
        from quantcairn import FunnelTracker
        from quantcairn.funnel_tracker import FunnelTracker as FT2
        assert FunnelTracker is FT2

    def test_demo_provider(self):
        from quantcairn import DemoDataProvider
        from quantcairn.demo_data import DemoDataProvider as DP2
        assert DemoDataProvider is DP2

    def test_preflight_report(self):
        from quantcairn import PreflightReport
        from quantcairn.preflight import PreflightReport as PR2
        assert PreflightReport is PR2

    def test_score_candidate(self):
        from quantcairn import score_candidate
        from quantcairn.candidate_ranking import score_candidate as SC2
        assert score_candidate is SC2

    def test_evaluate_universe_candidate(self):
        from quantcairn import evaluate_universe_candidate
        assert callable(evaluate_universe_candidate)

    def test_check_data_availability(self):
        from quantcairn import check_data_availability
        assert callable(check_data_availability)

    def test_load_runtime_settings(self):
        from quantcairn import load_runtime_settings
        assert callable(load_runtime_settings)


class TestBackwardCompatibility:
    """src.openalpha imports must still work unchanged."""

    def test_selector_ai_strategy_selector(self):
        from src.openalpha.selector import AIStrategySelector
        from quantcairn import AIStrategySelector as AIS
        assert AIStrategySelector is AIS

    def test_demo_data_provider(self):
        from src.openalpha.demo_data import DemoDataProvider
        from quantcairn import DemoDataProvider as DP
        assert DemoDataProvider is DP

    def test_funnel_tracker(self):
        from src.openalpha.funnel_tracker import FunnelTracker
        from quantcairn import FunnelTracker as FT
        assert FunnelTracker is FT

    def test_preflight(self):
        from src.openalpha.preflight import run_preflight
        from quantcairn import run_preflight as RP
        assert run_preflight is RP

    def test_candidate_ranking(self):
        from src.openalpha.candidate_ranking import score_candidate
        from quantcairn import score_candidate as SC
        assert score_candidate is SC


class TestDemoInstantiation:
    """The quantcairn top-level namespace allows creating demo objects."""

    def test_create_provider_from_top_level(self):
        from quantcairn import DemoDataProvider
        provider = DemoDataProvider()
        assert provider.symbols == ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]
        df = provider.get_ohlcv("AAPL")
        assert len(df) == 252

    def test_create_provider_from_sub_module(self):
        from quantcairn.demo_data import DemoDataProvider
        provider = DemoDataProvider()
        assert provider.symbols == ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]


class TestDirAndCompletion:
    """__dir__ should return all public names for IDE completion."""

    def test_dir_contains_all_members(self):
        import quantcairn
        d = dir(quantcairn)
        for name in quantcairn.__all__:
            assert name in d, f"'{name}' missing from dir(quantcairn)"

    def test_version_in_dir(self):
        import quantcairn
        assert "__version__" in dir(quantcairn)


class TestNoNewNamespaceConflicts:
    """quantcairn namespace must not shadow unrelated modules."""

    def test_import_does_not_shadow(self):
        """Importing quantcairn should not affect ability to import other modules."""
        import sys
        import quantcairn  # noqa: F401
        assert "quantcairn" in sys.modules
        # Should still be able to import unrelated stdlib
        import json
        assert json is not None


class TestUnknownAttrRaises:
    """Accessing non-existent attribute must raise clean error."""

    def test_unknown_attr(self):
        import quantcairn
        with pytest.raises(AttributeError):
            _ = quantcairn.THIS_DOES_NOT_EXIST
