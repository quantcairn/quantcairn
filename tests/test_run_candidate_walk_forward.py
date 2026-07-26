from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.candidate_validation import CandidateRecord, CandidateTransitionError, CandidateValidationStore, ValidationStatus
from src.candidate_validation import walk_forward_execution_runner as runner


REPO_ROOT = Path(__file__).resolve().parents[1]


def _configure_runner(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    project_dir = tmp_path / "project"
    artifacts_root = project_dir / "artifacts" / "candidates"
    validation_root = artifacts_root / "validation"
    admission_root = artifacts_root / "admission"
    backtest_root = artifacts_root / "backtests"
    walk_forward_root = artifacts_root / "walk_forward"
    data_root = project_dir / "data" / "market" / "longbridge"
    monkeypatch.setattr(runner, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)
    monkeypatch.setattr(runner, "DEFAULT_BACKTEST_ROOT", backtest_root)
    monkeypatch.setattr(runner, "DEFAULT_WALK_FORWARD_ROOT", walk_forward_root)
    return {
        "project_dir": project_dir,
        "artifacts_root": artifacts_root,
        "validation_root": validation_root,
        "admission_root": admission_root,
        "backtest_root": backtest_root,
        "walk_forward_root": walk_forward_root,
        "walk_forward_admission_root": artifacts_root / "walk_forward_admission",
        "data_root": data_root,
    }


def _write_ohlcv_csv(path: Path, symbol: str, *, start_utc: str = "2026-07-10T13:30:00+00:00", bars: int = 4) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    start = datetime.fromisoformat(start_utc)
    header = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "frequency", "timezone", "source"]
    rows = [header]
    for index in range(bars):
        ts = start + timedelta(minutes=15 * index)
        open_ = 10.0 + index * 0.1
        close = open_ + 0.05
        high = close + 0.05
        low = open_ - 0.05
        rows.append(
            [
                symbol,
                ts.isoformat(),
                f"{open_:.2f}",
                f"{high:.2f}",
                f"{low:.2f}",
                f"{close:.2f}",
                1000 + index,
                "15m",
                "UTC",
                "unit_test",
            ]
        )
    path.write_text("\n".join(",".join(map(str, row)) for row in rows) + "\n", encoding="utf-8")
    return path


def _write_dataset_validation_report(root: Path, candidate: CandidateRecord, *, benchmark_status: str = "VALID", eligible_for_backtest: bool = True, future_data_risk: bool = False, duplicate_count: int = 0, invalid_ohlc_count: int = 0) -> Path:
    report_dir = root / candidate.candidate_id
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
        "validator_version": "walk_forward_smoke",
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
                "report": {
                    "symbol": candidate.symbol,
                    "benchmark_symbol": candidate.benchmarks[0] if candidate.benchmarks else "",
                    "expected_frequency": candidate.timeframe,
                    "benchmark_mapping_status": benchmark_status,
                    "frequency_match": True,
                    "overlap_ratio": 1.0,
                    "missing_bar_ratio": 0.0,
                    "duplicate_timestamp_count": duplicate_count,
                    "invalid_ohlc_count": invalid_ohlc_count,
                    "missing_value_count": 0,
                    "future_data_risk": future_data_risk,
                    "eligible_for_backtest": eligible_for_backtest,
                    "session_completeness_status": "COMPLETE",
                    "fail_closed_reason": "",
                    "data_start": "2026-07-10T13:30:00+00:00",
                    "data_end": "2026-07-10T20:15:00+00:00",
                    "benchmark_start": "2026-07-10T13:30:00+00:00",
                    "benchmark_end": "2026-07-10T20:15:00+00:00",
                },
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


def _write_backtest_admission_audit(root: Path, candidate: CandidateRecord, report_path: Path) -> Path:
    audit_dir = root / candidate.candidate_id
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
    root: Path,
    candidate: CandidateRecord,
    *,
    benchmark_status: str = "VALID",
    reconciliation_status: str = "OK",
    success: bool = True,
) -> tuple[Path, Path]:
    output_root = root / candidate.candidate_id
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
        "ranking_status": "ELIGIBLE" if benchmark_status == "VALID" and reconciliation_status == "OK" else "INSUFFICIENT_EVIDENCE",
        "strategy_versions": ["baseline", "a", "b", "c"],
    }
    (run_dir / "comparison_summary.json").write_text(json.dumps(comparison_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "strategy_metrics.csv").write_text("version,metric\nbaseline,1\n", encoding="utf-8")
    (run_dir / "strategy_ranking.csv").write_text("version,rank\nbaseline,1\n", encoding="utf-8")
    (run_dir / "parameter_stability.json").write_text(
        json.dumps({"ranking_status": comparison_summary["ranking_status"], "selection_counts": {"minimum_range_pct=0.8|maximum_range_pct=8.0|max_entry_layers=5": 1}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "warnings.json").write_text("[]\n", encoding="utf-8")
    (run_dir / "configuration.json").write_text(json.dumps({"strategy_family": candidate.strategy_family}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "blocked_reason_counts.csv").write_text("reason,count\n", encoding="utf-8")
    (run_dir / "blocked_reason_by_strategy.csv").write_text("strategy,reason,count\n", encoding="utf-8")
    (run_dir / "report.md").write_text("# walk forward smoke\n", encoding="utf-8")
    for version in ("baseline", "a", "b", "c"):
        for name in (f"trades_{version}.csv", f"orders_{version}.csv", f"equity_{version}.csv", f"drawdown_{version}.csv", f"rejected_{version}.csv"):
            (run_dir / name).write_text("row\n", encoding="utf-8")
    metrics = []
    for version in ("baseline", "a", "b", "c"):
        metrics.append(
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
        )
    audit_payload = {
        "candidate_id": candidate.candidate_id,
        "previous_status": ValidationStatus.PENDING_BACKTEST.value,
        "proposed_status": ValidationStatus.BACKTEST_COMPLETE.value,
        "final_status": ValidationStatus.BACKTEST_COMPLETE.value if success else ValidationStatus.BACKTEST_FAILED.value,
        "candidate_current_status": ValidationStatus.BACKTEST_COMPLETE.value if success else ValidationStatus.BACKTEST_FAILED.value,
        "current_status_after": ValidationStatus.BACKTEST_COMPLETE.value if success else ValidationStatus.BACKTEST_FAILED.value,
        "candidate_status_after": ValidationStatus.BACKTEST_COMPLETE.value if success else ValidationStatus.BACKTEST_FAILED.value,
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
        "success": success,
        "failure_reason": "" if success else "reconciliation_failed",
        "backtest_engine_version": "manual_candidate_backtest_v1",
        "trade_api_used": False,
        "broker_used": False,
        "trade_context_initialized": False,
    }
    audit_path = output_root / "backtest_run_audit.json"
    audit_path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "backtest_run_summary.csv").write_text(
        "candidate_id,previous_status,final_status,operator,reason,started_at,completed_at,symbol,benchmarks,timeframe,strategy_family\n"
        f"{candidate.candidate_id},PENDING_BACKTEST,{audit_payload['final_status']},smoke_test,manual backtest smoke,2026-07-10T21:03:00+00:00,2026-07-10T21:03:01+00:00,{candidate.symbol},\"{'|'.join(candidate.benchmarks)}\",{candidate.timeframe},{candidate.strategy_family}\n",
        encoding="utf-8",
    )
    return audit_path, run_dir


def _write_walk_forward_admission_audit(root: Path, candidate: CandidateRecord, backtest_audit_path: Path) -> Path:
    audit_dir = root / candidate.candidate_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "walk_forward_admission_audit.json"
    payload = {
        "candidate_id": candidate.candidate_id,
        "candidate_current_status": ValidationStatus.PENDING_WALK_FORWARD.value,
        "previous_status": ValidationStatus.BACKTEST_COMPLETE.value,
        "proposed_status": ValidationStatus.PENDING_WALK_FORWARD.value,
        "final_status": ValidationStatus.PENDING_WALK_FORWARD.value,
        "applied": True,
        "operator": "smoke_test",
        "reason": "manual walk-forward admission",
        "started_at": "2026-07-10T22:00:00+00:00",
        "completed_at": "2026-07-10T22:00:01+00:00",
        "backtest_audit_path": str(backtest_audit_path),
    }
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit_path


def _make_candidate_ready_for_walk_forward(
    store: CandidateValidationStore,
    roots: dict[str, Path],
    *,
    symbol: str = "SOXS.US",
    benchmarks: tuple[str, ...] = ("SOXX.US", "SMH.US"),
    asset_type: str = "inverse_etf",
    strategy_family: str = "inverse_etf_range",
    risk_profile: str = "strict",
    benchmark_status: str = "VALID",
    eligible_for_backtest: bool = True,
    future_data_risk: bool = False,
    duplicate_count: int = 0,
    invalid_ohlc_count: int = 0,
    reconciliation_status: str = "OK",
    backtest_success: bool = True,
) -> CandidateRecord:
    candidate = CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=94.2,
        ai_reason="walk forward smoke",
        asset_type=asset_type,
        benchmarks=benchmarks,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe="15m",
        market="US",
        metadata={
            "dataset_validation_report_path": str(Path("artifacts/candidates/validation") / "placeholder" / "dataset_validation_report.json"),
            "dataset_validation_summary_path": str(Path("artifacts/candidates/validation") / "placeholder" / "dataset_validation_summary.csv"),
        },
    )
    candidate.metadata.update(
        {
            "dataset_validation_report_path": str(Path("artifacts/candidates/validation") / candidate.candidate_id / "dataset_validation_report.json"),
            "dataset_validation_summary_path": str(Path("artifacts/candidates/validation") / candidate.candidate_id / "dataset_validation_summary.csv"),
            "backtest_admission_audit_path": str(Path("artifacts/candidates/admission") / candidate.candidate_id / "backtest_admission_audit.json"),
            "backtest_run_audit_path": str(Path("artifacts/candidates/backtests") / candidate.candidate_id / "backtest_run_audit.json"),
            "walk_forward_admission_audit_path": str(Path("artifacts/candidates/walk_forward_admission") / candidate.candidate_id / "walk_forward_admission_audit.json"),
        }
    )
    store.save_candidates([candidate])
    report_path = _write_dataset_validation_report(
        roots["validation_root"],
        candidate,
        benchmark_status=benchmark_status,
        eligible_for_backtest=eligible_for_backtest,
        future_data_risk=future_data_risk,
        duplicate_count=duplicate_count,
        invalid_ohlc_count=invalid_ohlc_count,
    )
    admission_audit = _write_backtest_admission_audit(roots["admission_root"], candidate, report_path)
    backtest_audit_path, _ = _write_backtest_artifacts(
        roots["backtest_root"],
        candidate,
        benchmark_status=benchmark_status,
        reconciliation_status=reconciliation_status,
        success=backtest_success,
    )
    walk_forward_audit_path = _write_walk_forward_admission_audit(roots["walk_forward_admission_root"], candidate, backtest_audit_path)
    candidate.metadata.update(
        {
            "backtest_admission_audit_path": str(admission_audit),
            "backtest_run_audit_path": str(backtest_audit_path),
            "walk_forward_admission_audit_path": str(walk_forward_audit_path),
        }
    )
    for status in (
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
    ):
        store.transition(
            candidate.candidate_id,
            status,
            metadata={
                "benchmark_valid": benchmark_status == "VALID",
                "eligible_for_backtest": eligible_for_backtest,
                "future_data_risk": future_data_risk,
                "reconciliation_status": reconciliation_status,
                "dataset_validation_report_path": str(report_path),
                "dataset_validation_summary_path": str(report_path.with_name("dataset_validation_summary.csv")),
                "backtest_admission_audit_path": str(admission_audit),
            },
        )
    store.transition(
        candidate.candidate_id,
        ValidationStatus.DATA_VALID,
        metadata={
            "benchmark_valid": benchmark_status == "VALID",
            "eligible_for_backtest": eligible_for_backtest,
            "future_data_risk": future_data_risk,
            "reconciliation_status": reconciliation_status,
            "dataset_validation_report_path": str(report_path),
            "dataset_validation_summary_path": str(report_path.with_name("dataset_validation_summary.csv")),
            "backtest_admission_audit_path": str(admission_audit),
        },
    )
    store.transition(
        candidate.candidate_id,
        ValidationStatus.PENDING_BACKTEST,
        metadata={
            "data_status": ValidationStatus.DATA_VALID.value,
            "dataset_validation_report_path": str(report_path),
            "dataset_validation_summary_path": str(report_path.with_name("dataset_validation_summary.csv")),
            "backtest_admission_audit_path": str(admission_audit),
        },
    )
    store.transition(
        candidate.candidate_id,
        ValidationStatus.BACKTEST_COMPLETE,
        metadata={
            "backtest_complete": True,
            "backtest_audit_path": str(backtest_audit_path),
            "reconciliation_status": reconciliation_status,
            "benchmark_valid": benchmark_status == "VALID",
        },
    )
    store.transition(
        candidate.candidate_id,
        ValidationStatus.PENDING_WALK_FORWARD,
        metadata={
            "backtest_status": ValidationStatus.BACKTEST_COMPLETE.value,
            "backtest_audit_path": str(backtest_audit_path),
            "walk_forward_admission_audit_path": str(walk_forward_audit_path),
            "backtest_run_audit_path": str(backtest_audit_path),
        },
    )
    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    return refreshed


class FakeWalkForwardEvaluator:
    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[dict[str, object]] = []

    def evaluate(self, bars, *, symbol=None, strategy="a", parameter_grid=None, benchmark_bars=None, initial_cash=None, output_dir=None, max_candidates=None):
        self.calls.append(
            {
                "symbol": symbol,
                "strategy": strategy,
                "parameter_grid": list(parameter_grid or []),
                "initial_cash": initial_cash,
                "max_candidates": max_candidates,
                "bars": len(list(bars or [])),
                "benchmark_bars": len(list(benchmark_bars or [])),
            }
        )
        windows = []
        for index, params in enumerate((parameter_grid or [])[:2], start=1):
            windows.append(
                SimpleNamespace(
                    train_range={"start": "2026-06-01T13:30:00+00:00", "end": "2026-06-30T20:00:00+00:00"},
                    validation_range={"start": "2026-07-01T13:30:00+00:00", "end": "2026-07-10T20:00:00+00:00"},
                    test_range={"start": "2026-07-11T13:30:00+00:00", "end": "2026-07-20T20:00:00+00:00"},
                    selected_parameters=dict(params),
                    validation_score=1.0 + index,
                    trade_count=1,
                    warnings=[],
                    rejected_signals=[],
                    test_metrics={
                        "total_return": 0.02 * index,
                        "max_drawdown": 0.01,
                        "sharpe": 1.5,
                        "sortino": 1.8,
                        "calmar": 1.4,
                        "reconciliation_status": "OK",
                        "benchmark_status": "VALID",
                    },
                )
            )
        return SimpleNamespace(
            windows=windows,
            stitched_oos_equity=[
                {"timestamp": "2026-07-11T13:30:00+00:00", "equity": 100.0},
                {"timestamp": "2026-07-11T13:45:00+00:00", "equity": 101.0},
            ],
            aggregate_oos_metrics={
                "max_drawdown_max": 0.01,
                "sharpe_mean": 1.5,
                "sortino_mean": 1.8,
                "calmar_mean": 1.4,
                "profit_factor_mean": 1.2,
                "turnover_mean": 0.5,
                "total_return_mean": 0.03,
            },
            parameter_stability={
                "ranking_status": "ELIGIBLE",
                "instability_score": 0.25,
                "selection_counts": {
                    "minimum_range_pct=0.8|maximum_range_pct=8.0|max_entry_layers=5": 1,
                    "minimum_range_pct=1.0|maximum_range_pct=12.0|max_entry_layers=3": 1,
                },
                "regime_dependent": False,
                "isolated_optimum": False,
                "too_few_trades": False,
                "high_turnover": False,
                "drawdown_sensitive": False,
                "benchmark_validation": {"status": "VALID"},
                "evidence_status": "ELIGIBLE",
                "profitability_status": "ELIGIBLE",
                "deployment_status": "INELIGIBLE",
            },
            warnings=[],
        )


def test_run_candidate_walk_forward_dry_run_keeps_state_and_writes_nothing(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    _write_ohlcv_csv(roots["data_root"] / "SOXS_US_15m.csv", "SOXS.US")
    _write_ohlcv_csv(roots["data_root"] / "SOXX_US_15m.csv", "SOXX.US")
    _write_ohlcv_csv(roots["data_root"] / "SMH_US_15m.csv", "SMH.US")

    result = runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=roots["walk_forward_root"],
        dry_run=True,
        operator="smoke_test",
        reason="dry run",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.PENDING_WALK_FORWARD.value
    assert result["candidate_current_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
    assert result["proposed_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
    assert result["applied"] is False
    assert result["walk_forward_started"] is False
    assert result["all_trading_flags_false"] is True
    assert not (roots["walk_forward_root"] / candidate.candidate_id).exists()
    history = store.load_latest_history()
    assert sum(1 for item in history if item["to_status"] == ValidationStatus.PENDING_WALK_FORWARD.value) == 1


def test_run_candidate_walk_forward_apply_completes_and_writes_artifacts(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    _write_ohlcv_csv(roots["data_root"] / "SOXS_US_15m.csv", "SOXS.US")
    _write_ohlcv_csv(roots["data_root"] / "SOXX_US_15m.csv", "SOXX.US")
    _write_ohlcv_csv(roots["data_root"] / "SMH_US_15m.csv", "SMH.US")
    fake = FakeWalkForwardEvaluator(None)
    monkeypatch.setattr(runner, "WalkForwardEvaluator", lambda config: fake)

    result = runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=roots["walk_forward_root"],
        dry_run=False,
        operator="smoke_test",
        reason="apply walk forward",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.WALK_FORWARD_COMPLETE.value
    assert refreshed.trading_enabled is False
    assert refreshed.shadow_enabled is False
    assert refreshed.paper_enabled is False
    assert refreshed.live_enabled is False
    assert result["success"] is True
    assert result["applied"] is True
    assert result["candidate_current_status"] == ValidationStatus.WALK_FORWARD_COMPLETE.value
    assert result["final_status"] == ValidationStatus.WALK_FORWARD_COMPLETE.value
    assert result["benchmark_status"] == "VALID"
    assert result["reconciliation_status"] == "OK"
    assert result["all_trading_flags_false"] is True
    run_dir = roots["walk_forward_root"] / candidate.candidate_id
    assert (run_dir / "walk_forward_run_audit.json").exists()
    assert (run_dir / "walk_forward_run_summary.csv").exists()
    assert (run_dir / "walk_forward_metrics.json").exists()
    assert (run_dir / "window_results.csv").exists()
    assert (run_dir / "selected_parameters.csv").exists()
    assert (run_dir / "oos_equity.csv").exists()
    assert (run_dir / "oos_drawdown.csv").exists()
    assert (run_dir / "window_failures.csv").exists()
    assert (run_dir / "parameter_stability.json").exists()
    assert (run_dir / "report.md").exists()
    history = store.load_latest_history()
    assert any(item["to_status"] == ValidationStatus.WALK_FORWARD_COMPLETE.value for item in history)
    assert fake.calls, "expected fake evaluator to be invoked"
    payload = json.loads((run_dir / "walk_forward_run_audit.json").read_text(encoding="utf-8"))
    assert payload["candidate_id"] == candidate.candidate_id
    assert payload["symbol"] == "SOXS.US"
    assert payload["trade_api_used"] is False
    assert payload["broker_used"] is False
    assert payload["trade_context_initialized"] is False


def test_run_candidate_walk_forward_rejects_repeat_apply_after_completion(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    _write_ohlcv_csv(roots["data_root"] / "SOXS_US_15m.csv", "SOXS.US")
    _write_ohlcv_csv(roots["data_root"] / "SOXX_US_15m.csv", "SOXX.US")
    _write_ohlcv_csv(roots["data_root"] / "SMH_US_15m.csv", "SMH.US")
    monkeypatch.setattr(runner, "WalkForwardEvaluator", lambda config: FakeWalkForwardEvaluator(config))

    runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=roots["walk_forward_root"],
        dry_run=False,
        operator="smoke_test",
        reason="apply walk forward",
    )

    before_history = len(store.load_latest_history())
    with pytest.raises(CandidateTransitionError, match="candidate_must_be_pending_walk_forward"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=roots["walk_forward_root"],
            dry_run=False,
            operator="smoke_test",
            reason="repeat apply",
        )
    assert len(store.load_latest_history()) == before_history


@pytest.mark.parametrize(
    "strategy_family, expected_error",
    [
        ("trend_following", "unsupported_strategy_family:trend_following"),
        ("unknown_family", "unknown_strategy_family:unknown_family"),
    ],
)
def test_run_candidate_walk_forward_rejects_unsupported_strategy_family(tmp_path, monkeypatch, strategy_family, expected_error):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(
        store,
        roots,
        strategy_family=strategy_family,
        asset_type="common_stock",
        benchmarks=("QQQ.US",),
        risk_profile="balanced",
    )
    with pytest.raises(CandidateTransitionError, match=expected_error):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=roots["walk_forward_root"],
            dry_run=True,
            operator="smoke_test",
            reason="unsupported family",
        )


@pytest.mark.parametrize(
    "status, expected_error",
    [
        (ValidationStatus.PENDING_BACKTEST, "candidate_must_be_pending_walk_forward"),
        (ValidationStatus.BACKTEST_FAILED, "candidate_must_be_pending_walk_forward"),
        (ValidationStatus.DATA_VALID, "candidate_must_be_pending_walk_forward"),
    ],
)
def test_run_candidate_walk_forward_rejects_wrong_status(tmp_path, monkeypatch, status, expected_error):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    store.transition(candidate.candidate_id, ValidationStatus.REJECTED, reason="reset", metadata={"reason": "reset"})
    candidate = store.get_candidate(candidate.candidate_id)
    assert candidate is not None
    if status == ValidationStatus.PENDING_BACKTEST:
        candidate = CandidateRecord.from_ai_candidate(
            symbol="SOXS.US",
            selected_at="2026-07-10T21:32:41+08:00",
            source="ai_selector",
            ai_score=94.2,
            ai_reason="walk forward smoke",
            asset_type="inverse_etf",
            benchmarks=("SOXX.US", "SMH.US"),
            strategy_family="inverse_etf_range",
            risk_profile="strict",
            timeframe="15m",
            market="US",
            metadata={},
        )
        store = CandidateValidationStore(roots["artifacts_root"])
        store.save_candidates([candidate])
        store.transition(candidate.candidate_id, ValidationStatus.CLASSIFIED, metadata={"asset_type": "inverse_etf"})
    with pytest.raises(CandidateTransitionError, match=expected_error):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=roots["walk_forward_root"],
            dry_run=True,
            operator="smoke_test",
            reason="wrong status",
        )


def test_run_candidate_walk_forward_rejects_output_dir_escape(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)

    with pytest.raises(CandidateTransitionError, match="unsafe_path_parameter:output_dir"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=tmp_path / ".." / "escape",
            dry_run=True,
            operator="smoke_test",
            reason="escape",
        )


def test_run_candidate_walk_forward_rejects_backtest_audit_mismatch(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    audit_path = roots["backtest_root"] / candidate.candidate_id / "backtest_run_audit.json"
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["symbol"] = "AAPL.US"
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(CandidateTransitionError, match="backtest_audit_symbol_mismatch"):
        runner.admit_candidate_to_walk_forward(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=roots["walk_forward_root"],
            dry_run=True,
            operator="smoke_test",
            reason="mismatch",
        )


def test_run_candidate_walk_forward_apply_failure_enters_failed_state(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    _write_ohlcv_csv(roots["data_root"] / "SOXS_US_15m.csv", "SOXS.US")
    _write_ohlcv_csv(roots["data_root"] / "SOXX_US_15m.csv", "SOXX.US")
    _write_ohlcv_csv(roots["data_root"] / "SMH_US_15m.csv", "SMH.US")

    class FailingEvaluator:
        def __init__(self, config) -> None:
            self.config = config

        def evaluate(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "WalkForwardEvaluator", FailingEvaluator)

    result = runner.admit_candidate_to_walk_forward(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=roots["walk_forward_root"],
        dry_run=False,
        operator="smoke_test",
        reason="failing run",
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.WALK_FORWARD_FAILED.value
    assert result["success"] is False
    assert result["candidate_current_status"] == ValidationStatus.WALK_FORWARD_FAILED.value
    assert result["failure_stage"] in {"execution", "artifact_verification"}
    assert result["failure_reason"]


def test_run_candidate_walk_forward_cli_dry_run_and_help(tmp_path, monkeypatch):
    roots = _configure_runner(monkeypatch, tmp_path)
    store = CandidateValidationStore(roots["artifacts_root"])
    candidate = _make_candidate_ready_for_walk_forward(store, roots)
    _write_ohlcv_csv(roots["data_root"] / "SOXS_US_15m.csv", "SOXS.US", bars=120)
    _write_ohlcv_csv(roots["data_root"] / "SOXX_US_15m.csv", "SOXX.US", bars=120)
    _write_ohlcv_csv(roots["data_root"] / "SMH_US_15m.csv", "SMH.US", bars=120)

    script = REPO_ROOT / "scripts" / "run_candidate_walk_forward.py"
    env = dict(os.environ)
    env["SOXS_PROJECT_DIR"] = str(roots["project_dir"])
    dry = subprocess.run(
        [
            sys.executable,
            str(script),
            "--candidate-id",
            candidate.candidate_id,
            "--candidate-store",
            str(store.root_dir),
            "--output-dir",
            str(roots["walk_forward_root"]),
            "--operator",
            "smoke_test",
            "--reason",
            "cli dry run",
            "--dry-run",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["applied"] is False
    assert dry_payload["candidate_current_status"] == ValidationStatus.PENDING_WALK_FORWARD.value
    help_proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0
    assert "--candidate-id" in help_proc.stdout
    assert "--output-dir" in help_proc.stdout
