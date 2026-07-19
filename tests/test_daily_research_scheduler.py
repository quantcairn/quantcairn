from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.candidate_validation import CandidateRecord, CandidateValidationStore
from src.candidate_validation.research_scheduler import DailyResearchScheduler, latest_research_status, market_calendar_check
from src.dashboard import combined as dashboard


def _make_candidate(symbol: str, score: float, *, asset_type: str, strategy_family: str, benchmarks: tuple[str, ...], risk_profile: str = "balanced"):
    return CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-13T08:50:00-04:00",
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


def _seed_candidates(root: Path) -> None:
    store = CandidateValidationStore(root)
    store.save_candidates(
        [
            _make_candidate("AAPL.US", 92.0, asset_type="common_stock", strategy_family="trend_following", benchmarks=("QQQ.US", "SPY.US")),
            _make_candidate("SOXS.US", 88.0, asset_type="inverse_etf", strategy_family="inverse_range", benchmarks=("SOXX.US", "SMH.US"), risk_profile="strict"),
        ]
    )


def test_market_calendar_check_skips_weekend_and_holiday():
    weekend = market_calendar_check(date(2026, 7, 11))
    assert weekend["should_run"] is False
    assert weekend["reason"] == "weekend"
    assert weekend["is_weekend"] is True
    assert weekend["is_trading_day"] is False

    holiday = market_calendar_check(date(2026, 7, 3))
    assert holiday["should_run"] is False
    assert holiday["reason"] == "market_holiday"
    assert holiday["is_market_holiday"] is True
    assert holiday["is_trading_day"] is False

    monday_premarket = market_calendar_check(date(2026, 7, 13), now_et=datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York")))
    assert monday_premarket["should_run"] is True
    assert monday_premarket["current_session"] == "2026-07-13"
    assert monday_premarket["previous_completed_session"] == "2026-07-10"
    assert monday_premarket["is_premarket"] is True


def test_daily_research_scheduler_dry_run_does_not_write_files(tmp_path):
    project_dir = tmp_path / "project"
    candidate_root = project_dir / "artifacts" / "candidates"
    research_root = project_dir / "artifacts" / "research" / "daily"
    candidate_root.mkdir(parents=True, exist_ok=True)
    _seed_candidates(candidate_root)

    called = {"selector": 0}

    def _selector_runner(_project_dir: Path) -> dict[str, object]:
        called["selector"] += 1
        return {"returncode": 0}

    scheduler = DailyResearchScheduler(
        project_dir=project_dir,
        research_root=research_root,
        candidate_root=candidate_root,
        selector_runner=_selector_runner,
    )

    result = scheduler.run(date(2026, 7, 13), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["applied"] is False
    assert result["steps"][1]["status"] == "planned"
    assert called["selector"] == 0
    assert not research_root.exists()


def test_daily_research_scheduler_runs_and_is_idempotent(tmp_path):
    project_dir = tmp_path / "project"
    candidate_root = project_dir / "artifacts" / "candidates"
    research_root = project_dir / "artifacts" / "research" / "daily"
    candidate_root.mkdir(parents=True, exist_ok=True)
    _seed_candidates(candidate_root)

    called = {"selector": 0}

    def _selector_runner(_project_dir: Path) -> dict[str, object]:
        called["selector"] += 1
        return {"returncode": 0}

    scheduler = DailyResearchScheduler(
        project_dir=project_dir,
        research_root=research_root,
        candidate_root=candidate_root,
        selector_runner=_selector_runner,
    )

    first = scheduler.run(date(2026, 7, 13), dry_run=False, force=False)
    assert first["status"] == "completed"
    assert first["applied"] is True
    assert first["candidate_count"] == 2
    assert first["report_status"] == "completed"
    assert called["selector"] == 1

    day_dir = research_root / "2026-07-13"
    audit_path = day_dir / "research_run_audit.json"
    report_json = day_dir / "daily_candidate_report.json"
    report_md = day_dir / "daily_candidate_report.md"
    assert audit_path.exists()
    assert report_json.exists()
    assert report_md.exists()

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "completed"
    assert audit["report_status"] == "completed"
    assert audit["all_trading_flags_false"] is True
    assert audit["trade_api_used"] is False
    assert audit["broker_used"] is False
    assert audit["trade_context_initialized"] is False

    existing_audit_text = audit_path.read_text(encoding="utf-8")
    second = scheduler.run(date(2026, 7, 13), dry_run=False, force=False)
    assert second["status"] == "already_completed"
    assert second["applied"] is False
    assert called["selector"] == 1
    assert audit_path.read_text(encoding="utf-8") == existing_audit_text

    forced = scheduler.run(date(2026, 7, 13), dry_run=False, force=True)
    assert forced["status"] == "completed"
    assert forced["applied"] is True
    assert called["selector"] == 2
    forced_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert forced_audit["status"] == "completed"
    assert forced_audit["report_status"] == "completed"
    assert forced_audit["research_dir"] == "artifacts/research/daily/2026-07-13"

    latest = latest_research_status(project_dir=project_dir, research_root=research_root)
    assert latest["status_label"] == "SAFE"
    assert latest["research_date"] == "2026-07-13"
    assert latest["candidate_count"] == 2
    assert latest["report_status"] == "completed"


def test_daily_research_scheduler_status_api_reads_latest_run(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    candidate_root = project_dir / "artifacts" / "candidates"
    research_root = project_dir / "artifacts" / "research" / "daily"
    candidate_root.mkdir(parents=True, exist_ok=True)
    _seed_candidates(candidate_root)

    scheduler = DailyResearchScheduler(
        project_dir=project_dir,
        research_root=research_root,
        candidate_root=candidate_root,
        selector_runner=lambda _project_dir: {"returncode": 0},
    )
    scheduler.run(date(2026, 7, 13), dry_run=False, force=False)

    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
    client = dashboard.app.test_client()

    response = client.get("/api/research/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status_label"] == "SAFE"
    assert payload["research_date"] == "2026-07-13"
    assert payload["candidate_count"] == 2
    assert payload["report_status"] == "completed"

    api_status = client.get("/api/status").get_json()
    assert api_status["research_status"]["status_label"] == "SAFE"
    assert api_status["research_status"]["candidate_count"] == 2
    assert api_status["research_status"]["report_status"] == "completed"
    assert "AI 研究调度" in client.get("/").data.decode("utf-8")
