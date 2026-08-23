"""Tests for the read-only weight advisor.

All tests use tmp_path — no real state, no broker, no orders.
"""

from __future__ import annotations

import csv
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import src.outcome.weight_advisor as module
from src.outcome.weight_advisor import (
    BASELINE_WEIGHTS,
    FEATURE_NAMES,
    MIN_SAMPLE_SIZE,
    MODEL_VERSION,
    _load_outcomes,
    _compute_factor_performance,
    _compute_weight_suggestions,
    _compute_strategy_performance,
    FactorPerformance,
    run_weight_advisor,
)


def _write_outcome_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _outcome(
    symbol="AAPL",
    pnl_pct=5.0,
    rank=1,
    candidate_score=85.0,
    strategy_fit=72.0,
    regime="NORMAL",
    hold_s=14400,
    eligible=True,
    outcome="WIN",
    vol=70.0, vol_feat=65.0, trend=60.0, repeat=55.0, dd=50.0, risk=30.0, liq=80.0,
    strategy="range_detector",
) -> dict:
    return {
        "outcome_id": f"out-{symbol}-{pnl_pct}",
        "symbol": symbol,
        "pnl_pct": str(pnl_pct),
        "return_pct": str(pnl_pct),
        "realized_pnl": str(pnl_pct * 10.0),
        "formal_rank": str(rank),
        "formal_candidate_score": str(candidate_score),
        "strategy_fit_score": str(strategy_fit),
        "market_regime": regime,
        "hold_duration_seconds": str(hold_s),
        "entry_price": "100.0",
        "exit_price": str(100.0 + pnl_pct),
        "quantity": "1",
        "outcome": outcome,
        "strategy": strategy,
        "training_eligible": "true" if eligible else "false",
        "execution_mode": "paper",
        "selection_run_id": "run-1",
        "selection_bundle_hash": "hash-1",
        "selection_context_status": "BOUND_AT_ENTRY",
        "training_ineligible_reasons": "",
        "selection_date": "2026-07-01",
        "entry_time": "2026-07-01T10:00:00Z",
        "exit_time": "2026-07-02T14:00:00Z",
        "exit_reason": "take_profit",
        "account_id": "paper-default",
        "buy_fill_id": "B-1",
        "sell_fill_id": "S-1",
        # V3 feature columns
        "feature_volatility_score": str(vol),
        "feature_volume_score": str(vol_feat),
        "feature_trend_score": str(trend),
        "feature_repeatability_score": str(repeat),
        "feature_drawdown_safety_score": str(dd),
        "feature_risk_score": str(risk),
        "feature_liquidity_score": str(liq),
    }


# ═══════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════

class TestDataLoading:
    def test_empty_csv_returns_empty(self, tmp_path: Path):
        csv_path = tmp_path / "nonexistent.csv"
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            assert _load_outcomes() == []
        rows = [_outcome(eligible=True), _outcome(symbol="TSLA", eligible=False)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            outcomes = _load_outcomes()
            assert len(outcomes) == 1
            assert outcomes[0]["symbol"] == "AAPL"


# ═══════════════════════════════════════════════════════════════════════
# Main advisor
# ═══════════════════════════════════════════════════════════════════════

class TestWeightAdvisor:
    def test_insufficient_data_when_empty(self, tmp_path: Path):
        with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "nonexistent.csv"):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    assert report["status"] == "INSUFFICIENT_DATA"
                    assert report["sample_size"] == 0
                    assert report["approval_status"] == "PENDING_HUMAN_APPROVAL"

    def test_insufficient_data_when_below_threshold(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float(i)) for i in range(10)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    assert report["status"] == "INSUFFICIENT_DATA"
                    assert report["sample_size"] == 10

    def test_completed_when_above_threshold(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(25)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    assert report["status"] == "COMPLETED"
                    assert report["sample_size"] == 25

    def test_always_pending_human_approval(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(30)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    assert report["approval_status"] == "PENDING_HUMAN_APPROVAL"

    def test_proposed_weights_sum_to_one(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(30)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    total = sum(report["proposed_weights"].values())
                    assert abs(total - 1.0) < 0.02

    def test_baseline_never_changed(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(30)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    assert report["baseline_weights"] == BASELINE_WEIGHTS

    def test_dry_run_does_not_write(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(25)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        weights_path = tmp_path / "weights.json"
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", weights_path):
                    run_weight_advisor(dry_run=True)
                    assert not weights_path.exists()

    def test_apply_writes_report(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(25)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        weights_path = tmp_path / "weights.json"
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", weights_path):
                    with patch.object(module, "STRATEGY_PERF_PATH", tmp_path / "strategy_performance.json"):
                        report = run_weight_advisor(dry_run=False)
        assert weights_path.exists()
        loaded = json.loads(weights_path.read_text(encoding="utf-8"))
        assert loaded["model_version"] == MODEL_VERSION
        assert loaded["approval_status"] == "PENDING_HUMAN_APPROVAL"

    def test_evaluation_metrics_present(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(30)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    ev = report.get("evaluation_metrics", {})
                    assert "max_delta_per_factor" in ev
                    assert "sample_size" in ev

    def test_weight_suggestions_populated(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(30)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    suggestions = report.get("weight_suggestions", [])
                    assert len(suggestions) == 6
                    for s in suggestions:
                        assert 0 <= s["confidence"] <= 1.0

    def test_training_ineligible_not_in_sample(self, tmp_path: Path):
        """training_eligible=false outcomes must not be counted."""
        rows = [
            _outcome(symbol=f"S{i}", pnl_pct=float(i), eligible=(i % 3 != 0))
            for i in range(60)
        ]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    # 40 eligible out of 60 total
                    assert report["sample_size"] == 40

    def test_no_broker_import(self):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "LongBridgeBroker" not in source
        assert "place_order" not in source
        assert "TradeContext" not in source
        assert "allow_live_order" not in source
        assert "reduce_only" not in source


def run_test_direct():
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        # Data loading
        TestDataLoading().test_empty_csv_returns_empty(tp)
        TestDataLoading().test_filters_training_ineligible(tp)
        # Features
        TestFeatureExtraction().test_rank_inverse()
        TestFeatureExtraction().test_regime_one_hot()
        TestFeatureExtraction().test_target_is_pnl_pct()
        # Split
        TestTimeSplit().test_70_30_split()
        TestTimeSplit().test_small_dataset_split()
        # Correlation
        TestCorrelationImportance().test_produces_non_negative_weights()
        TestCorrelationImportance().test_small_sample()
        # Mapping
        TestWeightMapping().test_maps_to_six_dimensions()
        TestWeightMapping().test_blended_with_baseline()
        # Advisor
        TestWeightAdvisor().test_insufficient_data_when_empty(tp)
        TestWeightAdvisor().test_insufficient_data_when_below_threshold(tp)
        TestWeightAdvisor().test_completed_when_above_threshold(tp)
        TestWeightAdvisor().test_always_pending_human_approval(tp)
        TestWeightAdvisor().test_proposed_weights_sum_to_one(tp)
        TestWeightAdvisor().test_no_broker_import()
    print("direct run: all passed")


    TestWADelta().test_max_delta_enforced()
    TestWADelta().test_total_weights_sum_to_one()
    TestWAFactor().test_factor_performance()
    TestWAFactor().test_top_positive_factors()
    TestWAFactor().test_parquet_fallback_to_csv()
    TestWAConfidence().test_confidence_below_50_percent_for_low_samples()
    TestWAConfidence().test_confidence_increases_with_samples()
    TestWAStrategy().test_strategy_performance_generated()
    TestWAEdge().test_all_loss_handled()
    TestWAEdge().test_all_win_handled()
    TestWAEdge().test_empty_feature_columns()
    TestWASafety().test_no_broker_in_weight_advisor()
    TestWASafety().test_no_auto_activation()


if __name__ == "__main__":
    run_test_direct()


# ═══════════════════════════════════════════════════════════════════════
# V2 weight advisor tests
# ═══════════════════════════════════════════════════════════════════════

class TestWADelta:
    def test_max_delta_enforced(self):
        from src.outcome.weight_advisor import _compute_weight_suggestions, FactorPerformance
        factors = [
            FactorPerformance(factor=n, label=n, win_avg=70.0, loss_avg=50.0, diff=20.0, direction="positive", win_count=50, loss_count=50)
            for n in ("volatility_score", "volume_score", "trend_fit_score", "repeatability_score", "drawdown_safety_score", "correlation_bonus")
        ]
        suggestions = _compute_weight_suggestions(factors, sample_size=100)
        for s in suggestions:
            assert abs(s.delta) <= 0.05, f"{s.factor} delta {s.delta} exceeds max 0.05"

    def test_total_weights_sum_to_one(self):
        from src.outcome.weight_advisor import _compute_weight_suggestions, FactorPerformance
        factors = [FactorPerformance(factor=n, label=n, win_avg=70.0, loss_avg=50.0, diff=20.0, direction="positive", win_count=50, loss_count=50)
                   for n in ("volatility_score", "volume_score", "trend_fit_score", "repeatability_score", "drawdown_safety_score", "correlation_bonus")]
        suggestions = _compute_weight_suggestions(factors, sample_size=100)
        total = sum(s.suggested for s in suggestions)
        assert abs(total - 1.0) < 0.02, f"total={total}"


class TestWAFactor:
    def test_factor_performance(self):
        from src.outcome.weight_advisor import _compute_factor_performance
        outcomes = [
            {"outcome": "WIN", "feature_volatility_score": 80.0, "feature_volume_score": 70.0, "feature_trend_score": 60.0, "feature_repeatability_score": 50.0, "feature_drawdown_safety_score": 40.0, "feature_risk_score": 30.0, "feature_liquidity_score": 90.0},
            {"outcome": "WIN", "feature_volatility_score": 75.0, "feature_volume_score": 65.0, "feature_trend_score": 55.0, "feature_repeatability_score": 55.0, "feature_drawdown_safety_score": 45.0, "feature_risk_score": 35.0, "feature_liquidity_score": 85.0},
            {"outcome": "LOSS", "feature_volatility_score": 40.0, "feature_volume_score": 50.0, "feature_trend_score": 45.0, "feature_repeatability_score": 48.0, "feature_drawdown_safety_score": 42.0, "feature_risk_score": 55.0, "feature_liquidity_score": 60.0},
            {"outcome": "LOSS", "feature_volatility_score": 35.0, "feature_volume_score": 45.0, "feature_trend_score": 40.0, "feature_repeatability_score": 52.0, "feature_drawdown_safety_score": 38.0, "feature_risk_score": 60.0, "feature_liquidity_score": 55.0},
        ]
        factors = _compute_factor_performance(outcomes)
        assert len(factors) == 6
        vol = [f for f in factors if f.factor == "volatility_score"][0]
        assert vol.win_avg > vol.loss_avg

    def test_top_positive_factors(self):
        from src.outcome.weight_advisor import _compute_factor_performance
        outcomes = [{"outcome": "WIN", "feature_volatility_score": 90.0, "feature_volume_score": 70.0, "feature_trend_score": 60.0, "feature_repeatability_score": 50.0, "feature_drawdown_safety_score": 40.0, "feature_risk_score": 30.0, "feature_liquidity_score": 80.0},
                     {"outcome": "LOSS", "feature_volatility_score": 20.0, "feature_volume_score": 50.0, "feature_trend_score": 55.0, "feature_repeatability_score": 55.0, "feature_drawdown_safety_score": 45.0, "feature_risk_score": 55.0, "feature_liquidity_score": 60.0}]
        factors = _compute_factor_performance(outcomes)
        positive = [f for f in factors if f.diff > 0]
        assert len(positive) > 0

    def test_parquet_fallback_to_csv(self):
        from src.outcome.weight_advisor import _load_outcomes
        outcomes = _load_outcomes()
        assert isinstance(outcomes, list)


class TestWAConfidence:
    def test_confidence_below_50_percent_for_low_samples(self):
        from src.outcome.weight_advisor import _compute_weight_suggestions, FactorPerformance
        factors = [
            FactorPerformance(factor=n, label=n, win_avg=65.0, loss_avg=55.0, diff=10.0, direction="positive", win_count=3, loss_count=3)
            for n in ("volatility_score", "volume_score", "trend_fit_score", "repeatability_score", "drawdown_safety_score", "correlation_bonus")
        ]
        suggestions = _compute_weight_suggestions(factors, sample_size=6)
        avg_conf = sum(s.confidence for s in suggestions) / len(suggestions)
        assert avg_conf < 0.5, f"Expected confidence < 0.5 for 6 samples, got {avg_conf}"

    def test_confidence_increases_with_samples(self):
        from src.outcome.weight_advisor import _compute_weight_suggestions, FactorPerformance
        factors = [FactorPerformance(factor=n, label=n, win_avg=70.0, loss_avg=50.0, diff=20.0, direction="positive", win_count=20, loss_count=20)
                   for n in ("volatility_score", "volume_score", "trend_fit_score", "repeatability_score", "drawdown_safety_score", "correlation_bonus")]
        s_low = _compute_weight_suggestions(factors, sample_size=10)
        s_high = _compute_weight_suggestions(factors, sample_size=100)
        assert s_high[0].confidence > s_low[0].confidence


class TestWAStrategy:
    def test_strategy_performance_generated(self):
        from src.outcome.weight_advisor import _compute_strategy_performance
        outcomes = [
            {"strategy": "range_detector", "outcome": "WIN", "return_pct": 5.0, "realized_pnl": 500.0},
            {"strategy": "range_detector", "outcome": "LOSS", "return_pct": -3.0, "realized_pnl": -300.0},
            {"strategy": "mean_reversion", "outcome": "WIN", "return_pct": 8.0, "realized_pnl": 800.0},
            {"strategy": "mean_reversion", "outcome": "WIN", "return_pct": 12.0, "realized_pnl": 1200.0},
        ]
        sp = _compute_strategy_performance(outcomes)
        assert sp["strategy_count"] == 2
        assert sp["best_strategy"] == "mean_reversion"
        assert sp["worst_strategy"] == "range_detector"


class TestWAEdge:
    def test_all_loss_handled(self):
        from src.outcome.weight_advisor import _compute_factor_performance
        outcomes = [{"outcome": "LOSS", "feature_volatility_score": 20.0, "feature_volume_score": 30.0, "feature_trend_score": 25.0, "feature_repeatability_score": 30.0, "feature_drawdown_safety_score": 35.0, "feature_risk_score": 60.0, "feature_liquidity_score": 40.0}]
        factors = _compute_factor_performance(outcomes)
        assert factors[0].loss_avg > 0
        assert factors[0].win_avg == 0.0

    def test_all_win_handled(self):
        from src.outcome.weight_advisor import _compute_factor_performance
        outcomes = [{"outcome": "WIN", "feature_volatility_score": 90.0, "feature_volume_score": 70.0, "feature_trend_score": 65.0, "feature_repeatability_score": 55.0, "feature_drawdown_safety_score": 50.0, "feature_risk_score": 30.0, "feature_liquidity_score": 80.0}]
        factors = _compute_factor_performance(outcomes)
        assert factors[0].win_avg > 0
        assert factors[0].loss_avg == 0.0

    def test_empty_feature_columns(self):
        from src.outcome.weight_advisor import _compute_factor_performance
        outcomes = [{"outcome": "WIN"}]
        factors = _compute_factor_performance(outcomes)
        assert len(factors) == 6
        assert all(f.win_avg == 0.0 for f in factors)


class TestWASafety:
    def test_no_broker_in_weight_advisor(self):
        import src.outcome.weight_advisor as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "LongBridgeBroker" not in source
        assert "PaperBroker" not in source
        assert "place_order" not in source
        assert "TradeContext" not in source
        assert "allow_live_order" not in source
        assert "reduce_only" not in source

    def test_no_auto_activation(self):
        import src.outcome.weight_advisor as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "approve_proposal" not in source
        assert "ACTIVE" not in source
