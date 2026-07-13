from __future__ import annotations

import json
from pathlib import Path

from src.candidate_validation import CandidateValidationStore, ValidationStatus
from src.candidate_validation.performance_tracker import CandidatePerformanceTracker


def _make_candidate(symbol: str, score: float, *, asset_type: str, strategy_family: str, benchmarks: tuple[str, ...], risk_profile: str = "balanced"):
    from src.candidate_validation import CandidateRecord

    return CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=score,
        candidate_score=score,
        liquidity_score=score,
        trend_score=score,
        volatility_score=score,
        risk_score=score,
        strategy_fit_score=score,
        recommended_strategy=strategy_family,
        score_reason="unit_test",
        ai_reason="unit_test",
        asset_type=asset_type,
        benchmarks=benchmarks,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe="15m",
        market="US",
        metadata={"source": "unit_test"},
    )


def test_candidate_performance_tracker_saves_scores_and_buckets(tmp_path):
    root = tmp_path / "candidates"
    store = CandidateValidationStore(root)
    tracker = CandidatePerformanceTracker(root)

    c1 = _make_candidate("AAPL.US", 95.0, asset_type="common_stock", strategy_family="trend_following", benchmarks=("QQQ.US", "SPY.US"))
    c2 = _make_candidate("SOXS.US", 85.0, asset_type="inverse_etf", strategy_family="inverse_range", benchmarks=("SOXX.US", "SMH.US"), risk_profile="strict")
    c3 = _make_candidate("XYZ.US", 65.0, asset_type="common_stock", strategy_family="mean_reversion", benchmarks=("QQQ.US", "SPY.US"))

    store.save_candidates([c1, c2, c3])
    store.transition(c1.candidate_id, ValidationStatus.CLASSIFIED, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.BENCHMARK_ASSIGNED, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.STRATEGY_ASSIGNED, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.PENDING_DATA_VALIDATION, metadata={})
    store.transition(
        c1.candidate_id,
        ValidationStatus.DATA_VALID,
        metadata={"benchmark_valid": True, "eligible_for_backtest": True, "future_data_risk": False, "reconciliation_status": "OK"},
    )
    store.transition(c1.candidate_id, ValidationStatus.PENDING_BACKTEST, metadata={"data_status": ValidationStatus.DATA_VALID.value})
    store.transition(c1.candidate_id, ValidationStatus.BACKTEST_COMPLETE, metadata={"backtest_complete": True})
    store.transition(c1.candidate_id, ValidationStatus.PENDING_WALK_FORWARD, metadata={"backtest_status": ValidationStatus.BACKTEST_COMPLETE.value})
    store.transition(c1.candidate_id, ValidationStatus.WALK_FORWARD_COMPLETE, metadata={"walk_forward_complete": True})

    store.transition(c2.candidate_id, ValidationStatus.CLASSIFIED, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.BENCHMARK_ASSIGNED, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.STRATEGY_ASSIGNED, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.PENDING_DATA_VALIDATION, metadata={})
    store.transition(
        c2.candidate_id,
        ValidationStatus.DATA_VALID,
        metadata={"benchmark_valid": True, "eligible_for_backtest": True, "future_data_risk": False, "reconciliation_status": "OK"},
    )

    analysis = tracker.analyze(store.load_latest_candidates())
    assert analysis["candidate_count"] == 3
    assert analysis["average_score"] == 81.67
    assert analysis["high_score_candidate_count"] == 2
    assert analysis["high_score_success_rate"] == 50.0
    bucket_map = {row["score_bucket"]: row for row in analysis["score_bucket_distribution"]}
    assert bucket_map["90-100"]["candidate_count"] == 1
    assert bucket_map["90-100"]["data_valid_rate"] == 100.0
    assert bucket_map["90-100"]["backtest_complete_rate"] == 100.0
    assert bucket_map["80-90"]["candidate_count"] == 1
    assert bucket_map["80-90"]["data_valid_rate"] == 100.0
    assert bucket_map["80-90"]["backtest_complete_rate"] == 0.0
    assert bucket_map["70-80"]["candidate_count"] == 0
    assert bucket_map["60-70"]["candidate_count"] == 1
    assert bucket_map["60-70"]["data_valid_rate"] == 0.0

    assert tracker.performance_path.exists()
    assert tracker.summary_path.exists()
    assert len(tracker.load_records()) == 3
    with tracker.performance_path.open("r", encoding="utf-8") as handle:
        first_row = json.loads(handle.readline())
    assert first_row["factor_scores"]["candidate_score"] is not None
    assert first_row["recommended_strategy"] in {"trend_following", "inverse_range", "mean_reversion"}


def test_candidate_performance_tracker_snapshot_is_dashboard_ready(tmp_path):
    root = tmp_path / "candidates"
    store = CandidateValidationStore(root)
    tracker = CandidatePerformanceTracker(root)
    candidate = _make_candidate("AAPL.US", 91.0, asset_type="common_stock", strategy_family="trend_following", benchmarks=("QQQ.US", "SPY.US"))
    store.save_candidates([candidate])

    payload = tracker.analyze(store.load_latest_candidates())
    assert payload["title"] == "Candidate Ranking Performance"
    assert payload["state"] == "SAFE"
    assert payload["status_label"] == "SAFE"
    assert payload["high_score_threshold"] == 80.0
    assert payload["score_bucket_distribution"][0]["score_bucket"] == "90-100"
