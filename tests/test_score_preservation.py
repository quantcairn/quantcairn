"""Regression test: scores, rank, confidence, data_source, candidate_type
must be preserved through the full pipeline:
  BASE_RANKING → COMPOSITION_FILTER → FORMAL_TOP → report enrichment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


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
# _apply_formal_score_semantics regression
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormalScoreSemantics:
    """_apply_formal_score_semantics must not drop scores for already-formal items."""

    def test_score_is_formal_preserves_score(self):
        """When score_is_formal=True, score must NOT be set to None."""
        from scripts.run_ai_selector import _apply_formal_score_semantics

        item = {
            "ticker": "AAPL",
            "score": 88.5,
            "final_score": 88.5,
            "base_score": 85.0,
            "score_is_formal": True,
            "candidate_type": "RESEARCH_ONLY",
        }
        result = _apply_formal_score_semantics(item)
        assert result["score"] == 88.5, f"Expected score=88.5, got {result['score']}"
        assert result["score_is_formal"] is True
        assert result["score_type"] == "FORMAL"

    def test_score_type_formal_preserves_score(self):
        """When score_type='FORMAL', score must NOT be set to None."""
        from scripts.run_ai_selector import _apply_formal_score_semantics

        item = {
            "ticker": "NVDA",
            "score": 92.0,
            "final_score": 92.0,
            "score_type": "FORMAL",
        }
        result = _apply_formal_score_semantics(item)
        assert result["score"] == 92.0
        assert result["score_is_formal"] is True

    def test_no_eligibility_fields_defaults_to_formal(self):
        """Items without scoring_eligible or formal_scoring_eligibility
        should default to FORMAL (matching score_candidate() semantics)."""
        from scripts.run_ai_selector import _apply_formal_score_semantics

        item = {
            "ticker": "MSFT",
            "score": 75.0,
            "final_score": 75.0,
            # No scoring_eligible, no formal_scoring_eligibility, no score_is_formal
        }
        result = _apply_formal_score_semantics(item)
        # Should default to FORMAL (True), not DIAGNOSTIC (False)
        assert result["score"] is not None, (
            f"score should not be None for item with no eligibility flags"
        )
        assert result["score_is_formal"] is True

    def test_explicitly_non_formal_item_still_nullified(self):
        """When explicitly non-formal, score IS set to None — old behavior preserved."""
        from scripts.run_ai_selector import _apply_formal_score_semantics

        item = {
            "ticker": "BAD",
            "score": 40.0,
            "scoring_eligible": False,
            "formal_scoring_eligibility": False,
            "score_is_formal": False,
        }
        result = _apply_formal_score_semantics(item)
        assert result["score"] is None
        assert result["score_type"] == "DIAGNOSTIC"

    def test_score_is_formal_overrides_explicit_false_eligibility(self):
        """score_is_formal=True should win even if eligibility fields say False."""
        from scripts.run_ai_selector import _apply_formal_score_semantics

        item = {
            "ticker": "OVERRIDE",
            "score": 65.0,
            "scoring_eligible": False,
            "formal_scoring_eligibility": False,
            "score_is_formal": True,
        }
        result = _apply_formal_score_semantics(item)
        assert result["score"] == 65.0, f"score_is_formal=True should preserve score"
        assert result["score_type"] == "FORMAL"


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline-through test: selector → top5 has scores
# ═══════════════════════════════════════════════════════════════════════════════

class TestSelectorPipelineScorePreservation:
    """Full pipeline: BASE_RANKING → FORMAL_TOP preserves scores."""

    def test_eod_relaxed_top5_has_scores(self, monkeypatch):
        """EOD relaxed mode: top5 items must have non-None scores."""
        import os, tempfile
        from pathlib import Path
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        # Force EOD so relaxed path is used
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {
                    "market_state": "CLOSED",
                    "run_mode": "EOD_ONLY",
                    "is_trading_day": True,
                },
            })(),
        )

        class GoodContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", GoodContext)

        os.environ["OPENALPHA_UNIVERSE"] = "sample"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
                selector = AIStrategySelector()
                selector.selection_size = 3
                selector.universe._load_local_snapshot = lambda: ["AAPL", "MSFT", "NVDA"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                    _candidate("MSFT", 85.0),
                    _candidate("NVDA", 75.0),
                ]
                result = selector.run_selection(write_configs=False)

                for item in result["top5"]:
                    ticker = item.get("ticker", "?")
                    sc = item.get("score")
                    assert sc is not None, f"{ticker}: score should not be None"
                    assert sc > 0, f"{ticker}: score={sc} should be > 0"
        finally:
            os.environ.pop("OPENALPHA_UNIVERSE", None)

    def test_paper_mode_top5_has_confidence(self, monkeypatch):
        """PAPER mode: top5 items must have confidence, data_source, candidate_type."""
        import os, tempfile
        from pathlib import Path
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {
                    "market_state": "CLOSED",
                    "run_mode": "EOD_ONLY",
                    "is_trading_day": True,
                },
            })(),
        )

        class GoodContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", GoodContext)
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "PAPER")

        os.environ["OPENALPHA_UNIVERSE"] = "sample"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
                selector = AIStrategySelector()
                selector.selection_size = 2
                selector.universe._load_local_snapshot = lambda: ["AAPL", "MSFT"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                    _candidate("MSFT", 85.0),
                ]
                result = selector.run_selection(write_configs=False)

                for item in result["top5"]:
                    ticker = item.get("ticker", "?")
                    assert item.get("score") is not None, f"{ticker}: no score"
                    assert "confidence" in item, f"{ticker}: no confidence"
                    assert "data_source" in item, f"{ticker}: no data_source"
                    assert "candidate_type" in item, f"{ticker}: no candidate_type"
                    assert item["candidate_type"] == "PAPER_ELIGIBLE"
        finally:
            os.environ.pop("OPENALPHA_UNIVERSE", None)
