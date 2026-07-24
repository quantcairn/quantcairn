"""Tests for execution mode separation (LIVE / PAPER / RESEARCH).

Verifies:
  1. execution_mode resolves correctly from env var and preflight
  2. LIVE mode: strict quality, LIVE_TRADABLE candidate_type
  3. PAPER mode: relaxed quality, PAPER_ELIGIBLE candidate_type, confidence recorded
  4. RESEARCH mode: relaxed quality, RESEARCH_ONLY candidate_type
  5. PAPER mode produces formal candidates (not fallback) even with relaxed quality
  6. Confidence is computed for PAPER candidates
  7. execution_mode flows to the return dict
  8. config_writer receives execution_mode and candidate_type
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from contextlib import contextmanager

import pytest

from src.openalpha.selector import (
    _resolve_execution_mode,
    _quality_mode_is_strict,
    EXECUTION_MODES,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: execution_mode resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutionModeResolution:
    """_resolve_execution_mode picks the right mode."""

    def test_env_var_paper_overrides_everything(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "PAPER")
        assert _resolve_execution_mode("FULL") == "PAPER"
        assert _resolve_execution_mode("EOD_ONLY") == "PAPER"
        assert _resolve_execution_mode("DEGRADED") == "PAPER"

    def test_env_var_live(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "LIVE")
        assert _resolve_execution_mode("FULL") == "LIVE"

    def test_env_var_research(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "RESEARCH")
        assert _resolve_execution_mode("FULL") == "RESEARCH"

    def test_env_var_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "paper")
        assert _resolve_execution_mode("FULL") == "PAPER"

    def test_default_full_to_live(self, monkeypatch):
        monkeypatch.delenv("QUANTCAIRN_EXECUTION_MODE", raising=False)
        assert _resolve_execution_mode("FULL") == "LIVE"

    def test_default_non_full_to_research(self, monkeypatch):
        monkeypatch.delenv("QUANTCAIRN_EXECUTION_MODE", raising=False)
        assert _resolve_execution_mode("EOD_ONLY") == "RESEARCH"
        assert _resolve_execution_mode("AFTER_MARKET") == "RESEARCH"
        assert _resolve_execution_mode("DEGRADED") == "RESEARCH"
        assert _resolve_execution_mode("DEMO") == "RESEARCH"

    def test_unknown_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "INVALID")
        assert _resolve_execution_mode("FULL") == "LIVE"

    def test_execution_modes_constant(self):
        assert EXECUTION_MODES == ("LIVE", "PAPER", "RESEARCH")


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: quality mode strictness unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestQualityStrictness:
    """_quality_mode_is_strict still depends on run_mode, not execution_mode."""

    def test_full_is_strict(self):
        assert _quality_mode_is_strict("FULL") is True

    def test_non_full_is_not_strict(self):
        for mode in ("AFTER_MARKET", "EOD_ONLY", "DEGRADED", "DEMO", "", "UNKNOWN"):
            assert _quality_mode_is_strict(mode) is False


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: PAPER mode execution
# ═══════════════════════════════════════════════════════════════════════════════

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


class TestPaperMode:
    """PAPER execution mode: relaxed quality, PAPER_ELIGIBLE candidates."""

    def test_paper_mode_relaxed_quality_produces_candidates(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "PAPER")
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        class GoodContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", GoodContext)
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

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 2
                selector.universe._load_local_snapshot = lambda: ["AAPL", "MSFT"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                    _candidate("MSFT", 85.0),
                ]
                result = selector.run_selection(write_configs=False)

                assert result["execution_mode"] == "PAPER"
                assert result["candidate_type"] == "PAPER_ELIGIBLE"
                assert len(result["formal_candidates"]) > 0
                assert result["quality_fallback_active"] is False

    def test_paper_mode_candidates_have_confidence(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "PAPER")
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        class GoodContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", GoodContext)
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {"market_state": "CLOSED", "run_mode": "EOD_ONLY", "is_trading_day": True},
            })(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 1
                selector.universe._load_local_snapshot = lambda: ["AAPL"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                ]
                result = selector.run_selection(write_configs=False)

                for item in result["top5"]:
                    assert "confidence" in item, f"PAPER candidate should have confidence: {item}"
                    assert item["confidence"] > 0, f"Confidence should be >0: {item['confidence']}"

    def test_paper_mode_no_bid_ask_still_survives(self, monkeypatch):
        """PAPER mode: even without bid/ask, candidates survive quality check."""
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "PAPER")
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        class NoQuoteContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, None, None, False)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", NoQuoteContext)
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {"market_state": "CLOSED", "run_mode": "EOD_ONLY", "is_trading_day": True},
            })(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 1
                selector.universe._load_local_snapshot = lambda: ["AAPL"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                ]
                result = selector.run_selection(write_configs=False)

                assert result["execution_mode"] == "PAPER"
                # Even though quality relaxed rejects all (EOD mode), PAPER should still
                # produce formal candidates via the relaxed path
                assert len(result["formal_candidates"]) > 0


class TestResearchMode:
    """RESEARCH execution mode: relaxed quality, RESEARCH_ONLY candidates."""

    def test_research_mode_default_no_trade_eligibility(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "RESEARCH")
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        class GoodContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", GoodContext)
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {"market_state": "CLOSED", "run_mode": "EOD_ONLY", "is_trading_day": True},
            })(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 1
                selector.universe._load_local_snapshot = lambda: ["AAPL"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                ]
                result = selector.run_selection(write_configs=False)

                assert result["execution_mode"] == "RESEARCH"
                assert result["candidate_type"] == "RESEARCH_ONLY"


class TestLiveMode:
    """LIVE execution mode: strict quality, LIVE_TRADABLE candidates."""

    def test_live_mode_strict_quality_rejects_no_bid_ask(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "LIVE")
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        class NoQuoteContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, None, None, False)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", NoQuoteContext)
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {"market_state": "MARKET_OPEN", "run_mode": "FULL", "is_trading_day": True},
            })(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 1
                selector.universe._load_local_snapshot = lambda: ["AAPL"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                ]
                result = selector.run_selection(write_configs=False)

                assert result["execution_mode"] == "LIVE"
                assert result["quality_fallback_active"] is True
                assert len(result["formal_candidates"]) == 0

    def test_live_mode_good_data_produces_live_tradable(self, monkeypatch):
        monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "LIVE")
        from src.openalpha.selector import AIStrategySelector
        from src.openalpha import selector as selector_module

        class GoodContext:
            def history_metrics(self, symbol):
                return (2_000_000, 0.5, 50.0)
            def quote_metrics(self, symbol):
                return (50.0, 49.98, 50.02, True)
            def close(self):
                pass

        monkeypatch.setattr("src.openalpha.selector._QualityFilterContext", GoodContext)
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {"market_state": "MARKET_OPEN", "run_mode": "FULL", "is_trading_day": True},
            })(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(selector_module, "LOG_DIR", Path(tmpdir) / "logs")
            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 1
                selector.universe._load_local_snapshot = lambda: ["AAPL"]
                selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAPL", 95.0),
                ]
                result = selector.run_selection(write_configs=False)

                assert result["execution_mode"] == "LIVE"
                assert result["candidate_type"] == "LIVE_TRADABLE"
                assert len(result["formal_candidates"]) > 0


class TestConfigWriterExecutionMode:
    """Config writer propagates execution_mode and candidate_type."""

    def test_config_writes_execution_mode_and_candidate_type(self, monkeypatch, tmp_path):
        from src.openalpha.config_writer import write_top_configs, _top_config_dir
        monkeypatch.setattr("src.openalpha.config_writer._top_config_dir", lambda _=None: str(tmp_path))
        monkeypatch.setattr("src.openalpha.config_writer._load_existing_mode", lambda slot, default: "paper")

        items = [{
            "ticker": "AAPL",
            "range_low": 170.0,
            "range_high": 230.0,
            "score": 88.0,
            "confidence": 0.72,
            "execution_mode": "PAPER",
            "candidate_type": "PAPER_ELIGIBLE",
            "data_source": "eod_validated",
            "risk": {"stop_loss_pct": 1.5},
            "size": 100,
        }]
        write_top_configs(items, selection_run_id="test-run")

        import yaml
        top1 = yaml.safe_load((tmp_path / "TOP1.yaml").read_text(encoding="utf-8"))
        sel = top1["selection"]
        assert sel["execution_mode"] == "PAPER"
        assert sel["candidate_type"] == "PAPER_ELIGIBLE"
        assert sel["data_source"] == "eod_validated"
        assert sel["confidence"] == 0.72


class TestComputeConfidence:
    """_compute_confidence produces sensible values."""

    def test_live_data_max_confidence(self):
        from src.openalpha.selector import _compute_confidence
        item = {"score": 90.0, "data_source": "live"}
        conf = _compute_confidence(item, "live")
        assert conf >= 0.70  # 90/100 * 0.85 = 0.765

    def test_fallback_data_low_confidence(self):
        from src.openalpha.selector import _compute_confidence
        item = {"score": 80.0, "data_source": "fallback"}
        conf = _compute_confidence(item, "fallback")
        assert conf <= 0.40  # 80/100 * 0.30 = 0.24

    def test_eod_fallback_mid_confidence(self):
        from src.openalpha.selector import _compute_confidence
        item = {"score": 75.0, "data_source": "eod_fallback"}
        conf = _compute_confidence(item, "eod_fallback")
        assert 0.20 < conf < 0.50  # 75/100 * 0.40 = 0.30

    def test_confidence_clamped_0_to_1(self):
        from src.openalpha.selector import _compute_confidence
        # Very high score still clamped
        item = {"score": 200.0, "data_source": "live"}
        conf = _compute_confidence(item, "live")
        assert conf <= 1.0
        # Very low score still >= 0
        item2 = {"score": 0.0, "data_source": "fallback"}
        conf2 = _compute_confidence(item2, "fallback")
        assert conf2 >= 0.0
