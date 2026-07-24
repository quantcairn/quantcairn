"""Tests for run_mode-aware quality filtering.

Scenarios:
  A. FULL mode + missing bid/ask → candidate rejected
  B. EOD_ONLY mode + missing bid/ask → candidate survives
  C. Fallback candidate with avg_daily_volume_hint + price_midpoint_hint
     passes DATA_QUALITY in EOD mode
  D. AFTER_MARKET run produces TOP candidates instead of empty slots
  E. FULL mode + all quality rejected → quality_fallback_active = True
  F. EOD_ONLY mode + quality rejected all → quality_fallback_active = False
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from contextlib import contextmanager

import pytest


@contextmanager
def _with_sample_universe():
    prev = os.environ.get("OPENALPHA_UNIVERSE")
    os.environ["OPENALPHA_UNIVERSE"] = "sample"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("OPENALPHA_UNIVERSE", None)
        else:
            os.environ["OPENALPHA_UNIVERSE"] = prev


def _candidate(ticker: str, score: float, **extra) -> dict:
    base = {
        "ticker": ticker,
        "sector": "Technology",
        "score": score,
        "base_score": score,
        "volatility_score": 75.0,
        "volume_score": 72.0,
        "trend_fit_score": 68.0,
        "repeatability_score": 66.0,
        "drawdown_safety_score": 64.0,
        "correlation_penalty": 0.0,
        "range_low": 20.0,
        "range_high": 24.0,
        "suggested_range": "$20.00 - $24.00",
        "risk": {"stop_loss_pct": 1.5},
        "series": {"returns": [0.01] * 30},
    }
    base.update(extra)
    return base


# ═══════════════════════════════════════════════════════════════════════════════
# Test A/B/C: Unit tests on _apply_quality_filters_with_report
# ═══════════════════════════════════════════════════════════════════════════════
# These test the quality filter directly — no preflight mocking needed.

class TestQualityFilterModeAwareness:
    """Direct unit tests on _apply_quality_filters_with_report with various modes."""

    def setup_method(self):
        """Set up fake contexts before each test."""
        self._original_qfc = getattr(
            __import__("src.openalpha.selector", fromlist=["_QualityFilterContext"]),
            "_QualityFilterContext", None,
        )

    def _make_no_quote_context(self):
        """Context that returns NO bid/ask (like EOD/premarket)."""
        class Ctx:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)  # good OHLCV
            def quote_metrics(self, symbol):
                return (50.0, None, None, False)  # no spread
            def close(self):
                pass
        return Ctx

    def _make_good_context(self):
        """Context that returns everything (like during market hours)."""
        class Ctx:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)  # tight spread
            def close(self):
                pass
        return Ctx

    def _make_empty_context(self):
        """Context that returns nothing (no data at all)."""
        class Ctx:
            def history_metrics(self, symbol):
                return (None, None, None)
            def quote_metrics(self, symbol):
                return (None, None, None, False)
            def close(self):
                pass
        return Ctx

    # ── Test A: FULL + no bid/ask → rejected ─────────────────────────────

    def test_full_mode_no_bid_ask_rejects(self, monkeypatch):
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_no_quote_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [_candidate("T1", 85.0)]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="FULL"
        )
        assert len(filtered) == 0
        assert any(
            r.get("reason") == "spread_unavailable"
            for r in report.get("rows", [])
        )

    def test_full_mode_good_data_passes(self, monkeypatch):
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_good_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [_candidate("T2", 88.0)]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="FULL"
        )
        assert len(filtered) >= 1
        assert filtered[0]["ticker"] == "T2"

    # ── Test B: EOD modes + no bid/ask → survives ────────────────────────

    def test_eod_only_mode_no_bid_ask_survives(self, monkeypatch):
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_no_quote_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [_candidate("T3", 90.0)]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="EOD_ONLY"
        )
        assert len(filtered) >= 1
        assert filtered[0]["ticker"] == "T3"

    def test_after_market_mode_no_bid_ask_survives(self, monkeypatch):
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_no_quote_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [_candidate("T4", 92.0)]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="AFTER_MARKET"
        )
        assert len(filtered) >= 1

    def test_degraded_mode_no_bid_ask_survives(self, monkeypatch):
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_no_quote_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [_candidate("T5", 95.0)]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="DEGRADED"
        )
        assert len(filtered) >= 1

    # ── Safety: EOD still enforces basic checks ──────────────────────────

    def test_eod_still_rejects_no_price_and_no_hints(self, monkeypatch):
        """Zero data + no hints → rejected even in EOD mode."""
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_empty_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [_candidate("GHOST", 60.0)]  # no hints
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="EOD_ONLY"
        )
        assert len(filtered) == 0

    def test_eod_rejects_volume_below_500k(self, monkeypatch):
        """Volume < 500K → rejected in all modes."""
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )

        class LowVolCtx:
            def history_metrics(self, symbol):
                return (100_000, 0.5, 50.0)  # 100K vol
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", LowVolCtx
        )
        candidates = [_candidate("LOWV", 70.0)]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="EOD_ONLY"
        )
        assert len(filtered) == 0
        assert report["removed_by_volume_filter"] >= 1

    # ── Test C: Fallback hints pass quality ──────────────────────────────

    def test_fallback_with_hints_passes_eod(self, monkeypatch):
        """Candidate with data_source=fallback + hints passes in EOD mode."""
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_empty_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [
            _candidate(
                "FB1", 88.0,
                data_source="fallback",
                avg_daily_volume_hint=5_000_000,
                price_midpoint_hint=50.0,
            )
        ]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="EOD_ONLY"
        )
        assert len(filtered) >= 1
        passed = [r for r in report.get("rows", []) if r.get("symbol") == "FB1"]
        assert passed[0]["removed"] is False

    def test_fallback_low_volume_hint_rejected(self, monkeypatch):
        """Even with hints, volume < 500K should fail."""
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_empty_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [
            _candidate(
                "LOW", 75.0,
                data_source="fallback",
                avg_daily_volume_hint=100_000,
                price_midpoint_hint=50.0,
            )
        ]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="EOD_ONLY"
        )
        assert len(filtered) == 0
        assert report["removed_by_volume_filter"] >= 1

    def test_fallback_in_full_mode_without_live_rejected(self, monkeypatch):
        """Same fallback in FULL mode → rejected due to missing spread."""
        from src.openalpha.selector import (
            _apply_quality_filters_with_report,
        )
        FakeCtx = self._make_empty_context()
        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", FakeCtx
        )
        candidates = [
            _candidate(
                "FB2", 88.0,
                data_source="fallback",
                avg_daily_volume_hint=5_000_000,
                price_midpoint_hint=50.0,
            )
        ]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="FULL"
        )
        assert len(filtered) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test D: End-to-end AFTER_MARKET produces TOP candidates
# ═══════════════════════════════════════════════════════════════════════════════

class TestAfterMarketProducesTopCandidates:
    """Integration tests: verify the full pipeline produces results in EOD mode."""

    @pytest.mark.slow
    def test_after_market_produces_top_slots(self, monkeypatch):
        """AFTER_MARKET mode: pipeline produces non-empty Formal Candidates."""
        from src.openalpha.selector import AIStrategySelector

        # Patch preflight to return AFTER_MARKET
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("FP", (), {
                "to_dict": lambda self: {
                    "market_state": "AFTER_HOURS",
                    "run_mode": "AFTER_MARKET",
                    "is_trading_day": True,
                    "session_label": "AFTERHOURS",
                    "session_reason": "after_hours",
                    "current_session_date": "2026-07-24",
                    "previous_session_date": "2026-07-23",
                },
            })(),
        )

        # Good live data (full market context)
        class GoodCtx:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", GoodCtx
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            import src.openalpha.selector as sel_mod
            monkeypatch.setattr(sel_mod, "LOG_DIR", Path(tmpdir) / "logs")

            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 2
                selector.universe._load_local_snapshot = (
                    lambda: ["AAPL", "MSFT"]
                )
                selector.news.collect_for_symbols = (
                    lambda symbols: {s: [] for s in symbols}
                )
                selector._score_with_live_flag = (
                    lambda symbols, news_map, live_enabled: [
                        _candidate("AAPL", 95.0),
                        _candidate("MSFT", 85.0),
                    ]
                )

                result = selector.run_selection(write_configs=False)

                assert result["run_mode"] == "AFTER_MARKET"
                assert result["candidate_type"] == "RESEARCH_ONLY"
                assert result["quality_fallback_active"] is False
                assert len(result["formal_candidates"]) > 0
                assert len(result["top5"]) > 0

    @pytest.mark.slow
    def test_full_mode_quality_all_rejected_sets_fallback(self, monkeypatch):
        """FULL mode + all quality-rejected → quality_fallback_active=True."""
        from src.openalpha.selector import AIStrategySelector

        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("FP", (), {
                "to_dict": lambda self: {
                    "market_state": "MARKET_OPEN",
                    "run_mode": "FULL",
                    "is_trading_day": True,
                },
            })(),
        )

        # No bid/ask → spread_unavailable
        class NoQuoteCtx:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, None, None, False)
            def close(self):
                pass

        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", NoQuoteCtx
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            import src.openalpha.selector as sel_mod
            monkeypatch.setattr(sel_mod, "LOG_DIR", Path(tmpdir) / "logs")

            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 2
                selector.universe._load_local_snapshot = (
                    lambda: ["AAPL", "NVDA"]
                )
                selector.news.collect_for_symbols = (
                    lambda symbols: {s: [] for s in symbols}
                )
                selector._score_with_live_flag = (
                    lambda symbols, news_map, live_enabled: [
                        _candidate("AAPL", 95.0),
                        _candidate("NVDA", 88.0),
                    ]
                )

                result = selector.run_selection(write_configs=False)

                assert result["run_mode"] == "FULL"
                assert result["quality_fallback_active"] is True
                assert result["formal_candidates"] == []
                assert len(result["preview_candidates"]) > 0

    @pytest.mark.slow
    def test_eod_mode_produces_formal_candidates(self, monkeypatch):
        """EOD_ONLY mode: relaxed path → formal candidates exist."""
        from src.openalpha.selector import AIStrategySelector

        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("FP", (), {
                "to_dict": lambda self: {
                    "market_state": "CLOSED",
                    "run_mode": "EOD_ONLY",
                    "is_trading_day": True,
                },
            })(),
        )

        # No live quotes at all — EOD mode should survive
        class NoQuoteCtx:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, None, None, False)
            def close(self):
                pass

        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", NoQuoteCtx
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            import src.openalpha.selector as sel_mod
            monkeypatch.setattr(sel_mod, "LOG_DIR", Path(tmpdir) / "logs")

            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 1
                selector.universe._load_local_snapshot = (
                    lambda: ["AAPL"]
                )
                selector.news.collect_for_symbols = (
                    lambda symbols: {s: [] for s in symbols}
                )
                selector._score_with_live_flag = (
                    lambda symbols, news_map, live_enabled: [
                        _candidate("AAPL", 95.0),
                    ]
                )

                result = selector.run_selection(write_configs=False)

                assert result["run_mode"] == "EOD_ONLY"
                assert result["quality_fallback_active"] is False
                # EOD mode: candidate survives relaxed quality check
                assert len(result["formal_candidates"]) > 0
                assert len(result["top5"]) > 0
