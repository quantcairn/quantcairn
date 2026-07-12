from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.candidate_validation import (
    CandidateRecord,
    CandidateTransitionError,
    CandidateValidationStore,
    ValidationStatus,
)
from src.candidate_validation import backtest_admission_runner as runner


def _write_admission_ready_report(
    base_dir: Path,
    candidate: CandidateRecord,
    *,
    benchmark_status: str = "VALID",
    eligible_for_backtest: bool = True,
    future_data_risk: bool = False,
    duplicate_count: int = 0,
    invalid_ohlc_count: int = 0,
    frequency_match: bool = True,
    candidate_status: str = ValidationStatus.PENDING_DATA_VALIDATION.value,
) -> Path:
    report_dir = base_dir / candidate.candidate_id
    report_dir.mkdir(parents=True, exist_ok=True)
    overall_status = (
        ValidationStatus.DATA_VALID.value
        if benchmark_status == "VALID"
        and eligible_for_backtest
        and not future_data_risk
        and duplicate_count == 0
        and invalid_ohlc_count == 0
        and frequency_match
        else ValidationStatus.DATA_INVALID.value
    )
    payload = {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "selected_at": candidate.selected_at,
        "candidate_current_status": candidate_status,
        "validation_status_before": candidate_status,
        "validation_status_after": overall_status,
        "applied": True,
        "dry_run": False,
        "download_missing": False,
        "validator_version": "test",
        "started_at": "2026-07-10T21:00:00+00:00",
        "completed_at": "2026-07-10T21:00:01+00:00",
        "candidate": candidate.to_dict(),
        "data_sources": [],
        "validations": [
            {
                "symbol": candidate.symbol,
                "benchmark": candidate.benchmarks[0] if candidate.benchmarks else "",
                "frequency": candidate.timeframe,
                "benchmark_status": benchmark_status,
                "frequency_match": frequency_match,
                "overlap_ratio": 1.0,
                "missing_bar_ratio": 0.0,
                "duplicate_count": duplicate_count,
                "invalid_ohlc_count": invalid_ohlc_count,
                "missing_value_count": 0,
                "future_data_risk": future_data_risk,
                "eligible_for_backtest": eligible_for_backtest,
                "session_completeness_status": "COMPLETE",
                "fail_closed_reason": "" if overall_status == ValidationStatus.DATA_VALID.value else "blocked",
                "report": {
                    "symbol": candidate.symbol,
                    "benchmark_symbol": candidate.benchmarks[0] if candidate.benchmarks else "",
                    "expected_frequency": candidate.timeframe,
                    "benchmark_mapping_status": benchmark_status,
                    "frequency_match": frequency_match,
                    "overlap_ratio": 1.0,
                    "missing_bar_ratio": 0.0,
                    "duplicate_timestamp_count": duplicate_count,
                    "invalid_ohlc_count": invalid_ohlc_count,
                    "missing_value_count": 0,
                    "future_data_risk": future_data_risk,
                    "eligible_for_backtest": eligible_for_backtest,
                    "session_completeness_status": "COMPLETE",
                    "fail_closed_reason": "" if overall_status == ValidationStatus.DATA_VALID.value else "blocked",
                    "data_start": "2026-07-10T13:30:00+00:00",
                    "data_end": "2026-07-10T20:00:00+00:00",
                    "benchmark_start": "2026-07-10T13:30:00+00:00",
                    "benchmark_end": "2026-07-10T20:00:00+00:00",
                },
            }
        ],
        "overall": {
            "status": overall_status,
            "rejection_reason": "" if overall_status == ValidationStatus.DATA_VALID.value else "blocked",
            "manifest_status": "OK",
            "benchmark_valid": benchmark_status == "VALID",
            "eligible_for_backtest": eligible_for_backtest,
            "future_data_risk": future_data_risk,
            "reconciliation_status": "OK",
        },
    }
    report_path = report_dir / "dataset_validation_report.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "dataset_validation_summary.csv").write_text("candidate_id,status\n", encoding="utf-8")
    return report_path


def _make_data_valid_candidate(
    store: CandidateValidationStore,
    *,
    symbol: str = "AAPL.US",
    benchmarks: tuple[str, ...] = ("QQQ.US",),
    asset_type: str = "common_stock",
    strategy_family: str = "equity_mean_reversion",
    risk_profile: str = "balanced",
    metadata: dict[str, object] | None = None,
) -> CandidateRecord:
    pending_metadata = dict(metadata or {})
    candidate = CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=91.2,
        ai_reason="manual admission smoke",
        asset_type=asset_type,
        benchmarks=benchmarks,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe="15m",
        market="US",
        metadata=pending_metadata,
    )
    if "dataset_validation_report_path" not in candidate.metadata:
        candidate.metadata["dataset_validation_report_path"] = str(Path(candidate.candidate_id) / "dataset_validation_report.json")
    if "dataset_validation_summary_path" not in candidate.metadata:
        candidate.metadata["dataset_validation_summary_path"] = str(Path(candidate.candidate_id) / "dataset_validation_summary.csv")
    store.save_candidates([candidate])
    for status in (
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
    ):
        store.transition(candidate.candidate_id, status, metadata={})
    store.transition(
        candidate.candidate_id,
        ValidationStatus.DATA_VALID,
        metadata={
            "benchmark_valid": True,
            "eligible_for_backtest": True,
            "future_data_risk": False,
            "reconciliation_status": "OK",
        },
    )
    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    return refreshed


def _configure_runner(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    project_dir = tmp_path / "project"
    validation_root = project_dir / "artifacts" / "candidates" / "validation"
    admission_root = project_dir / "artifacts" / "candidates" / "admission"
    monkeypatch.setattr(runner, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(runner, "DEFAULT_VALIDATION_ROOT", validation_root)
    monkeypatch.setattr(runner, "DEFAULT_ADMISSION_ROOT", admission_root)
    return project_dir, validation_root, admission_root


def test_admit_candidate_dry_run_leaves_state_unchanged(tmp_path, monkeypatch):
    project_dir, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    result = runner.admit_candidate_to_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=True,
        operator="ops",
        reason="smoke",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.DATA_VALID.value
    assert result["candidate_current_status"] == ValidationStatus.DATA_VALID.value
    assert result["proposed_status"] == ValidationStatus.PENDING_BACKTEST.value
    assert result["applied"] is False
    assert result["dry_run"] is True
    assert json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))["applied"] is False
    assert json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))["candidate_status_after"] == ValidationStatus.DATA_VALID.value
    assert Path(result["summary_path"]).exists()
    assert Path(result["report_path"]).exists()
    assert project_dir.exists()


def test_admit_candidate_apply_updates_state_and_history(tmp_path, monkeypatch):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    result = runner.admit_candidate_to_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=False,
        operator="ops",
        reason="go ahead",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.PENDING_BACKTEST.value
    assert refreshed.trading_enabled is False
    assert refreshed.shadow_enabled is False
    assert refreshed.paper_enabled is False
    assert refreshed.live_enabled is False
    assert refreshed.metadata["operator"] == "ops"
    assert refreshed.metadata["reason"] == "go ahead"
    assert refreshed.metadata["admitted_at"]
    history = store.load_latest_history()
    assert any(item["to_status"] == ValidationStatus.PENDING_BACKTEST.value for item in history)
    assert all(item["to_status"] != ValidationStatus.BACKTEST_COMPLETE.value for item in history)
    assert result["applied"] is True
    assert result["new_status"] == ValidationStatus.PENDING_BACKTEST.value


@pytest.mark.parametrize("target_status", [ValidationStatus.DATA_INVALID.value, ValidationStatus.PENDING_DATA_VALIDATION.value])
def test_admit_candidate_rejects_non_data_valid_state(tmp_path, monkeypatch, target_status):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    if target_status == ValidationStatus.PENDING_DATA_VALIDATION.value:
        candidate = CandidateRecord.from_ai_candidate(
            symbol="AAPL.US",
            selected_at="2026-07-10T21:32:41+08:00",
            source="ai_selector",
            ai_score=91.2,
            ai_reason="manual admission smoke",
            asset_type="common_stock",
            benchmarks=("QQQ.US",),
            strategy_family="equity_mean_reversion",
            risk_profile="balanced",
            timeframe="15m",
            market="US",
            metadata={
                "dataset_validation_report_path": "missing/report.json",
                "dataset_validation_summary_path": "missing/summary.csv",
            },
        )
        store.save_candidates([candidate])
        for status in (
            ValidationStatus.CLASSIFIED,
            ValidationStatus.BENCHMARK_ASSIGNED,
            ValidationStatus.STRATEGY_ASSIGNED,
            ValidationStatus.PENDING_DATA_VALIDATION,
        ):
            store.transition(candidate.candidate_id, status, metadata={})
    else:
        candidate = CandidateRecord.from_ai_candidate(
            symbol="AAPL.US",
            selected_at="2026-07-10T21:32:41+08:00",
            source="ai_selector",
            ai_score=91.2,
            ai_reason="manual admission smoke",
            asset_type="common_stock",
            benchmarks=("QQQ.US",),
            strategy_family="equity_mean_reversion",
            risk_profile="balanced",
            timeframe="15m",
            market="US",
            metadata={},
        )
        candidate.metadata["dataset_validation_report_path"] = str(Path(candidate.candidate_id) / "dataset_validation_report.json")
        candidate.metadata["dataset_validation_summary_path"] = str(Path(candidate.candidate_id) / "dataset_validation_summary.csv")
        store.save_candidates([candidate])
        for status in (
            ValidationStatus.CLASSIFIED,
            ValidationStatus.BENCHMARK_ASSIGNED,
            ValidationStatus.STRATEGY_ASSIGNED,
            ValidationStatus.PENDING_DATA_VALIDATION,
        ):
            store.transition(candidate.candidate_id, status, metadata={})
        store.transition(candidate.candidate_id, ValidationStatus.DATA_INVALID, reason="blocked", metadata={"reason": "blocked"})
    _write_admission_ready_report(validation_root, candidate, candidate_status=candidate.validation_status)
    with pytest.raises(CandidateTransitionError, match="candidate_must_be_data_valid"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


def test_admit_candidate_rejects_invalid_report_metadata(tmp_path, monkeypatch):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    report_path = validation_root / candidate.candidate_id / "dataset_validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["overall"]["benchmark_valid"] = False
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(CandidateTransitionError, match="benchmark_status_invalid"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


def test_admit_candidate_rejects_missing_or_corrupt_report(tmp_path, monkeypatch):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})

    with pytest.raises(CandidateTransitionError, match="dataset_report_missing"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )

    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)
    report_path = validation_root / candidate.candidate_id / "dataset_validation_report.json"
    report_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(CandidateTransitionError, match="dataset_report_invalid"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


@pytest.mark.parametrize(
    "field, value, expected_error",
    [
        ("eligible_for_backtest", False, "eligible_for_backtest_required"),
        ("future_data_risk", True, "future_data_risk_must_be_false"),
        ("duplicate_count", 1, "duplicate_count_must_be_zero"),
        ("invalid_ohlc_count", 1, "invalid_ohlc_count_must_be_zero"),
        ("frequency_match", False, "frequency_mismatch"),
    ],
)
def test_admit_candidate_rejects_invalid_report_checks(tmp_path, monkeypatch, field, value, expected_error):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    report_path = validation_root / candidate.candidate_id / "dataset_validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if field == "frequency_match":
        report["validations"][0][field] = value
        report["validations"][0]["report"][field] = value
    else:
        report["overall"][field] = value
        report["validations"][0][field] = value
        report["validations"][0]["report"][field] = value
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(CandidateTransitionError, match=expected_error):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


def test_admit_candidate_rejects_symbol_mismatch(tmp_path, monkeypatch):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)
    report_path = validation_root / candidate.candidate_id / "dataset_validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["symbol"] = "MSFT.US"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(CandidateTransitionError, match="dataset_report_symbol_mismatch"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


def test_admit_candidate_rejects_bad_inputs_and_does_not_auto_start_backtest(tmp_path, monkeypatch):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    with pytest.raises(CandidateTransitionError, match="candidate_not_found"):
        runner.admit_candidate_to_backtest(
            candidate_id="missing",
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )
    with pytest.raises(CandidateTransitionError, match="reason_required"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="",
        )
    with pytest.raises(CandidateTransitionError, match="operator_required"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="",
            reason="smoke",
        )

    result = runner.admit_candidate_to_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=True,
        operator="ops",
        reason="smoke",
    )
    assert result["trade_api_used"] is False
    assert result["broker_used"] is False
    assert result["applied"] is False
    assert result["proposed_status"] == ValidationStatus.PENDING_BACKTEST.value
    history = store.load_latest_history()
    assert all(item["to_status"] != ValidationStatus.PENDING_BACKTEST.value or item.get("reason") != "smoke" for item in history)


def test_admit_candidate_rejects_repeated_apply(tmp_path, monkeypatch):
    _, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    first = runner.admit_candidate_to_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=False,
        operator="ops",
        reason="smoke",
    )
    assert first["applied"] is True
    with pytest.raises(CandidateTransitionError, match="candidate_must_be_data_valid"):
        runner.admit_candidate_to_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=False,
            operator="ops",
            reason="smoke",
        )


def test_admit_candidate_cli_supports_dry_run_and_apply(tmp_path, monkeypatch):
    project_dir, validation_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_data_valid_candidate(store, metadata={})
    _write_admission_ready_report(validation_root, candidate)

    script = Path(__file__).resolve().parents[1] / "scripts" / "admit_candidate_to_backtest.py"
    env = dict(os.environ)
    env["SOXS_PROJECT_DIR"] = str(project_dir)
    dry = subprocess.run(
        [
            sys.executable,
            str(script),
            "--candidate-id",
            candidate.candidate_id,
            "--candidate-store",
            str(store.root_dir),
            "--operator",
            "ops",
            "--reason",
            "smoke",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert dry.returncode == 0
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["candidate_current_status"] == ValidationStatus.DATA_VALID.value
    assert dry_payload["proposed_status"] == ValidationStatus.PENDING_BACKTEST.value
    assert dry_payload["applied"] is False

    apply = subprocess.run(
        [
            sys.executable,
            str(script),
            "--candidate-id",
            candidate.candidate_id,
            "--candidate-store",
            str(store.root_dir),
            "--operator",
            "ops",
            "--reason",
            "smoke",
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert apply.returncode == 0
    apply_payload = json.loads(apply.stdout)
    assert apply_payload["candidate_current_status"] == ValidationStatus.PENDING_BACKTEST.value
    assert apply_payload["applied"] is True
