from __future__ import annotations

import json
from pathlib import Path

from src.candidate_validation import CandidateValidationStore, ValidationStatus
from src.candidate_validation import research_report as research_report_module
from src.candidate_validation.research_report import CandidateDailyResearchReportGenerator
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


def test_daily_research_report_writes_markdown_and_json(tmp_path):
    candidate_root = tmp_path / "candidates"
    research_root = tmp_path / "research" / "daily"
    store = CandidateValidationStore(candidate_root)

    c1 = _make_candidate("AAPL.US", 95.0, asset_type="common_stock", strategy_family="trend_following", benchmarks=("QQQ.US", "SPY.US"))
    c2 = _make_candidate("SOXS.US", 85.0, asset_type="inverse_etf", strategy_family="inverse_range", benchmarks=("SOXX.US", "SMH.US"), risk_profile="strict")
    c3 = _make_candidate("XYZ.US", 65.0, asset_type="common_stock", strategy_family="mean_reversion", benchmarks=("QQQ.US", "SPY.US"))
    store.save_candidates([c1, c2, c3])

    store.transition(c1.candidate_id, ValidationStatus.CLASSIFIED, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.BENCHMARK_ASSIGNED, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.STRATEGY_ASSIGNED, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.PENDING_DATA_VALIDATION, metadata={})
    store.transition(c1.candidate_id, ValidationStatus.DATA_VALID, metadata={"benchmark_valid": True, "eligible_for_backtest": True, "future_data_risk": False, "reconciliation_status": "OK"})
    store.transition(c1.candidate_id, ValidationStatus.PENDING_BACKTEST, metadata={"data_status": ValidationStatus.DATA_VALID.value})
    store.transition(c1.candidate_id, ValidationStatus.BACKTEST_COMPLETE, metadata={"backtest_complete": True})
    store.transition(c1.candidate_id, ValidationStatus.PENDING_WALK_FORWARD, metadata={"backtest_status": ValidationStatus.BACKTEST_COMPLETE.value})
    store.transition(c1.candidate_id, ValidationStatus.WALK_FORWARD_COMPLETE, metadata={"walk_forward_complete": True})

    store.transition(c2.candidate_id, ValidationStatus.CLASSIFIED, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.BENCHMARK_ASSIGNED, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.STRATEGY_ASSIGNED, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.PENDING_DATA_VALIDATION, metadata={})
    store.transition(c2.candidate_id, ValidationStatus.DATA_VALID, metadata={"benchmark_valid": True, "eligible_for_backtest": True, "future_data_risk": False, "reconciliation_status": "OK"})
    store.transition(c2.candidate_id, ValidationStatus.PENDING_BACKTEST, metadata={"data_status": ValidationStatus.DATA_VALID.value})
    store.transition(c2.candidate_id, ValidationStatus.BACKTEST_COMPLETE, metadata={"backtest_complete": True})

    # leave c3 as AI_CANDIDATE to keep the failure analysis meaningful
    tracker = CandidatePerformanceTracker(candidate_root)
    tracker.sync(store.load_latest_candidates())

    generator = CandidateDailyResearchReportGenerator(root_dir=research_root, candidate_root=candidate_root)
    report = generator.write()

    json_path = research_root / "daily_candidate_report.json"
    md_path = research_root / "daily_candidate_report.md"
    assert json_path.exists()
    assert md_path.exists()
    assert report["title"] == "AI Candidate Daily Research Report"
    assert report["candidate_count"] == 3
    assert report["average_score"] == 81.67
    assert report["top_candidates"][0]["symbol"] == "AAPL.US"
    assert report["top_candidates"][0]["candidate_score"] == 95.0
    assert report["performance"]["title"] == "Candidate Ranking Performance"
    assert report["performance"]["high_score_candidate_count"] == 2
    assert report["failure_analysis"]["statuses"]["BACKTEST_FAILED"] == 0
    assert report["failure_analysis"]["statuses"]["WALK_FORWARD_FAILED"] == 0

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["title"] == "AI Candidate Daily Research Report"
    assert payload["score_distribution"][0]["score_bucket"] == "90-100"

    markdown = md_path.read_text(encoding="utf-8")
    assert "# AI Candidate Daily Research Report" in markdown
    assert "Top Candidates" in markdown
    assert "Score Distribution" in markdown
    assert "Failure Analysis" in markdown
    assert "AAPL.US" in markdown


def test_daily_research_report_writes_dashboard_snapshot(tmp_path, monkeypatch):
    candidate_root = tmp_path / "candidates"
    research_root = tmp_path / "research" / "daily"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(
        research_report_module,
        "_load_ai_selection_report",
        lambda project_dir=None: {
            "selection_run_id": "research-run-123",
            "selection_date": "2026-08-02",
            "selection_stage": "FINALIZED",
            "execution_status": "COMPLETED",
            "result_quality": "COMPLETE",
            "research_admission": "RESEARCH_READY",
        },
    )
    store = CandidateValidationStore(candidate_root)
    store.save_candidates([
        _make_candidate(
            "NVDA.US",
            91.0,
            asset_type="common_stock",
            strategy_family="mean_reversion",
            benchmarks=("QQQ.US", "SPY.US"),
        )
    ])

    report = CandidateDailyResearchReportGenerator(root_dir=research_root, candidate_root=candidate_root).write()

    snapshot_path = state_dir / "dashboard_snapshots" / "candidate_research_report.json"
    assert snapshot_path.exists()
    envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert envelope["source_run_id"] == "research-run-123"
    assert envelope["generated_at"] == report["generated_at"]
    assert envelope["data"]["snapshot_name"] == "candidate_research_report"
    assert envelope["data"]["source"] == "candidate_daily_research_report"
    assert envelope["data"]["run_id"] == "research-run-123"
    assert envelope["data"]["candidate_count"] == 1
    assert envelope["data"]["top_candidates"][0]["symbol"] == "NVDA.US"


def test_daily_research_report_snapshot_write_failure_does_not_fail_report(tmp_path, monkeypatch):
    candidate_root = tmp_path / "candidates"
    research_root = tmp_path / "research" / "daily"
    store = CandidateValidationStore(candidate_root)
    store.save_candidates([
        _make_candidate(
            "MSFT.US",
            89.0,
            asset_type="common_stock",
            strategy_family="mean_reversion",
            benchmarks=("QQQ.US", "SPY.US"),
        )
    ])
    monkeypatch.setattr(
        research_report_module,
        "write_dashboard_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("snapshot unavailable")),
    )

    report = CandidateDailyResearchReportGenerator(root_dir=research_root, candidate_root=candidate_root).write()

    assert report["candidate_count"] == 1
    assert (research_root / "daily_candidate_report.json").exists()
    assert (research_root / "daily_candidate_report.md").exists()


def test_daily_research_report_handles_empty_store(tmp_path):
    generator = CandidateDailyResearchReportGenerator(root_dir=tmp_path / "research" / "daily", candidate_root=tmp_path / "candidates")
    report = generator.build()

    assert report["candidate_count"] == 0
    assert report["score_distribution"] == []
    assert report["top_candidates"] == []
