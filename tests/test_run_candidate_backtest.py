from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.candidate_validation import CandidateRecord, CandidateTransitionError, CandidateValidationStore, ValidationStatus
from src.candidate_validation import backtest_execution_runner as runner


REPO_ROOT = Path("/Users/chenwei/soxs-range-arbitrage")


def _write_ohlcv_csv(path: Path, symbol: str, *, base_utc: str = "2026-07-10T13:30:00+00:00") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = datetime.fromisoformat(base_utc)
    second = first.replace(minute=45)
    header = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "frequency", "timezone", "source"]
    rows = [
        [symbol, first.isoformat(), 10.0, 10.5, 9.9, 10.2, 1000, "15m", "UTC", "unit_test"],
        [symbol, second.isoformat(), 10.2, 10.6, 10.1, 10.4, 1200, "15m", "UTC", "unit_test"],
    ]
    text = [",".join(header)]
    for row in rows:
        text.append(",".join(str(item) for item in row))
    path.write_text("\n".join(text) + "\n", encoding="utf-8")
    return path


def _configure_runner(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    project_dir = tmp_path / "project"
    validation_root = project_dir / "artifacts" / "candidates" / "validation"
    admission_root = project_dir / "artifacts" / "candidates" / "admission"
    backtest_root = project_dir / "artifacts" / "candidates" / "backtests"
    data_root = project_dir / "data" / "market" / "longbridge"
    monkeypatch.setattr(runner, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(runner, "DEFAULT_VALIDATION_ROOT", validation_root)
    monkeypatch.setattr(runner, "DEFAULT_ADMISSION_ROOT", admission_root)
    monkeypatch.setattr(runner, "DEFAULT_BACKTEST_ROOT", backtest_root)
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)
    return project_dir, validation_root, admission_root, backtest_root, data_root


def _make_pending_backtest_candidate(
    store: CandidateValidationStore,
    *,
    symbol: str,
    benchmarks: tuple[str, ...],
    asset_type: str,
    strategy_family: str,
    risk_profile: str,
    validation_root: Path,
    admission_root: Path,
    benchmark_symbol: str | None = None,
) -> CandidateRecord:
    candidate = CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=92.1,
        ai_reason="manual backtest smoke",
        asset_type=asset_type,
        benchmarks=benchmarks,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe="15m",
        market="US",
        metadata={},
    )
    audit_dir = admission_root / candidate.candidate_id
    report_dir = validation_root / candidate.candidate_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    benchmark = benchmark_symbol or (benchmarks[0] if benchmarks else "")
    report_path = report_dir / "dataset_validation_report.json"
    summary_path = report_dir / "dataset_validation_summary.csv"
    report_payload = {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "selected_at": candidate.selected_at,
        "candidate_current_status": ValidationStatus.PENDING_DATA_VALIDATION.value,
        "validation_status_before": ValidationStatus.PENDING_DATA_VALIDATION.value,
        "validation_status_after": ValidationStatus.DATA_VALID.value,
        "applied": True,
        "dry_run": False,
        "download_missing": False,
        "validator_version": "unit_test",
        "started_at": "2026-07-10T21:00:00+00:00",
        "completed_at": "2026-07-10T21:00:01+00:00",
        "candidate": candidate.to_dict(),
        "data_sources": [],
        "validations": [
            {
                "symbol": candidate.symbol,
                "benchmark": benchmark,
                "frequency": candidate.timeframe,
                "benchmark_status": "VALID",
                "frequency_match": True,
                "overlap_ratio": 1.0,
                "missing_bar_ratio": 0.0,
                "duplicate_count": 0,
                "invalid_ohlc_count": 0,
                "missing_value_count": 0,
                "future_data_risk": False,
                "eligible_for_backtest": True,
                "session_completeness_status": "COMPLETE",
                "fail_closed_reason": "",
                "report": {
                    "symbol": candidate.symbol,
                    "benchmark_symbol": benchmark,
                    "expected_frequency": candidate.timeframe,
                    "benchmark_mapping_status": "VALID",
                    "frequency_match": True,
                    "overlap_ratio": 1.0,
                    "missing_bar_ratio": 0.0,
                    "duplicate_timestamp_count": 0,
                    "invalid_ohlc_count": 0,
                    "missing_value_count": 0,
                    "future_data_risk": False,
                    "eligible_for_backtest": True,
                    "session_completeness_status": "COMPLETE",
                    "fail_closed_reason": "",
                    "data_start": "2026-07-10T13:30:00+00:00",
                    "data_end": "2026-07-10T20:00:00+00:00",
                    "benchmark_start": "2026-07-10T13:30:00+00:00",
                    "benchmark_end": "2026-07-10T20:00:00+00:00",
                },
            }
        ],
        "overall": {
            "status": ValidationStatus.DATA_VALID.value,
            "rejection_reason": "",
            "manifest_status": "OK",
            "benchmark_valid": True,
            "benchmark_status": "VALID",
            "eligible_for_backtest": True,
            "future_data_risk": False,
            "reconciliation_status": "OK",
        },
    }
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text("candidate_id,status\n", encoding="utf-8")
    audit_payload = {
        "candidate_id": candidate.candidate_id,
        "candidate_current_status": ValidationStatus.DATA_VALID.value,
        "previous_status": ValidationStatus.DATA_VALID.value,
        "new_status": ValidationStatus.PENDING_BACKTEST.value,
        "dataset_report_path": str(Path(candidate.candidate_id) / "dataset_validation_report.json"),
        "applied": True,
        "operator": "ops",
        "reason": "manual backtest admission",
        "started_at": "2026-07-10T21:02:00+00:00",
        "completed_at": "2026-07-10T21:02:01+00:00",
    }
    (audit_dir / "backtest_admission_audit.json").write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    store.save_candidates([candidate])
    for status in (
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
        ValidationStatus.DATA_VALID,
    ):
        store.transition(
            candidate.candidate_id,
            status,
            metadata={
                "benchmark_valid": True,
                "eligible_for_backtest": True,
                "future_data_risk": False,
                "reconciliation_status": "OK",
                "backtest_admission_audit_path": str(Path(candidate.candidate_id) / "backtest_admission_audit.json"),
                "dataset_validation_report_path": str(Path(candidate.candidate_id) / "dataset_validation_report.json"),
                "dataset_validation_summary_path": str(Path(candidate.candidate_id) / "dataset_validation_summary.csv"),
            },
        )
    store.transition(
        candidate.candidate_id,
        ValidationStatus.PENDING_BACKTEST,
        metadata={
            "data_status": ValidationStatus.DATA_VALID.value,
            "backtest_admission_audit_path": str(Path(candidate.candidate_id) / "backtest_admission_audit.json"),
            "dataset_validation_report_path": str(Path(candidate.candidate_id) / "dataset_validation_report.json"),
            "dataset_validation_summary_path": str(Path(candidate.candidate_id) / "dataset_validation_summary.csv"),
        },
    )
    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    return refreshed


def _fake_comparison_result(candidate: CandidateRecord, *, success: bool = True):
    row = {
        "version": "baseline",
        "strategy": "baseline",
        "trade_count_definition": "fill_count",
        "order_count": 2,
        "fill_count": 2,
        "closed_trade_count": 1,
        "open_position_count": 0,
        "open_position_quantity": 0,
        "winning_trade_count": 1,
        "losing_trade_count": 0,
        "win_rate": 1.0,
        "profit_factor": 1.5,
        "profit_factor_status": "OK",
        "total_return": 0.05 if success else -0.02,
        "max_drawdown": 0.02,
        "sharpe": 1.1,
        "sortino": 1.3,
        "calmar": 1.4,
        "turnover": 0.9,
        "exposure": 0.25,
        "total_commission": 0.1,
        "total_slippage": 0.05,
        "spread_cost": 0.02,
        "blocked_by_trend_count": 0,
        "blocked_by_cost_count": 0,
        "blocked_by_inventory_count": 0,
        "time_stop_evaluation_count": 0,
        "time_stop_signal_count": 0,
        "time_stop_exit_count": 0,
        "time_stop_blocked_count": 0,
        "reconciliation_status": "OK" if success else "FAILED",
        "reconciliation_difference": 0.0,
        "evidence_status": "INELIGIBLE",
        "profitability_status": "INELIGIBLE",
        "deployment_status": "INELIGIBLE",
        "benchmark_status": "VALID",
    }
    if not success:
        row["reconciliation_status"] = "FAILED"
    output_dir = None

    def _factory(*args, **kwargs):
        nonlocal output_dir
        output_dir = Path(kwargs.get("output_dir"))
        run_dir = output_dir / "comparison_fake"
        run_dir.mkdir(parents=True, exist_ok=True)
        comparison_payload = {
            "run_id": "comparison_fake",
            "symbol": candidate.symbol,
            "data_start": "2026-07-10T13:30:00+00:00",
            "data_end": "2026-07-10T20:00:00+00:00",
            "comparison": [dict(row, version=v) for v in ("baseline", "a", "b", "c")],
            "summary": {
                "evidence_status": "INELIGIBLE",
                "profitability_status": "INELIGIBLE",
                "deployment_status": "INELIGIBLE",
                "reconciliation_status": "OK" if success else "FAILED",
            },
            "metrics": [dict(row, version=v) for v in ("baseline", "a", "b", "c")],
            "ranking": [{"version": "baseline", "rank": 1}],
            "parameter_candidates": [],
            "parameter_stability": {"selected_parameters": []},
            "warnings": [],
            "configuration": {"strategy_family": candidate.strategy_family},
        }
        (run_dir / "comparison_summary.json").write_text(json.dumps(comparison_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ("strategy_metrics.csv", "strategy_ranking.csv", "parameter_stability.json", "warnings.json", "configuration.json", "blocked_reason_counts.csv", "blocked_reason_by_strategy.csv", "report.md"):
            (run_dir / name).write_text("fake\n", encoding="utf-8")
        for version in ("baseline", "a", "b", "c"):
            for name in (f"trades_{version}.csv", f"orders_{version}.csv", f"equity_{version}.csv", f"drawdown_{version}.csv", f"rejected_{version}.csv"):
                (run_dir / name).write_text("fake\n", encoding="utf-8")
        return SimpleNamespace(
            run_id="comparison_fake",
            summary={
                "evidence_status": "INELIGIBLE",
                "profitability_status": "INELIGIBLE",
                "deployment_status": "INELIGIBLE",
                "reconciliation_status": "OK" if success else "FAILED",
            },
            comparison=[dict(row, version=v) for v in ("baseline", "a", "b", "c")],
            metrics=[dict(row, version=v) for v in ("baseline", "a", "b", "c")],
            ranking=[{"version": "baseline", "rank": 1}],
            parameter_stability={"selected_parameters": []},
            warnings=[],
        )

    return _factory


def test_strategy_family_registry_exposes_explicit_mappings():
    policy = runner._resolve_strategy_family_policy("equity_mean_reversion")
    assert policy.family == "equity_mean_reversion"
    assert policy.alias_of is None
    assert policy.strategy_versions == ("baseline", "a", "b", "c")
    assert policy.requires_benchmark is True

    alias_policy = runner._resolve_strategy_family_policy("mean_reversion")
    assert alias_policy.alias_of == "equity_mean_reversion"
    assert alias_policy.strategy_versions == ("baseline", "a", "b", "c")

    benchmark_policy = runner._resolve_strategy_family_policy("index_follow")
    assert benchmark_policy.alias_of == "benchmark"
    assert benchmark_policy.direction_mode == "benchmark"

    inverse_policy = runner._resolve_strategy_family_policy("inverse_etf_range")
    assert inverse_policy.alias_of == "inverse_range"
    assert inverse_policy.requires_strict_risk_profile is True


def test_run_candidate_backtest_dry_run_does_not_execute_or_mutate_state(tmp_path, monkeypatch):
    project_dir, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")

    def _should_not_run(*args, **kwargs):
        raise AssertionError("compare_versions should not run in dry-run")

    monkeypatch.setattr(runner, "compare_versions", _should_not_run)

    result = runner.run_candidate_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=backtest_root,
        dry_run=True,
        operator="ops",
        reason="smoke",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.PENDING_BACKTEST.value
    assert result["applied"] is False
    assert result["current_status"] == ValidationStatus.PENDING_BACKTEST.value
    assert result["proposed_action"] == "run_offline_backtest"
    assert result["trade_api_used"] is False
    assert result["broker_used"] is False
    assert result["trade_context_initialized"] is False
    assert result["quote_api_used"] is False
    assert result["strategy_family_policy"]["resolved_family"] == "equity_mean_reversion"
    assert result["strategy_versions"] == ["baseline", "a", "b", "c"]
    assert not (backtest_root / candidate.candidate_id).exists()


def test_run_candidate_backtest_apply_success_updates_state_and_writes_audit(tmp_path, monkeypatch):
    project_dir, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")
    monkeypatch.setattr(runner, "compare_versions", _fake_comparison_result(candidate, success=True))

    result = runner.run_candidate_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=backtest_root,
        dry_run=False,
        operator="ops",
        reason="run backtest",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.BACKTEST_COMPLETE.value
    assert refreshed.trading_enabled is False
    assert refreshed.shadow_enabled is False
    assert refreshed.paper_enabled is False
    assert refreshed.live_enabled is False
    assert result["success"] is True
    assert result["final_status"] == ValidationStatus.BACKTEST_COMPLETE.value
    assert result["candidate_current_status"] == ValidationStatus.BACKTEST_COMPLETE.value
    assert result["evidence_status"] == "INELIGIBLE"
    assert result["profitability_status"] == "INELIGIBLE"
    assert result["deployment_status"] == "INELIGIBLE"
    assert result["strategy_family_policy"]["resolved_family"] == "equity_mean_reversion"
    assert result["strategy_versions"] == ["baseline", "a", "b", "c"]
    assert (backtest_root / candidate.candidate_id / "backtest_run_audit.json").exists()
    assert (backtest_root / candidate.candidate_id / "backtest_run_summary.csv").exists()
    history = store.load_latest_history()
    assert any(item["to_status"] == ValidationStatus.BACKTEST_COMPLETE.value for item in history)
    assert all(item["to_status"] != ValidationStatus.PENDING_WALK_FORWARD.value for item in history)
    assert all(item["to_status"] != ValidationStatus.PENDING_SHADOW.value for item in history)


def test_run_candidate_backtest_apply_failure_updates_failed_state(tmp_path, monkeypatch):
    project_dir, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")
    monkeypatch.setattr(runner, "compare_versions", _fake_comparison_result(candidate, success=False))

    result = runner.run_candidate_backtest(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=backtest_root,
        dry_run=False,
        operator="ops",
        reason="run backtest",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.BACKTEST_FAILED.value
    assert result["success"] is False
    assert result["final_status"] == ValidationStatus.BACKTEST_FAILED.value
    assert result["candidate_current_status"] == ValidationStatus.BACKTEST_FAILED.value
    assert result["failure_stage"] in {"backtest_execution", "artifact_verification", "state_write"}
    assert result["exception_type"]
    assert result["partial_artifacts"]["audit_path"].endswith("backtest_run_audit.json")
    assert "reconciliation_failed" in result["failure_reason"] or "FAILED" in result["failure_reason"]
    history = store.load_latest_history()
    assert any(item["to_status"] == ValidationStatus.BACKTEST_FAILED.value for item in history)


@pytest.mark.parametrize(
    "status",
    [ValidationStatus.DATA_VALID.value, ValidationStatus.PENDING_DATA_VALIDATION.value],
)
def test_run_candidate_backtest_rejects_non_pending_candidate(tmp_path, monkeypatch, status):
    _, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = CandidateRecord.from_ai_candidate(
        symbol="AAPL.US",
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=92.1,
        ai_reason="manual backtest smoke",
        asset_type="common_stock",
        benchmarks=("QQQ.US",),
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        timeframe="15m",
        market="US",
        metadata={},
    )
    store.save_candidates([candidate])
    if status == ValidationStatus.DATA_VALID.value:
        for step in (
            ValidationStatus.CLASSIFIED,
            ValidationStatus.BENCHMARK_ASSIGNED,
            ValidationStatus.STRATEGY_ASSIGNED,
            ValidationStatus.PENDING_DATA_VALIDATION,
            ValidationStatus.DATA_VALID,
        ):
            metadata = {}
            if step == ValidationStatus.DATA_VALID:
                metadata = {
                    "benchmark_valid": True,
                    "eligible_for_backtest": True,
                    "future_data_risk": False,
                    "reconciliation_status": "OK",
                }
            store.transition(candidate.candidate_id, step, metadata=metadata)
    else:
        for step in (
            ValidationStatus.CLASSIFIED,
            ValidationStatus.BENCHMARK_ASSIGNED,
            ValidationStatus.STRATEGY_ASSIGNED,
            ValidationStatus.PENDING_DATA_VALIDATION,
        ):
            store.transition(candidate.candidate_id, step, metadata={})
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")

    with pytest.raises(CandidateTransitionError, match="candidate_must_be_pending_backtest"):
        runner.run_candidate_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=backtest_root,
            dry_run=True,
            operator="ops",
            reason="run backtest",
        )


def test_run_candidate_backtest_rejects_missing_or_invalid_inputs(tmp_path, monkeypatch):
    _, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")
    (admission_root / candidate.candidate_id / "backtest_admission_audit.json").unlink()
    with pytest.raises(CandidateTransitionError, match="backtest_admission_audit_missing"):
        runner.run_candidate_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=backtest_root,
            dry_run=True,
            operator="ops",
            reason="run backtest",
        )


def test_run_candidate_backtest_rejects_unknown_strategy_family(tmp_path, monkeypatch):
    _, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="mystery_family",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")

    with pytest.raises(CandidateTransitionError, match="unknown_strategy_family"):
        runner.run_candidate_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=backtest_root,
            dry_run=True,
            operator="ops",
            reason="run backtest",
        )


def test_run_candidate_backtest_rejects_trend_following_as_unsupported(tmp_path, monkeypatch):
    _, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="trend_following",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")

    with pytest.raises(CandidateTransitionError, match="unsupported_strategy_family:trend_following"):
        runner.run_candidate_backtest(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=backtest_root,
            dry_run=True,
            operator="ops",
            reason="run backtest",
        )


def test_run_candidate_backtest_cli_dry_run(tmp_path, monkeypatch):
    project_dir, validation_root, admission_root, backtest_root, data_root = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_backtest_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        validation_root=validation_root,
        admission_root=admission_root,
    )
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", "AAPL.US")
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", "QQQ.US")

    env = os.environ.copy()
    env["SOXS_PROJECT_DIR"] = str(project_dir)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_candidate_backtest.py"),
        "--candidate-id",
        candidate.candidate_id,
        "--candidate-store",
        str(store.root_dir),
        "--output-dir",
        str(backtest_root),
        "--operator",
        "ops",
        "--reason",
        "cli dry run",
        "--dry-run",
    ]
    completed = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["applied"] is False
    assert payload["candidate_id"] == candidate.candidate_id
    assert payload["current_status"] == ValidationStatus.PENDING_BACKTEST.value
    assert payload["proposed_action"] == "run_offline_backtest"
    assert not (backtest_root / candidate.candidate_id).exists()
