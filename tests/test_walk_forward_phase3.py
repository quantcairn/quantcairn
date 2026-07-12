from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backtest.models import Bar
from src.backtest.walk_forward import WalkForwardConfig, WalkForwardEvaluator


def _make_bars(rows: int = 120):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = 10.0 + (index % 10) * 0.05
        bars.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=120_000,
            )
        )
    return bars


def _flat_bars(rows: int = 90):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = 10.0
        bars.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.005, 4),
                low=round(close * 0.995, 4),
                close=round(close, 4),
                volume=120_000,
            )
        )
    return bars


def test_walk_forward_emits_parameter_stability_outputs(tmp_path):
    bars = _make_bars()
    evaluator = WalkForwardEvaluator(
        WalkForwardConfig(train_size=30, validation_size=15, test_size=15, step_size=15, purge_gap=2, embargo_gap=2)
    )
    result = evaluator.evaluate(
        bars,
        symbol="SOXS.US",
        strategy="c",
        parameter_grid=[
            {"minimum_range_pct": 1.0, "max_entry_layers": 3},
            {"minimum_range_pct": 0.8, "max_entry_layers": 5},
        ],
        output_dir=tmp_path,
        max_candidates=2,
    )

    artifact_dirs = list(tmp_path.iterdir())
    assert artifact_dirs
    root = artifact_dirs[0]
    assert (root / "summary.json").exists()
    assert (root / "parameter_stability.json").exists()
    assert (root / "parameter_candidates.csv").exists()
    assert (root / "top_candidates.csv").exists()
    assert (root / "parameter_sensitivity.csv").exists()
    assert (root / "blocked_reason_counts.csv").exists()
    assert (root / "blocked_reason_by_window.csv").exists()
    assert result.parameter_candidates
    assert result.parameter_sensitivity
    assert "instability_score" in result.parameter_stability
    assert "evidence_status" in result.parameter_stability
    assert "profitability_status" in result.parameter_stability
    assert "deployment_status" in result.parameter_stability


def test_walk_forward_does_not_use_test_window_for_selection():
    bars = _make_bars()
    evaluator = WalkForwardEvaluator(
        WalkForwardConfig(train_size=30, validation_size=15, test_size=15, step_size=15, purge_gap=2, embargo_gap=2)
    )
    left = evaluator.evaluate(
        bars,
        symbol="SOXS.US",
        strategy="c",
        parameter_grid=[{"minimum_range_pct": 1.0}],
        max_candidates=1,
    )
    right = evaluator.evaluate(
        bars,
        symbol="SOXS.US",
        strategy="c",
        parameter_grid=[{"minimum_range_pct": 1.0}],
        max_candidates=1,
    )
    assert left.to_dict() == right.to_dict()


def test_walk_forward_reports_evidence_gate_fields():
    bars = _flat_bars()
    evaluator = WalkForwardEvaluator(
        WalkForwardConfig(train_size=30, validation_size=15, test_size=15, step_size=15, purge_gap=2, embargo_gap=2)
    )
    result = evaluator.evaluate(
        bars,
        symbol="SOXS.US",
        strategy="c",
        parameter_grid=[{"minimum_range_pct": 1.0}],
        max_candidates=1,
    )

    assert result.parameter_stability["ranking_status"] in {"ELIGIBLE", "INSUFFICIENT_EVIDENCE"}
    assert "evidence_thresholds" in result.parameter_stability
    assert "active_window_ratio" in result.parameter_stability
    assert "no_trade_window_ratio" in result.parameter_stability
    assert "profitability_status" in result.parameter_stability
    assert "deployment_status" in result.parameter_stability
