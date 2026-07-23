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
    _extract_features,
    _time_split,
    _correlation_importance,
    _map_to_selector_features,
    _evaluate_on_test,
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
) -> dict:
    return {
        "outcome_id": f"out-{symbol}-{pnl_pct}",
        "symbol": symbol,
        "pnl_pct": str(pnl_pct),
        "realized_pnl": str(pnl_pct * 10.0),
        "formal_rank": str(rank),
        "formal_candidate_score": str(candidate_score),
        "strategy_fit_score": str(strategy_fit),
        "market_regime": regime,
        "hold_duration_seconds": str(hold_s),
        "entry_price": "100.0",
        "exit_price": str(100.0 + pnl_pct),
        "quantity": "1",
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
    }


# ═══════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════

class TestDataLoading:
    def test_empty_csv_returns_empty(self, tmp_path: Path):
        csv_path = tmp_path / "nonexistent.csv"
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            assert _load_outcomes() == []

    def test_filters_training_ineligible(self, tmp_path: Path):
        rows = [_outcome(eligible=True), _outcome(symbol="TSLA", eligible=False)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            outcomes = _load_outcomes()
            assert len(outcomes) == 1
            assert outcomes[0]["symbol"] == "AAPL"


# ═══════════════════════════════════════════════════════════════════════
# Feature extraction
# ═══════════════════════════════════════════════════════════════════════

class TestFeatureExtraction:
    def test_rank_inverse(self):
        outcomes = [_outcome(rank=1), _outcome(symbol="TSLA", rank=4)]
        features, targets = _extract_features(outcomes)
        assert features[0]["formal_rank_inverse"] == 1.0
        assert features[1]["formal_rank_inverse"] == 0.25

    def test_regime_one_hot(self):
        outcomes = [
            _outcome(symbol="A", regime="NORMAL"),
            _outcome(symbol="B", regime="BEAR"),
            _outcome(symbol="C", regime="BULL"),
            _outcome(symbol="D", regime="RANGE"),
            _outcome(symbol="E", regime="UNKNOWN"),
        ]
        features, _ = _extract_features(outcomes)
        assert features[0]["regime_NORMAL"] == 1.0
        assert features[1]["regime_BEAR"] == 1.0
        assert features[2]["regime_BULL"] == 1.0
        assert features[3]["regime_RANGE"] == 1.0
        # Unknown → all 0
        assert features[4]["regime_NORMAL"] == 0.0
        assert features[4]["regime_BEAR"] == 0.0

    def test_target_is_pnl_pct(self):
        outcomes = [_outcome(pnl_pct=7.5), _outcome(symbol="TSLA", pnl_pct=-3.2)]
        _, targets = _extract_features(outcomes)
        assert targets == [7.5, -3.2]


# ═══════════════════════════════════════════════════════════════════════
# Time split
# ═══════════════════════════════════════════════════════════════════════

class TestTimeSplit:
    def test_70_30_split(self):
        outcomes = [_outcome(symbol=f"S{i}") for i in range(100)]
        features, targets = _extract_features(outcomes)
        train_f, train_t, test_f, test_t = _time_split(features, targets, outcomes)
        assert len(train_f) == 70
        assert len(test_f) == 30

    def test_small_dataset_split(self):
        outcomes = [_outcome(symbol=f"S{i}") for i in range(10)]
        features, targets = _extract_features(outcomes)
        train_f, train_t, test_f, test_t = _time_split(features, targets, outcomes)
        assert len(train_f) == 7
        assert len(test_f) == 3


# ═══════════════════════════════════════════════════════════════════════
# Correlation importance
# ═══════════════════════════════════════════════════════════════════════

class TestCorrelationImportance:
    def test_produces_non_negative_weights(self):
        features = [
            {"a": 1.0, "b": 0.0},
            {"a": 0.5, "b": 0.5},
            {"a": 0.0, "b": 1.0},
        ]
        targets = [1.0, 0.5, 0.0]
        imp = _correlation_importance(features, targets)
        assert all(v >= 0.0 for v in imp.values())

    def test_small_sample(self):
        features = [{"a": 1.0}, {"a": 0.5}]
        targets = [1.0, 0.5]
        imp = _correlation_importance(features, targets)
        assert len(imp) > 0


# ═══════════════════════════════════════════════════════════════════════
# Weight mapping
# ═══════════════════════════════════════════════════════════════════════

class TestWeightMapping:
    def test_maps_to_six_dimensions(self):
        importance = {
            "formal_candidate_score": 0.4,
            "strategy_fit_score": 0.3,
            "formal_rank_inverse": 0.15,
            "hold_duration_hours": 0.1,
            "regime_NORMAL": 0.03,
            "regime_BULL": 0.02,
        }
        weights = _map_to_selector_features(importance)
        assert len(weights) == 6
        assert all(n in weights for n in FEATURE_NAMES)
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.01

    def test_blended_with_baseline(self):
        """Mapping always blends 50/50 with baseline."""
        importance = {"formal_candidate_score": 1.0}
        weights = _map_to_selector_features(importance)
        for dim in FEATURE_NAMES:
            assert abs(weights[dim] - BASELINE_WEIGHTS[dim]) < 0.5


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
                    assert report["train_set_size"] == 17  # 70% of 25
                    assert report["test_set_size"] == 8

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
                    assert "baseline" in ev
                    assert "proposed" in ev
                    assert "r2" in ev["baseline"]
                    assert "hit_rate" in ev["baseline"]

    def test_feature_importances_sorted(self, tmp_path: Path):
        rows = [_outcome(symbol=f"S{i}", pnl_pct=float((i % 7) - 3)) for i in range(30)]
        csv_path = tmp_path / "outcomes.csv"
        _write_outcome_csv(csv_path, rows)
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                with patch.object(module, "WEIGHTS_PATH", tmp_path / "weights.json"):
                    report = run_weight_advisor(dry_run=True)
                    imports = list(report.get("feature_importances", {}).values())
                    # Should be sorted descending
                    for i in range(1, len(imports)):
                        assert imports[i] <= imports[i - 1] + 0.0001

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


if __name__ == "__main__":
    run_test_direct()
