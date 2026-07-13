from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.candidate_validation import CandidateRecord, CandidateTransitionError, CandidateValidationStore, ValidationStatus
from src.candidate_validation import walk_forward_admission_runner as runner


def _configure_runner(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    project_dir = tmp_path / "project"
    backtest_root = project_dir / "artifacts" / "candidates" / "backtests"
    validation_root = project_dir / "artifacts" / "candidates" / "validation"
    admission_root = project_dir / "artifacts" / "candidates" / "admission"
    wf_admission_root = project_dir / "artifacts" / "candidates" / "walk_forward_admission"
    monkeypatch.setattr(runner, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(runner, "DEFAULT_BACKTEST_ROOT", backtest_root)
    monkeypatch.setattr(runner, "DEFAULT_WALK_FORWARD_ADMISSION_ROOT", wf_admission_root)
    return project_dir, backtest_root, validation_root, admission_root, wf_admission_root


def _write_dataset_validation_report(
    validation_root: Path,
    candidate: CandidateRecord,
    *,
    benchmark_status: str = "VALID",
    eligible_for_backtest: bool = True,
    future_data_risk: bool = False,
    duplicate_count: int = 0,
    invalid_ohlc_count: int = 0,
) -> Path:
    report_dir = validation_root / candidate.candidate_id
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "dataset_validation_report.json"
    summary_path = report_dir / "dataset_validation_summary.csv"
    payload = {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "selected_at": candidate.selected_at,
        "candidate_current_status": ValidationStatus.PENDING_DATA_VALIDATION.value,
        "validation_status_before": ValidationStatus.PENDING_DATA_VALIDATION.value,
        "validation_status_after": ValidationStatus.DATA_VALID.value,
        "applied": True,
        "dry_run": False,
        "download_missing": False,
        "validator_version": "walk_forward_admission_smoke",
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
                "frequency_match": True,
                "overlap_ratio": 1.0,
                "missing_bar_ratio": 0.0,
                "duplicate_count": duplicate_count,
                "invalid_ohlc_count": invalid_ohlc_count,
                "missing_value_count": 0,
                "future_data_risk": future_data_risk,
                "eligible_for_backtest": eligible_for_backtest,
                "session_completeness_status": "COMPLETE",
                "fail_closed_reason": "",
            }
        ],
        "overall": {
            "status": ValidationStatus.DATA_VALID.value,
            "rejection_reason": "",
            "manifest_status": "OK",
            "benchmark_valid": benchmark_status == "VALID",
            "benchmark_status": benchmark_status,
            "eligible_for_backtest": eligible_for_backtest,
            "future_data_risk": future_data_risk,
            "reconciliation_status": "OK",
        },
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text("candidate_id,status\n", encoding="utf-8")
    return report_path


def _write_backtest_admission_audit(admission_root: Path, candidate: CandidateRecord, report_path: Path) -> Path:
    audit_dir = admission_root / candidate.candidate_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "backtest_admission_audit.json"
    payload = {
        "candidate_id": candidate.candidate_id,
        "candidate_current_status": ValidationStatus.PENDING_BACKTEST.value,
        "previous_status": ValidationStatus.DATA_VALID.value,
        "new_status": ValidationStatus.PENDING_BACKTEST.value,
        "dataset_report_path": str(report_path),
        "dataset_summary_path": str(report_path.with_name("dataset_validation_summary.csv")),
        "applied": True,
        "operator": "smoke_test",
        "reason": "manual backtest admission",
        "started_at": "2026-07-10T21:02:00+00:00",
        "completed_at": "2026-07-10T21:02:01+00:00",
    }
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit_path


def _write_backtest_artifacts(
    backtest_root: Path,
    candidate: CandidateRecord,
    *,
    benchmark_status: str = "VALID",
    reconciliation_status: str = "OK",
) -> tuple[Path, Path]:
    output_root = backtest_root / candidate.candidate_id
    run_dir = output_root / "comparison_fake"
    run_dir.mkdir(parents=True, exist_ok=True)
    comparison_summary = {
        "symbol": candidate.symbol,
        "benchmark_symbol": candidate.benchmarks[0] if candidate.benchmarks else "",
        "benchmark_status": benchmark_status,
        "reconciliation_status": reconciliation_status,
        "evidence_status": "INSUFFICIENT_EVIDENCE",
        "profitability_status": "INELIGIBLE",
        "deployment_status": "INELIGIBLE",
        "ranking_status": "INSUFFICIENT_EVIDENCE",
        "strategy_versions": ["baseline", "a", "b", "c"],
    }
    (run_dir / "comparison_summary.json").write_text(json.dumps(comparison_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "strategy_metrics.csv").write_text("version,metric\nbaseline,1\n", encoding="utf-8")
    (run_dir / "strategy_ranking.csv").write_text("version,rank\nbaseline,1\n", encoding="utf-8")
    (run_dir / "parameter_stability.json").write_text(json.dumps({"selected_parameters": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "warnings.json").write_text("[]\n", encoding="utf-8")
    (run_dir / "configuration.json").write_text(json.dumps({"strategy_family": candidate.strategy_family}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "blocked_reason_counts.csv").write_text("reason,count\n", encoding="utf-8")
    (run_dir / "blocked_reason_by_strategy.csv").write_text("strategy,reason,count\n", encoding="utf-8")
    (run_dir / "report.md").write_text("# walk forward smoke\n", encoding="utf-8")
    for version in ("baseline", "a", "b", "c"):
        for name in (f"trades_{version}.csv", f"orders_{version}.csv", f"equity_{version}.csv", f"drawdown_{version}.csv", f"rejected_{version}.csv"):
            (run_dir / name).write_text("row\n", encoding="utf-8")
    metrics = [
        {
            "version": version,
            "strategy": version,
            "trade_count_definition": "fill_count",
            "order_count": 1,
            "fill_count": 1,
            "closed_trade_count": 1,
            "open_position_count": 0,
            "open_position_quantity": 0,
            "winning_trade_count": 1,
            "losing_trade_count": 0,
            "win_rate": 1.0,
            "profit_factor": 1.2,
            "profit_factor_status": "OK",
            "total_return": 0.01,
            "max_drawdown": 0.01,
            "sharpe": 1.0,
            "sortino": 1.0,
            "calmar": 1.0,
            "turnover": 1.0,
            "exposure": 0.25,
            "commission": 0.01,
            "slippage": 0.01,
            "spread_cost": 0.0,
            "blocked_by_trend_count": 0,
            "blocked_by_cost_count": 0,
            "blocked_by_inventory_count": 0,
            "time_stop_evaluation_count": 0,
            "time_stop_signal_count": 0,
            "time_stop_exit_count": 0,
            "time_stop_blocked_count": 0,
            "reconciliation_status": reconciliation_status,
            "reconciliation_difference": 0.0,
            "evidence_status": "INSUFFICIENT_EVIDENCE",
            "profitability_status": "INELIGIBLE",
            "deployment_status": "INELIGIBLE",
            "benchmark_status": benchmark_status,
        }
        for version in ("baseline", "a", "b", "c")
    ]
    audit_payload = {
        "candidate_id": candidate.candidate_id,
        "previous_status": ValidationStatus.PENDING_BACKTEST.value,
        "proposed_status": ValidationStatus.BACKTEST_COMPLETE.value,
        "final_status": ValidationStatus.BACKTEST_COMPLETE.value,
        "candidate_current_status": ValidationStatus.BACKTEST_COMPLETE.value,
        "current_status_after": ValidationStatus.BACKTEST_COMPLETE.value,
        "candidate_status_after": ValidationStatus.BACKTEST_COMPLETE.value,
        "operator": "smoke_test",
        "reason": "manual backtest smoke",
        "started_at": "2026-07-10T21:03:00+00:00",
        "completed_at": "2026-07-10T21:03:01+00:00",
        "symbol": candidate.symbol,
        "benchmarks": list(candidate.benchmarks),
        "timeframe": candidate.timeframe,
        "strategy_family": candidate.strategy_family,
        "strategy_versions": ["baseline", "a", "b", "c"],
        "backtest_run_dir": str(run_dir),
        "backtest_run_path": str(run_dir),
        "comparison_summary": comparison_summary,
        "backtest_metrics": metrics,
        "evidence_status": "INSUFFICIENT_EVIDENCE",
        "profitability_status": "INELIGIBLE",
        "deployment_status": "INELIGIBLE",
        "reconciliation_status": reconciliation_status,
        "applied": True,
        "success": reconciliation_status == "OK",
        "failure_reason": "" if reconciliation_status == "OK" else "reconciliation_failed",
        "backtest_engine_version": "manual_candidate_backtest_v1",
        "trade_api_used": False,
        "broker_used": False,
        "trade_context_initialized": False,
    }
    audit_path = output_root / "backtest_run_audit.json"
    audit_path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "backtest_run_summary.csv").write_text(
        "candidate_id,previous_status,final_status,operator,reason,started_at,completed_at,symbol,benchmarks,timeframe,strategy_family\n"
        f"{candidate.candidate_id},PENDING_BACKTEST,BACKTEST_COMPLETE,smoke_test,manual backtest smoke,2026-07-10T21:03:00+00:00,2026-07-10T21:03:01+00:00,{candidate.symbol},\"{'|'.join(candidate.benchmarks)}\",{candidate.timeframe},{candidate.strategy_family}\n",
        encoding="utf-8",
    )
    return audit_path, run_dir


def _make_backtest_complete_candidate(
    store: CandidateValidationStore,
    *,
    validation_root: Path,
    admission_root: Path,
    backtest_root: Path,
    symbol: str = "SOXS.US",
    benchmarks: tuple[str, ...] = ("SOXX.US", "SMH.US"),
    asset_type: str = "inverse_etf",
    strategy_family: str = "inverse_etf_range",
    risk_profile: str = "strict",
) -> CandidateRecord:
    candidate = CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=94.2,
        ai_reason="walk forward admission smoke",
        asset_type=asset_type,
        benchmarks=benchmarks,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe="15m",
        market="US",
        metadata={},
    )
    report_path = _write_dataset_validation_report(validation_root, candidate)
    admission_audit_path = _write_backtest_admission_audit(admission_root, candidate, report_path)
    backtest_audit_path, backtest_run_dir = _write_backtest_artifacts(backtest_root, candidate)
    store.save_candidates([candidate])
    for status in (
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
        ValidationStatus.DATA_VALID,
        ValidationStatus.PENDING_BACKTEST,
    ):
        metadata = {
            "benchmark_valid": True,
            "eligible_for_backtest": True,
            "future_data_risk": False,
            "reconciliation_status": "OK",
            "backtest_admission_audit_path": str(admission_audit_path),
            "dataset_validation_report_path": str(report_path),
            "dataset_validation_summary_path": str(report_path.with_name("dataset_validation_summary.csv")),
        }
        if status == ValidationStatus.PENDING_BACKTEST:
            metadata = {
                **metadata,
                "data_status": ValidationStatus.DATA_VALID.value,
                "backtest_admission_audit_path": str(admission_audit_path),
            }
        store.transition(candidate.candidate_id, status, metadata=metadata)
    store.transition(
        candidate.candidate_id,
        ValidationStatus.BACKTEST_COMPLETE,
        metadata={
            "backtest_complete": True,
            "backtest_audit_path": str(backtest_audit_path),
            "backtest_summary_path": str(backtest_run_dir.parent / "backtest_run_summary.csv"),
            "backtest_run_path": str(backtest_run_dir),
            "backtest_run_dir": str(backtest_run_dir),
            "backtest_admission_audit_path": str(admission_audit_path),
            "dataset_validation_report_path": str(report_path),
            "dataset_validation_summary_path": str(report_path.with_name("dataset_validation_summary.csv")),
            "benchmark_valid": True,
            "eligible_for_backtest": True,
            "future_data_risk": False,
            "reconciliation_status": "OK",
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
        },
    )
    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    return refreshed


def test_backtest_complete_dry_run_leaves_state_unchanged(tmp_path, monkeypatch):
    _, backtest_root, validation_root, admission_root, wf_admission_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )

    result = runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=True,
        operator="ops",
        reason="smoke",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.BACKTEST_COMPLETE.value
    assert result["candidate_current_status"] == ValidationStatus.BACKTEST_COMPLETE.value
    assert result["proposed_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
    assert result["applied"] is False
    assert result["dry_run"] is True
    assert result["reconciliation_all_ok"] is True
    assert result["completed_versions"] == ["a", "b", "baseline", "c"] or sorted(result["completed_versions"]) == ["a", "b", "baseline", "c"]
    assert result["trade_api_used"] is False
    assert result["broker_used"] is False
    assert result["walk_forward_started"] is False
    assert (wf_admission_root / candidate.candidate_id / "walk_forward_admission_audit.json").exists()
    assert (wf_admission_root / candidate.candidate_id / "walk_forward_admission_summary.csv").exists()


def test_backtest_complete_apply_moves_to_pending_walk_forward(tmp_path, monkeypatch):
    _, backtest_root, validation_root, admission_root, wf_admission_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )

    result = runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=False,
        operator="ops",
        reason="smoke",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.PENDING_WALK_FORWARD.value
    assert refreshed.trading_enabled is False
    assert refreshed.shadow_enabled is False
    assert refreshed.paper_enabled is False
    assert refreshed.live_enabled is False
    assert refreshed.metadata["operator"] == "ops"
    assert refreshed.metadata["reason"] == "smoke"
    assert refreshed.metadata["operator_action"] == "manual_walk_forward_admission"
    assert refreshed.metadata["backtest_status"] == ValidationStatus.BACKTEST_COMPLETE.value
    assert refreshed.metadata["walk_forward_admission_audit_path"]
    assert refreshed.metadata["walk_forward_admission_summary_path"]
    assert result["applied"] is True
    assert result["candidate_current_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
    assert (wf_admission_root / candidate.candidate_id / "walk_forward_admission_audit.json").exists()
    history = store.load_latest_history()
    assert any(item["to_status"] == ValidationStatus.PENDING_WALK_FORWARD.value for item in history)
    assert all(item["to_status"] != ValidationStatus.WALK_FORWARD_COMPLETE.value for item in history)


def test_backtest_failed_or_pending_rejected(tmp_path, monkeypatch):
    _, backtest_root, validation_root, admission_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )
    store.transition(
        candidate.candidate_id,
        ValidationStatus.REJECTED,
        reason="failed",
        metadata={"reason": "failed"},
    )
    with pytest.raises(CandidateTransitionError, match="candidate_must_be_backtest_complete"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )

    pending_store = CandidateValidationStore(tmp_path / "pending_store")
    pending_candidate = CandidateRecord.from_ai_candidate(
        symbol="SOXS.US",
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=94.2,
        ai_reason="walk forward admission smoke",
        asset_type="inverse_etf",
        benchmarks=("SOXX.US", "SMH.US"),
        strategy_family="inverse_etf_range",
        risk_profile="strict",
        timeframe="15m",
        market="US",
        metadata={},
    )
    pending_store.save_candidates([pending_candidate])
    for status in (
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
        ValidationStatus.DATA_VALID,
        ValidationStatus.PENDING_BACKTEST,
    ):
        metadata = {
            "benchmark_valid": True,
            "eligible_for_backtest": True,
            "future_data_risk": False,
            "reconciliation_status": "OK",
            "data_status": ValidationStatus.DATA_VALID.value,
            "backtest_status": ValidationStatus.BACKTEST_COMPLETE.value,
        }
        pending_store.transition(
            pending_candidate.candidate_id,
            status,
            metadata=metadata,
        )
    pending_store.transition(
        pending_candidate.candidate_id,
        ValidationStatus.BACKTEST_FAILED,
        reason="broken",
        metadata={"reason": "broken"},
    )
    with pytest.raises(CandidateTransitionError, match="candidate_must_be_backtest_complete"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=pending_candidate.candidate_id,
            candidate_store=pending_store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


def test_backtest_audit_validation_blocks_incomplete_or_mismatched_data(tmp_path, monkeypatch):
    _, backtest_root, validation_root, admission_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )

    audit_path = Path(candidate.metadata["backtest_audit_path"])
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["symbol"] = "QQQ.US"
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(CandidateTransitionError, match="backtest_audit_symbol_mismatch"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )

    # build a second clean candidate and then break its artifacts
    candidate2 = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )
    missing_path = backtest_root / candidate2.candidate_id / "comparison_fake" / "strategy_metrics.csv"
    missing_path.unlink()
    with pytest.raises(CandidateTransitionError, match="backtest_artifacts_missing"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate2.candidate_id,
            candidate_store=store.root_dir,
            dry_run=True,
            operator="ops",
            reason="smoke",
        )


def test_repeat_apply_rejected_and_history_not_overwritten(tmp_path, monkeypatch):
    _, backtest_root, validation_root, admission_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )
    before_history = len(store.load_latest_history())
    first = runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        dry_run=False,
        operator="ops",
        reason="smoke",
    )
    assert first["applied"] is True
    after_history = len(store.load_latest_history())
    assert after_history == before_history + 1
    with pytest.raises(CandidateTransitionError, match="candidate_must_be_backtest_complete"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            dry_run=False,
            operator="ops",
            reason="smoke",
        )
    assert len(store.load_latest_history()) == after_history


def test_walk_forward_admission_cli_supports_dry_run_and_apply(tmp_path, monkeypatch):
    project_dir, backtest_root, validation_root, admission_root, _ = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_backtest_complete_candidate(
        store,
        validation_root=validation_root,
        admission_root=admission_root,
        backtest_root=backtest_root,
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "admit_candidate_to_walk_forward.py"
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
    assert dry_payload["candidate_current_status"] == ValidationStatus.BACKTEST_COMPLETE.value
    assert dry_payload["proposed_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
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
    assert apply_payload["candidate_current_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
    assert apply_payload["applied"] is True
