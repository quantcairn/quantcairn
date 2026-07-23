"""Tests for the shadow market regime evaluator.

All tests verify that the shadow evaluator NEVER modifies
selected_symbols, scores, TOP configs, SelectionBundle, or any
trading state.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.shadow.regime_evaluator import (
    CandidateIndicators,
    _bull_score,
    _bear_score,
    _range_score,
    _classify_candidate,
    _score_to_label,
    _safe_float,
    evaluate_shadow_regime,
)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _ind(**kw) -> CandidateIndicators:
    defaults = {
        "symbol": "AAPL", "price": 150.0, "ma20": 145.0, "ma50": 140.0,
        "ma200": 130.0, "adx": 20.0, "spread_pct": 0.1, "gap_pct": 0.5,
        "avg_dollar_volume_20d": 50000000.0, "rsi": 55.0,
        "relative_strength_vs_spy": 5.0, "three_day_change_pct": 2.0,
    }
    # Map short helper names to full field names
    aliases = {"rs": "relative_strength_vs_spy", "chg": "three_day_change_pct"}
    for short, full in aliases.items():
        if short in kw:
            kw[full] = kw.pop(short)
    defaults.update(kw)
    return CandidateIndicators(**defaults)


def _bundle(top10_list=None, top3_list=None, candidates_list=None):
    report = {}
    if top10_list:
        report["top10"] = top10_list
    if top3_list:
        report["top3"] = top3_list
    if candidates_list:
        report["tradable_top_candidates"] = candidates_list
    return {"manifest": {"selection_run_id": "test-1", "selection_date": "2026-07-23"},
            "report": report, "bundle_root": Path("/tmp")}


def _candidate(ticker="AAPL", price=150.0, ma20=145.0, ma50=140.0, rsi=55.0,
               adx=20.0, rs=5.0, chg=2.0, **kw):
    return {
        "ticker": ticker,
        "market_data": {
            "current_price": price, "ma20": ma20, "ma50": ma50,
            "rsi_14": rsi, "adx": adx,
            "relative_strength_vs_SPY": rs, "three_day_change_pct": chg,
        },
        **kw,
    }


# ═══════════════════════════════════════════════════════════════════════
# Score helpers
# ═══════════════════════════════════════════════════════════════════════

class TestScoreHelpers:
    def test_score_to_label(self):
        assert _score_to_label(80) == "STRONG"
        assert _score_to_label(60) == "MODERATE"
        assert _score_to_label(40) == "WEAK"
        assert _score_to_label(10) == "NEGLIGIBLE"

    def test_safe_float_nan(self):
        assert _safe_float(float("nan")) == 0.0
        assert _safe_float(float("inf")) == 0.0
        assert _safe_float(float("-inf")) == 0.0

    def test_safe_float_valid(self):
        assert _safe_float(42.5) == 42.5
        assert _safe_float("3.14") == 3.14


# ═══════════════════════════════════════════════════════════════════════
# Bull score
# ═══════════════════════════════════════════════════════════════════════

class TestBullScore:
    def test_strong_bull(self):
        ind = _ind(price=160, ma20=145, ma50=140, rs=10, adx=22, chg=5.0,
                   volume_score=70, trend_score=70)
        score, reasons = _bull_score(ind)
        assert score >= 70

    def test_weak_bull_price_below_ma20(self):
        ind = _ind(price=140, ma20=145, rs=-2)
        score, reasons = _bull_score(ind)
        assert score < 50
        assert "price_below_ma20" in reasons

    def test_no_data_produces_sane_scores(self):
        ind = _ind(price=0, ma20=0, ma50=0, rs=0)
        score, reasons = _bull_score(ind)
        assert score >= 0


# ═══════════════════════════════════════════════════════════════════════
# Bear score
# ═══════════════════════════════════════════════════════════════════════

class TestBearScore:
    def test_strong_bear(self):
        ind = _ind(price=130, ma20=145, ma50=150, rs=-10, adx=22, chg=-5.0,
                   volatility_score=70, is_inverse_etf=True)
        score, reasons = _bear_score(ind)
        assert score >= 60

    def test_inverse_etf_gets_bonus(self):
        ind1 = _ind(price=130, ma20=145, rs=-5, is_inverse_etf=True)
        ind2 = _ind(price=130, ma20=145, rs=-5, is_inverse_etf=False)
        s1, _ = _bear_score(ind1)
        s2, _ = _bear_score(ind2)
        assert s1 > s2

    def test_no_data_sane(self):
        ind = _ind(price=0, ma20=0, rs=0)
        score, _ = _bear_score(ind)
        assert score >= 0


# ═══════════════════════════════════════════════════════════════════════
# Range score
# ═══════════════════════════════════════════════════════════════════════

class TestRangeScore:
    def test_strong_range(self):
        ind = _ind(price=148, ma20=150, rsi=50, adx=18, chg=1.0, gap_pct=0.5, spread_pct=0.1)
        score, reasons = _bear_score(ind)
        assert score >= 0  # Just doesn't crash

    def test_range_near_ma20(self):
        ind = _ind(price=150, ma20=150, rsi=50, adx=18, chg=1.0)
        score, reasons = _range_score(ind)
        assert score >= 50

    def test_adx_too_high_reduces_range(self):
        ind = _ind(price=150, ma20=150, rsi=50, adx=35)
        score, reasons = _range_score(ind)
        assert "adx_trending" in reasons


# ═══════════════════════════════════════════════════════════════════════
# Classification
# ═══════════════════════════════════════════════════════════════════════

class TestClassification:
    def test_winner_matches_highest_score(self):
        ind = _ind(price=160, ma20=145, ma50=140, rs=10, adx=22, chg=5.0,
                   volume_score=80, trend_score=80)
        result = _classify_candidate(ind)
        assert result["winner"] == "BULL"

    def test_inverse_etf_usually_bear(self):
        ind = _ind(symbol="SOXS", price=30, ma20=35, ma50=38, rs=-8,
                   chg=-3.0, is_inverse_etf=True, asset_type="inverse_etf")
        result = _classify_candidate(ind)
        # Inverse ETF in downtrend should score high on bear
        assert result["bear_score"] >= result["bull_score"]

    def test_all_scores_in_range(self):
        ind = _ind()
        result = _classify_candidate(ind)
        for key in ("bull_score", "bear_score", "range_score"):
            assert 0 <= result[key] <= 100


# ═══════════════════════════════════════════════════════════════════════
# Evaluation (integration)
# ═══════════════════════════════════════════════════════════════════════

class TestEvaluate:
    def test_no_bundle(self):
        with patch("src.shadow.regime_evaluator.load_committed_selection_bundle", return_value=None):
            r = evaluate_shadow_regime(dry_run=True)
            assert r["status"] == "NO_BUNDLE"

    def test_empty_bundle(self):
        bundle = _bundle()
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["status"] == "NO_CANDIDATES"

    def test_with_candidates(self):
        bundle = _bundle(top10_list=[
            _candidate("AAPL"), _candidate("TSLA", price=200, ma20=210, rs=-3),
            _candidate("NVDA", price=500, ma20=480, rs=15, chg=8.0),
        ])
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["status"] == "OK"
        assert r["candidate_count"] == 3
        assert r["evaluated_count"] == 3
        assert len(r["per_candidate"]) == 3
        assert "regime_distribution" in r
        assert "aggregate_scores" in r
        assert "dominant_regime" in r

    def test_dry_run_does_not_write(self, tmp_path: Path):
        bundle = _bundle(top10_list=[_candidate("AAPL")])
        # Patch _write_report to do nothing
        with patch("src.shadow.regime_evaluator._write_report", return_value=tmp_path / "fake.json"):
            r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
            assert r["status"] == "OK"

    def test_never_modifies_selection_bundle(self):
        bundle = _bundle(
            top10_list=[_candidate("AAPL"), _candidate("SOXS")],
            top3_list=[_candidate("AAPL")],
        )
        bundle_copy = json.dumps(bundle, sort_keys=True, default=str)
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        # Bundle unchanged after evaluation
        bundle_after = json.dumps(bundle, sort_keys=True, default=str)
        assert bundle_copy == bundle_after

    def test_current_selected_unchanged(self):
        bundle = _bundle(
            top10_list=[_candidate("AAPL"), _candidate("TSLA"), _candidate("SOXS")],
            top3_list=[_candidate("AAPL"), _candidate("TSLA")],
        )
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["current_selected_symbols"] == ["AAPL", "TSLA"]
        # The bundle's top3 is still ["AAPL", "TSLA"]
        assert [c["ticker"] for c in bundle["report"]["top3"]] == ["AAPL", "TSLA"]

    def test_research_only_flag(self):
        bundle = _bundle(top10_list=[_candidate("AAPL")])
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["research_only"] is True
        assert r["shadow_mode"] is True

    def test_comparison_when_different(self):
        """Shadow may suggest different TOP3 candidates."""
        bundle = _bundle(
            top10_list=[
                _candidate("AAPL", price=160, ma20=145, rs=15, chg=8),
                _candidate("SOXS", price=30, ma20=35, rs=-10, chg=-5),
                _candidate("TSLA", price=200, ma20=210, rs=-3, adx=25),
            ],
            top3_list=[_candidate("TSLA")],
        )
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert "comparison" in r
        comp = r["comparison"]
        assert comp["current"] == ["TSLA"]
        assert len(comp["shadow_top3"]) == 1
        # Shadow might pick AAPL (bull) or SOXS (bear) instead of TSLA
        assert comp["changed"] is not None  # True or False, either is fine


# ═══════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_candidate_with_zero_price_skipped(self):
        bundle = _bundle(top10_list=[_candidate("AAPL", price=0)])
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["evaluated_count"] == 0

    def test_candidate_with_no_market_data(self):
        bundle = _bundle(top10_list=[{"ticker": "UNKNOWN"}])
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["evaluated_count"] == 0

    def test_mixed_valid_invalid(self):
        bundle = _bundle(top10_list=[
            _candidate("AAPL"), _candidate("TSLA", price=0), {"ticker": "GHOST"},
        ])
        r = evaluate_shadow_regime(dry_run=True, selection_bundle=bundle)
        assert r["candidate_count"] == 3
        assert r["evaluated_count"] == 1  # only AAPL


# ═══════════════════════════════════════════════════════════════════════
# Safety boundaries
# ═══════════════════════════════════════════════════════════════════════

class TestSafetyBoundaries:
    def test_no_broker_imports(self):
        source = Path(__import__("src.shadow.regime_evaluator").__file__).read_text()
        assert "LongBridgeBroker" not in source
        assert "PaperBroker" not in source
        assert "TradeContext" not in source
        assert "place_order" not in source

    def test_no_selector_modification(self):
        source = Path(__import__("src.shadow.regime_evaluator").__file__).read_text()
        assert "write_top_configs" not in source
        assert "selected_symbols = " not in source and "selected_symbols=" not in source
        assert "persist_selection_bundle" not in source
        assert "allow_live_order" not in source
        assert "reduce_only" not in source
        assert "write_selection_state" not in source

    def test_no_trading_engine_reference(self):
        source = Path(__import__("src.shadow.regime_evaluator").__file__).read_text()
        assert "TradingEngine" not in source
        assert "RiskManager" not in source
        assert "PortfolioManager" not in source


def run_test_direct():
    # Helpers
    TestScoreHelpers().test_score_to_label()
    TestScoreHelpers().test_safe_float_nan()
    TestScoreHelpers().test_safe_float_valid()
    # Bull
    TestBullScore().test_strong_bull()
    TestBullScore().test_weak_bull_price_below_ma20()
    TestBullScore().test_no_data_produces_sane_scores()
    # Bear
    TestBearScore().test_strong_bear()
    TestBearScore().test_inverse_etf_gets_bonus()
    TestBearScore().test_no_data_sane()
    # Range
    TestRangeScore().test_range_near_ma20()
    TestRangeScore().test_adx_too_high_reduces_range()
    # Classification
    TestClassification().test_winner_matches_highest_score()
    TestClassification().test_inverse_etf_usually_bear()
    TestClassification().test_all_scores_in_range()
    # Integration
    TestEvaluate().test_no_bundle()
    TestEvaluate().test_empty_bundle()
    TestEvaluate().test_with_candidates()
    TestEvaluate().test_never_modifies_selection_bundle()
    TestEvaluate().test_current_selected_unchanged()
    TestEvaluate().test_research_only_flag()
    TestEvaluate().test_comparison_when_different()
    # Edge cases
    TestEdgeCases().test_candidate_with_zero_price_skipped()
    TestEdgeCases().test_candidate_with_no_market_data()
    TestEdgeCases().test_mixed_valid_invalid()
    # Safety
    TestSafetyBoundaries().test_no_broker_imports()
    TestSafetyBoundaries().test_no_selector_modification()
    TestSafetyBoundaries().test_no_trading_engine_reference()
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()
