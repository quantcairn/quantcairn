from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sin

from src.backtest.comparison import compare_versions
from src.backtest.models import Bar


def _bars(rows: int = 180):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = 10.0 + sin(index / 3.0) * 0.7
        bars.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=150_000,
            )
        )
    benchmark = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = 100.0 + (index % 8) * 0.1
        benchmark.append(
            Bar(
                symbol="SMH.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=200_000,
            )
        )
    return bars, benchmark


def test_comparison_outputs_ranking_and_artifacts(tmp_path):
    bars, benchmark = _bars()
    result = compare_versions(
        bars,
        symbol="SOXS.US",
        benchmark_bars=benchmark,
        versions=["baseline", "a", "b", "c"],
        initial_cash=10_000.0,
        output_dir=tmp_path,
        scenario_name="stable_range",
    )

    assert result.comparison
    assert result.ranking
    assert result.ranking[0]["risk_adjusted_score"] >= result.ranking[-1]["risk_adjusted_score"]
    assert result.summary["risk_adjusted_ranking"]
    assert "evidence_status" in result.summary
    assert "profitability_status" in result.summary
    assert "deployment_status" in result.summary
    assert "profitability_eligible_versions" in result.summary
    assert "deployment_eligible_versions" in result.summary
    assert result.parameter_stability["unique_parameter_sets"] >= 1
    artifact_root = tmp_path / result.run_id
    assert (artifact_root / "comparison_summary.json").exists()
    assert (artifact_root / "strategy_metrics.csv").exists()
    assert (artifact_root / "strategy_ranking.csv").exists()
    assert (artifact_root / "parameter_stability.json").exists()
    assert (artifact_root / "blocked_reason_counts.csv").exists()
    assert (artifact_root / "blocked_reason_by_strategy.csv").exists()
    assert (artifact_root / "report.md").exists()


def test_comparison_is_deterministic():
    bars, benchmark = _bars()
    left = compare_versions(bars, symbol="SOXS.US", benchmark_bars=benchmark, versions=["baseline", "a", "b", "c"], initial_cash=10_000.0)
    right = compare_versions(bars, symbol="SOXS.US", benchmark_bars=benchmark, versions=["baseline", "a", "b", "c"], initial_cash=10_000.0)
    left_payload = left.to_dict()
    right_payload = right.to_dict()
    left_payload.pop("run_id", None)
    right_payload.pop("run_id", None)
    assert left_payload == right_payload
