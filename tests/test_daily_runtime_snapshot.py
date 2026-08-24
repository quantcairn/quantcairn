from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from src.config.runtime_paths import RuntimePaths
from src.reports.daily_runtime_snapshot import (
    _freshness,
    _parse_timestamp,
    collect_daily_runtime_snapshot,
    write_daily_runtime_snapshot,
)


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        project_dir=tmp_path / "code",
        state_dir=tmp_path / "runtime" / "state",
        reports_dir=tmp_path / "runtime" / "reports",
        artifacts_dir=tmp_path / "runtime" / "artifacts",
        logs_dir=tmp_path / "runtime" / "logs",
    )


def _write_evidence(paths: RuntimePaths) -> None:
    paths.reports_dir.mkdir(parents=True)
    paths.artifacts_dir.joinpath("candidates").mkdir(parents=True)
    paths.artifacts_dir.joinpath("research", "daily", "2026-08-25").mkdir(parents=True)
    paths.state_dir.joinpath("top_supervisor").mkdir(parents=True)
    (paths.reports_dir / "ai_selection_latest.json").write_text(json.dumps({
        "execution_status": "COMPLETED", "generated_at": "2026-08-25T22:00:00+08:00",
        "selection_run_id": "run-1", "selection_date": "2026-08-25",
        "final_selected_symbols": ["AIG", "AGNC"], "universe_count": 35,
        "filtered_count": 20, "formal_eligible_count": 5,
        "quality_checked_count": 5, "quality_timeout_count": 0,
    }), encoding="utf-8")
    (paths.artifacts_dir / "candidates" / "validation_scheduler_runs.jsonl").write_text(json.dumps({
        "status": "SAFE", "validation_run_id": "validation-1", "selection_run_id": "run-1",
        "selection_date": "2026-08-25", "timestamp": "2026-08-25T22:10:00+08:00",
        "candidates_scanned": 2, "candidates_advanced": 1,
        "candidates_failed": 1, "candidates_skipped": 0,
    }) + "\n", encoding="utf-8")
    research = paths.artifacts_dir / "research" / "daily" / "2026-08-25"
    (research / "research_run_audit.json").write_text(json.dumps({
        "status": "completed", "research_run_id": "research-1", "selection_run_id": "run-1",
        "selection_date": "2026-08-25", "candidate_count": 2, "completed_at": "2026-08-25T23:00:00+08:00",
    }), encoding="utf-8")
    (research / "daily_candidate_report.json").write_text(json.dumps({"candidate_count": 2}), encoding="utf-8")
    (paths.state_dir / "top_supervisor" / "status").write_text("state=running\n", encoding="utf-8")
    config = paths.project_dir / "configs"
    config.mkdir(parents=True)
    (config / "TOP1.yaml").write_text("enabled: true\nticker: AIG\nmode: paper\nselection_run_id: run-1\n", encoding="utf-8")


def test_snapshot_schema_and_provenance(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_evidence(paths)
    snapshot = collect_daily_runtime_snapshot(
        snapshot_date=date(2026, 8, 25), paths=paths,
        as_of=datetime(2026, 8, 25, 23, 30, tzinfo=timezone.utc),
    )
    assert snapshot["schema_version"] == "daily_runtime_snapshot.v1"
    assert snapshot["selector"]["final_candidates"] == ["AIG", "AGNC"]
    assert snapshot["selector"]["quality_timeout_count"] == 0
    assert snapshot["candidate_validation"]["rejected"] == 1
    assert snapshot["research"]["status"] == "COMPLETED"
    assert snapshot["evidence"]["selection_report"]["path"].endswith("reports/ai_selection_latest.json")
    assert snapshot["dashboard"]["status"] == "NOT_COLLECTED"
    assert snapshot["overall_status"] == "HEALTHY"
    assert snapshot["run_consistency"]["status"] == "CONSISTENT"
    assert snapshot["selector"]["freshness"]["status"] == "FRESH"


def test_missing_evidence_is_explicit_and_does_not_crash(tmp_path: Path) -> None:
    snapshot = collect_daily_runtime_snapshot(snapshot_date=date(2026, 8, 25), paths=_paths(tmp_path))
    assert snapshot["overall_status"] == "BLOCKED"
    assert snapshot["selector"]["status"] == "NOT_AVAILABLE"
    assert snapshot["candidate_validation"]["status"] == "NOT_AVAILABLE"
    assert snapshot["research"]["status"] == "NOT_AVAILABLE"


def test_daily_snapshot_writes_idempotently_and_atomically(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    snapshot = collect_daily_runtime_snapshot(snapshot_date=date(2026, 8, 25), paths=paths)
    first = write_daily_runtime_snapshot(snapshot, reports_dir=paths.reports_dir)
    snapshot["overall_status"] = "DEGRADED"
    second = write_daily_runtime_snapshot(snapshot, reports_dir=paths.reports_dir)
    assert first == second
    assert len(list((paths.reports_dir / "daily_runtime").glob("2026-08-25.*"))) == 2
    assert json.loads(first[0].read_text(encoding="utf-8"))["overall_status"] == "DEGRADED"
    assert not list((paths.reports_dir / "daily_runtime").glob("*.tmp"))


def test_snapshot_uses_external_report_root_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    snapshot = collect_daily_runtime_snapshot(snapshot_date=date(2026, 8, 25), paths=paths)
    write_daily_runtime_snapshot(snapshot, reports_dir=paths.reports_dir)
    assert (paths.reports_dir / "daily_runtime" / "2026-08-25.json").exists()
    assert not (paths.project_dir / "reports").exists()
    assert not (paths.project_dir / "state").exists()


def test_stale_selector_is_not_healthy_even_when_file_is_current(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_evidence(paths)
    report = paths.reports_dir / "ai_selection_latest.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["selection_date"] = "2026-08-21"
    payload["generated_at"] = "2026-08-21T22:00:00+08:00"
    report.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = collect_daily_runtime_snapshot(
        snapshot_date=date(2026, 8, 25), paths=paths,
        as_of=datetime(2026, 8, 25, 23, 0, tzinfo=timezone.utc),
    )
    assert snapshot["selector"]["freshness"]["status"] == "STALE"
    assert snapshot["overall_status"] != "HEALTHY"


def test_run_mismatch_is_structured(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_evidence(paths)
    top = paths.project_dir / "configs" / "TOP1.yaml"
    top.write_text("enabled: true\nticker: AIG\nmode: paper\nselection_run_id: old-run\n", encoding="utf-8")
    snapshot = collect_daily_runtime_snapshot(
        snapshot_date=date(2026, 8, 25), paths=paths,
        as_of=datetime(2026, 8, 25, 23, 0, tzinfo=timezone.utc),
    )
    assert snapshot["run_consistency"]["status"] == "MISMATCH"
    assert snapshot["overall_status"] != "HEALTHY"


def test_unknown_run_identity_is_not_fabricated(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_evidence(paths)
    for path in (paths.reports_dir / "ai_selection_latest.json", paths.artifacts_dir / "candidates" / "validation_scheduler_runs.jsonl"):
        text = path.read_text(encoding="utf-8").replace("run-1", "")
        path.write_text(text, encoding="utf-8")
    research = paths.artifacts_dir / "research" / "daily" / "2026-08-25" / "research_run_audit.json"
    research.write_text(research.read_text(encoding="utf-8").replace("run-1", ""), encoding="utf-8")
    (paths.project_dir / "configs" / "TOP1.yaml").write_text("enabled: true\nticker: AIG\nmode: paper\n", encoding="utf-8")
    snapshot = collect_daily_runtime_snapshot(snapshot_date=date(2026, 8, 25), paths=paths)
    assert snapshot["run_consistency"]["status"] == "UNKNOWN"
    assert snapshot["run_consistency"]["selection_run_id"] is None


def test_naive_timestamp_uses_host_local_timezone_not_utc() -> None:
    parsed = _parse_timestamp("2026-08-25T12:00:00")
    assert parsed is not None
    local = datetime(2026, 8, 25, 12, 0, tzinfo=datetime.now().astimezone().tzinfo)
    assert parsed == local.astimezone(timezone.utc)
    freshness = _freshness(
        source_timestamp="2026-08-25T12:00:00",
        source_operational_date="2026-08-25",
        expected_date=date(2026, 8, 25),
        as_of=local.astimezone(timezone.utc),
        policy_key="selector",
    )
    assert freshness["timestamp_timezone_source"] == "host_local_for_naive"


def test_aware_offsets_preserve_instant() -> None:
    plus_eight = _parse_timestamp("2026-08-25T20:00:00+08:00")
    utc = _parse_timestamp("2026-08-25T12:00:00Z")
    assert plus_eight is not None and utc is not None
    assert plus_eight == utc


def test_future_timestamp_is_unknown_not_stale() -> None:
    result = _freshness(
        source_timestamp="2026-08-26T00:00:00Z",
        source_operational_date="2026-08-25",
        expected_date=date(2026, 8, 25),
        as_of=datetime(2026, 8, 25, 23, 0, tzinfo=timezone.utc),
        policy_key="selector",
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "source_timestamp_in_future"


def test_old_operational_date_is_stale_even_with_recent_timestamp() -> None:
    result = _freshness(
        source_timestamp="2026-08-25T12:00:00Z",
        source_operational_date="2026-08-21",
        expected_date=date(2026, 8, 25),
        as_of=datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc),
        policy_key="selector",
    )
    assert result["status"] == "STALE"
    assert result["reason"] == "source_operational_date_older_than_snapshot_date"


def test_validation_and_research_keep_latest_different_run_provenance(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_evidence(paths)
    validation_path = paths.artifacts_dir / "candidates" / "validation_scheduler_runs.jsonl"
    validation_path.write_text(json.dumps({"status": "SAFE", "validation_run_id": "old-validation", "selection_run_id": "old-run", "timestamp": "2026-08-25T22:10:00+08:00"}) + "\n", encoding="utf-8")
    research_dir = paths.artifacts_dir / "research" / "daily" / "2026-08-24"
    research_dir.mkdir(parents=True)
    (research_dir / "research_run_audit.json").write_text(json.dumps({"status": "completed", "research_run_id": "old-research", "selection_run_id": "old-run", "completed_at": "2026-08-24T23:00:00+08:00"}), encoding="utf-8")
    current_audit = paths.artifacts_dir / "research" / "daily" / "2026-08-25" / "research_run_audit.json"
    current_audit.write_text(json.dumps({"status": "completed", "research_run_id": "old-research", "selection_run_id": "old-run", "completed_at": "2026-08-25T23:00:00+08:00"}), encoding="utf-8")
    snapshot = collect_daily_runtime_snapshot(snapshot_date=date(2026, 8, 25), paths=paths, as_of=datetime(2026, 8, 25, 23, 30, tzinfo=timezone.utc))
    assert snapshot["candidate_validation"]["relationship"] == "DIFFERENT_RUN"
    assert snapshot["research"]["relationship"] == "DIFFERENT_RUN"
    assert snapshot["run_consistency"]["status"] == "MISMATCH"
