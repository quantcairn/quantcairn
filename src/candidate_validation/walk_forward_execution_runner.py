from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from src.backtest import BacktestDataFeed, WalkForwardConfig, WalkForwardEvaluator
from src.backtest.benchmarking import validate_benchmark_alignment

from .backtest_execution_runner import _find_local_data_file, _load_json, _resolve_candidate_store_root
from .models import CandidateRecord, CandidateTransitionError, ValidationStatus
from .store import CandidateValidationStore


PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
DEFAULT_BACKTEST_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "backtests"
DEFAULT_WALK_FORWARD_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "walk_forward"
DEFAULT_DATA_ROOT = PROJECT_DIR / "data" / "market" / "longbridge"
WALK_FORWARD_ENGINE_VERSION = "manual_candidate_walk_forward_v1"
WALK_FORWARD_PARAMETER_REGISTRY_VERSION = "manual_walk_forward_parameter_registry_v1"
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True, slots=True)
class WalkForwardFamilyPolicy:
    requested_family: str
    resolved_family: str
    alias_of: str | None
    supported: bool
    requires_benchmark: bool
    requires_strict_risk_profile: bool
    direction_mode: str
    parameter_grid: tuple[dict[str, Any], ...]
    walk_forward_config: WalkForwardConfig
    notes: str = ""


_DEFAULT_WF_CONFIG = WalkForwardConfig(
    train_size=30,
    validation_size=15,
    test_size=15,
    step_size=15,
    purge_gap=2,
    embargo_gap=2,
    random_seed=42,
)

_DEFAULT_PARAMETER_GRID: tuple[dict[str, Any], ...] = (
    {"minimum_range_pct": 1.0, "maximum_range_pct": 12.0, "max_entry_layers": 3},
    {"minimum_range_pct": 0.8, "maximum_range_pct": 8.0, "max_entry_layers": 5},
)

_WALK_FORWARD_FAMILY_REGISTRY: dict[str, WalkForwardFamilyPolicy] = {
    "range_etf": WalkForwardFamilyPolicy(
        requested_family="range_etf",
        resolved_family="range_etf",
        alias_of=None,
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=False,
        direction_mode="range",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Baseline range configuration for ETF candidates.",
    ),
    "inverse_range": WalkForwardFamilyPolicy(
        requested_family="inverse_range",
        resolved_family="inverse_range",
        alias_of=None,
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=True,
        direction_mode="inverse_range",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Inverse ETF range configuration with strict risk profile.",
    ),
    "inverse_etf_range": WalkForwardFamilyPolicy(
        requested_family="inverse_etf_range",
        resolved_family="inverse_range",
        alias_of="inverse_range",
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=True,
        direction_mode="inverse_range",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Explicit alias for inverse_range; no separate engine implementation.",
    ),
    "leveraged_range": WalkForwardFamilyPolicy(
        requested_family="leveraged_range",
        resolved_family="leveraged_range",
        alias_of=None,
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=True,
        direction_mode="leveraged_range",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Leveraged ETF range configuration with strict risk profile.",
    ),
    "equity_mean_reversion": WalkForwardFamilyPolicy(
        requested_family="equity_mean_reversion",
        resolved_family="equity_mean_reversion",
        alias_of=None,
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=False,
        direction_mode="equity_mean_reversion",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Equity mean reversion configuration.",
    ),
    "mean_reversion": WalkForwardFamilyPolicy(
        requested_family="mean_reversion",
        resolved_family="equity_mean_reversion",
        alias_of="equity_mean_reversion",
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=False,
        direction_mode="equity_mean_reversion",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Explicit alias for equity_mean_reversion.",
    ),
    "benchmark": WalkForwardFamilyPolicy(
        requested_family="benchmark",
        resolved_family="benchmark",
        alias_of=None,
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=False,
        direction_mode="benchmark",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Benchmark-following configuration.",
    ),
    "index_follow": WalkForwardFamilyPolicy(
        requested_family="index_follow",
        resolved_family="benchmark",
        alias_of="benchmark",
        supported=True,
        requires_benchmark=True,
        requires_strict_risk_profile=False,
        direction_mode="benchmark",
        parameter_grid=_DEFAULT_PARAMETER_GRID,
        walk_forward_config=_DEFAULT_WF_CONFIG,
        notes="Explicit alias for benchmark-following configuration.",
    ),
}

_UNSUPPORTED_STRATEGY_FAMILIES = {
    "trend_following": "unsupported_strategy_family:trend_following",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_component(value: str) -> str:
    text = str(value or "").strip()
    if not text or not _SAFE_COMPONENT_RE.fullmatch(text):
        raise CandidateTransitionError(f"unsafe_path_component:{text!r}")
    return text


def _ensure_no_parent_refs(path: str | Path, *, label: str) -> None:
    raw = Path(path)
    if any(part == ".." for part in raw.parts):
        raise CandidateTransitionError(f"unsafe_path_parameter:{label}")


def _atomic_write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


def _atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        try:
            return _jsonable(value.value)
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _candidate_flags_false(candidate: CandidateRecord) -> bool:
    return not any(
        bool(flag)
        for flag in (
            candidate.trading_enabled,
            candidate.shadow_enabled,
            candidate.paper_enabled,
            candidate.live_enabled,
        )
    )


def _resolve_candidate_admission_path(candidate: CandidateRecord, store: CandidateValidationStore) -> Path:
    candidate_id = _safe_component(candidate.candidate_id)
    metadata = dict(candidate.metadata or {})
    candidates: list[Path] = []
    for key in ("walk_forward_admission_audit_path", "walk_forward_admission_path", "walk_forward_source_audit_path"):
        raw = metadata.get(key)
        if raw:
            path = Path(str(raw))
            if path.is_absolute():
                candidates.append(path)
            else:
                candidates.extend(
                    [
                        path,
                        PROJECT_DIR / path,
                        DEFAULT_WALK_FORWARD_ROOT / path,
                        DEFAULT_WALK_FORWARD_ROOT / candidate_id / path.name,
                        store.root_dir.parent / path,
                        store.root_dir.parent / "walk_forward_admission" / path,
                        store.root_dir.parent / "walk_forward_admission" / candidate_id / path.name,
                    ]
                )
    candidates.extend(
        [
            DEFAULT_WALK_FORWARD_ROOT / candidate_id / "walk_forward_admission_audit.json",
            PROJECT_DIR / "artifacts" / "candidates" / "walk_forward_admission" / candidate_id / "walk_forward_admission_audit.json",
            store.root_dir.parent / "walk_forward_admission" / candidate_id / "walk_forward_admission_audit.json",
        ]
    )
    for candidate_path in candidates:
        if candidate_path.exists() and candidate_path.is_file():
            return candidate_path
    raise CandidateTransitionError("walk_forward_admission_audit_missing")


def _resolve_backtest_audit_path(candidate: CandidateRecord, store: CandidateValidationStore) -> Path:
    candidate_id = _safe_component(candidate.candidate_id)
    metadata = dict(candidate.metadata or {})
    candidates: list[Path] = []
    for key in ("backtest_run_audit_path", "backtest_audit_path", "backtest_run_path", "backtest_summary_path"):
        raw = metadata.get(key)
        if raw:
            path = Path(str(raw))
            if path.is_absolute():
                candidates.append(path)
            else:
                candidates.extend(
                    [
                        path,
                        PROJECT_DIR / path,
                        DEFAULT_BACKTEST_ROOT / path,
                        DEFAULT_BACKTEST_ROOT / candidate_id / path.name,
                        store.root_dir.parent / path,
                        store.root_dir.parent / "backtests" / path,
                        store.root_dir.parent / "backtests" / candidate_id / path.name,
                    ]
                )
    candidates.extend(
        [
            DEFAULT_BACKTEST_ROOT / candidate_id / "backtest_run_audit.json",
            DEFAULT_BACKTEST_ROOT / candidate_id / "backtest_audit.json",
            PROJECT_DIR / "artifacts" / "candidates" / "backtests" / candidate_id / "backtest_run_audit.json",
            store.root_dir.parent / "backtests" / candidate_id / "backtest_run_audit.json",
        ]
    )
    for candidate_path in candidates:
        if candidate_path.exists() and candidate_path.is_file():
            return candidate_path
    raise CandidateTransitionError("backtest_audit_missing")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CandidateTransitionError(f"audit_invalid:{path.name}:{exc}") from exc
    if not isinstance(payload, dict):
        raise CandidateTransitionError(f"audit_invalid:{path.name}:root_must_be_object")
    return payload


def _load_csv_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as exc:
        raise CandidateTransitionError(f"csv_invalid:{path.name}:{exc}") from exc


def _strategy_family_policy(strategy_family: str) -> WalkForwardFamilyPolicy:
    family = str(strategy_family or "").strip().lower()
    if family in _UNSUPPORTED_STRATEGY_FAMILIES:
        raise CandidateTransitionError(_UNSUPPORTED_STRATEGY_FAMILIES[family])
    policy = _WALK_FORWARD_FAMILY_REGISTRY.get(family)
    if policy is None or not policy.supported:
        raise CandidateTransitionError(f"unknown_strategy_family:{family}")
    return policy


def _read_backtest_evidence(
    candidate: CandidateRecord,
    store: CandidateValidationStore,
) -> tuple[dict[str, Any], Path, Path, dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str], str]:
    backtest_audit_path = _resolve_backtest_audit_path(candidate, store)
    audit_payload = _load_json(backtest_audit_path)
    if str(audit_payload.get("candidate_id") or "").strip() != candidate.candidate_id:
        raise CandidateTransitionError("backtest_audit_candidate_mismatch")
    if str(audit_payload.get("symbol") or "").strip().upper() != candidate.symbol:
        raise CandidateTransitionError("backtest_audit_symbol_mismatch")
    if tuple(str(item).strip().upper() for item in audit_payload.get("benchmarks") or ()) != candidate.benchmarks:
        raise CandidateTransitionError("backtest_audit_benchmark_mismatch")
    if str(audit_payload.get("timeframe") or "").strip().lower() != str(candidate.timeframe or "").strip().lower():
        raise CandidateTransitionError("backtest_audit_timeframe_mismatch")
    if str(audit_payload.get("strategy_family") or "").strip().lower() != str(candidate.strategy_family or "").strip().lower():
        raise CandidateTransitionError("backtest_audit_strategy_family_mismatch")
    if str(audit_payload.get("applied") if "applied" in audit_payload else True).lower() not in {"true", "1"}:
        raise CandidateTransitionError("backtest_audit_not_applied")
    if str(audit_payload.get("success") if "success" in audit_payload else True).lower() not in {"true", "1"}:
        raise CandidateTransitionError("backtest_audit_not_successful")
    if str(audit_payload.get("final_status") or "").strip().upper() != ValidationStatus.BACKTEST_COMPLETE.value:
        raise CandidateTransitionError("backtest_audit_not_complete")
    if str(audit_payload.get("candidate_current_status") or "").strip().upper() != ValidationStatus.BACKTEST_COMPLETE.value:
        raise CandidateTransitionError("backtest_audit_not_complete")
    if str(audit_payload.get("trade_api_used") if "trade_api_used" in audit_payload else False).lower() not in {"false", "0"}:
        raise CandidateTransitionError("trade_api_used")
    if str(audit_payload.get("broker_used") if "broker_used" in audit_payload else False).lower() not in {"false", "0"}:
        raise CandidateTransitionError("broker_used")
    if str(audit_payload.get("trade_context_initialized") if "trade_context_initialized" in audit_payload else False).lower() not in {"false", "0"}:
        raise CandidateTransitionError("trade_context_initialized")

    comparison_summary = audit_payload.get("comparison_summary")
    if not isinstance(comparison_summary, dict):
        raise CandidateTransitionError("comparison_summary_missing")
    if str(comparison_summary.get("benchmark_status") or "").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if str(comparison_summary.get("reconciliation_status") or "").strip().upper() != "OK":
        raise CandidateTransitionError("reconciliation_must_be_ok")

    backtest_metrics = audit_payload.get("backtest_metrics")
    if not isinstance(backtest_metrics, list) or not backtest_metrics:
        raise CandidateTransitionError("backtest_metrics_missing")
    versions = []
    for row in backtest_metrics:
        if not isinstance(row, dict):
            raise CandidateTransitionError("backtest_metrics_invalid")
        version = str(row.get("version") or "").strip().lower()
        if version:
            versions.append(version)
    if not all(item in set(versions) for item in ("baseline", "a", "b", "c")):
        raise CandidateTransitionError("backtest_versions_incomplete")

    backtest_run_dir_raw = audit_payload.get("backtest_run_dir") or audit_payload.get("backtest_run_path") or audit_payload.get("output_dir")
    if not backtest_run_dir_raw:
        raise CandidateTransitionError("backtest_run_dir_missing")
    backtest_run_dir = Path(str(backtest_run_dir_raw))
    if backtest_run_dir.is_file():
        backtest_run_dir = backtest_run_dir.parent
    if not backtest_run_dir.exists():
        raise CandidateTransitionError("backtest_run_dir_missing")

    required_files = [
        "comparison_summary.json",
        "strategy_metrics.csv",
        "strategy_ranking.csv",
        "parameter_stability.json",
        "report.md",
        "warnings.json",
        "configuration.json",
        "blocked_reason_counts.csv",
        "blocked_reason_by_strategy.csv",
    ]
    missing = [name for name in required_files if not (backtest_run_dir / name).exists()]
    for version in ("baseline", "a", "b", "c"):
        for name in (f"trades_{version}.csv", f"orders_{version}.csv", f"equity_{version}.csv", f"drawdown_{version}.csv", f"rejected_{version}.csv"):
            if not (backtest_run_dir / name).exists():
                missing.append(name)
    if missing:
        raise CandidateTransitionError(f"backtest_artifacts_missing:{','.join(missing)}")

    comparison_summary_path = backtest_run_dir / "comparison_summary.json"
    comparison_summary_file = _load_json(comparison_summary_path)
    if str(comparison_summary_file.get("benchmark_status") or "").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if str(comparison_summary_file.get("reconciliation_status") or "").strip().upper() != "OK":
        raise CandidateTransitionError("reconciliation_must_be_ok")

    strategy_metrics_path = backtest_run_dir / "strategy_metrics.csv"
    strategy_metrics = _load_csv_rows(strategy_metrics_path)
    if not strategy_metrics:
        raise CandidateTransitionError("strategy_metrics_missing")
    strategy_ranking_path = backtest_run_dir / "strategy_ranking.csv"
    strategy_ranking = _load_csv_rows(strategy_ranking_path)
    if not strategy_ranking:
        raise CandidateTransitionError("strategy_ranking_missing")

    candidate_metadata = dict(candidate.metadata or {})
    admission_audit_path = candidate_metadata.get("walk_forward_admission_audit_path") or candidate_metadata.get("admission_audit_path")
    if not admission_audit_path:
        raise CandidateTransitionError("walk_forward_admission_audit_missing")
    admission_path = Path(str(admission_audit_path))
    if not admission_path.exists():
        raise CandidateTransitionError("walk_forward_admission_audit_missing")
    admission_payload = _load_json(admission_path)
    if str(admission_payload.get("applied") if "applied" in admission_payload else True).lower() not in {"true", "1"}:
        raise CandidateTransitionError("walk_forward_admission_not_applied")
    if str(admission_payload.get("previous_status") or "").strip().upper() != ValidationStatus.BACKTEST_COMPLETE.value:
        raise CandidateTransitionError("walk_forward_admission_previous_status_mismatch")
    if str(admission_payload.get("proposed_status") or "").strip().upper() != ValidationStatus.PENDING_WALK_FORWARD.value:
        raise CandidateTransitionError("walk_forward_admission_status_mismatch")

    return (
        audit_payload,
        backtest_audit_path,
        backtest_run_dir,
        comparison_summary_file,
        comparison_summary,
        backtest_metrics,
        strategy_metrics,
        strategy_ranking,
        versions,
    )


def _build_candidate_result(candidate: CandidateRecord, *, backtest_audit_path: Path, backtest_run_dir: Path, comparison_summary: dict[str, Any], backtest_metrics: list[dict[str, Any]], strategy_metrics: list[dict[str, Any]], strategy_ranking: list[dict[str, Any]], versions: list[str], parameter_registry_version: str, walk_forward_engine_version: str, random_seed: int, training_window: dict[str, int], output_root: Path, strategy_version: str, parameter_grid: list[dict[str, Any]], walk_forward_config: WalkForwardConfig, data_paths: dict[str, str], benchmark_status: str, evidence_status: str, profitability_status: str, deployment_status: str, result: Any, current_status: str, dry_run: bool, operator: str, reason: str, started_at: str, completed_at: str, current_output_dir: Path | None = None, failure_stage: str = "", failure_reason: str = "", success: bool = False) -> dict[str, Any]:
    completed_versions = sorted({str(row.get("version") or "").strip().lower() for row in backtest_metrics if str(row.get("version") or "").strip()})
    oos_equity_rows = []
    stitched_equity = list(getattr(result, "stitched_oos_equity", []) or [])
    running_peak = None
    for row in stitched_equity:
        equity = float(row.get("equity") or 0.0)
        if running_peak is None or equity > running_peak:
            running_peak = equity
        drawdown = max(0.0, (running_peak or equity) - equity)
        oos_equity_rows.append({**row, "drawdown": round(drawdown, 6), "peak_equity": round(running_peak or equity, 6)})
    aggregate = dict(getattr(result, "aggregate_oos_metrics", {}) or {})
    total_return_values = [float(row.get("total_return") or 0.0) for row in backtest_metrics]
    active_windows = [window for window in getattr(result, "windows", []) if int(getattr(window, "trade_count", 0) or 0) > 0]
    no_trade_windows = [window for window in getattr(result, "windows", []) if int(getattr(window, "trade_count", 0) or 0) == 0]
    positive_windows = [window for window in getattr(result, "windows", []) if float((getattr(window, "test_metrics", {}) or {}).get("total_return") or 0.0) > 0]
    selected_parameter_frequency: dict[str, int] = {}
    window_results: list[dict[str, Any]] = []
    window_failures: list[dict[str, Any]] = []
    selected_parameters_rows: list[dict[str, Any]] = []
    for index, window in enumerate(getattr(result, "windows", []) or [], start=1):
        params = dict(getattr(window, "selected_parameters", {}) or {})
        key = "|".join(f"{k}={params[k]}" for k in sorted(params))
        selected_parameter_frequency[key] = selected_parameter_frequency.get(key, 0) + 1
        test_metrics = dict(getattr(window, "test_metrics", {}) or {})
        window_row = {
            "window_index": index,
            "train_start": window.train_range.get("start"),
            "train_end": window.train_range.get("end"),
            "validation_start": window.validation_range.get("start"),
            "validation_end": window.validation_range.get("end"),
            "test_start": window.test_range.get("start"),
            "test_end": window.test_range.get("end"),
            "validation_score": getattr(window, "validation_score", None),
            "trade_count": getattr(window, "trade_count", None),
            "selected_parameters": json.dumps(_jsonable(params), ensure_ascii=False),
            "test_total_return": test_metrics.get("total_return"),
            "test_max_drawdown": test_metrics.get("max_drawdown"),
            "test_sharpe": test_metrics.get("sharpe"),
            "test_sortino": test_metrics.get("sortino"),
            "test_calmar": test_metrics.get("calmar"),
            "reconciliation_status": test_metrics.get("reconciliation_status"),
            "benchmark_status": test_metrics.get("benchmark_status"),
        }
        window_results.append(window_row)
        selected_parameters_rows.append(
            {
                "window_index": index,
                "parameter_key": key,
                "selected_parameters": json.dumps(_jsonable(params), ensure_ascii=False),
                "validation_score": getattr(window, "validation_score", None),
                "test_total_return": test_metrics.get("total_return"),
                "test_trade_count": test_metrics.get("trade_count"),
            }
        )
        if not getattr(window, "warnings", None) and int(getattr(window, "trade_count", 0) or 0) > 0:
            continue
        failure_reason_text = "no_trade" if int(getattr(window, "trade_count", 0) or 0) == 0 else ",".join(getattr(window, "warnings", []) or [])
        window_failures.append(
            {
                "window_index": index,
                "reason": failure_reason_text or "unknown",
                "train_start": window.train_range.get("start"),
                "train_end": window.train_range.get("end"),
                "validation_start": window.validation_range.get("start"),
                "validation_end": window.validation_range.get("end"),
                "test_start": window.test_range.get("start"),
                "test_end": window.test_range.get("end"),
            }
        )
    total_windows = len(getattr(result, "windows", []) or [])
    active_window_ratio = round(len(active_windows) / total_windows, 6) if total_windows else 0.0
    no_trade_window_ratio = round(len(no_trade_windows) / total_windows, 6) if total_windows else 0.0
    positive_window_ratio = round(len(positive_windows) / max(1, len(active_windows)), 6) if active_windows else 0.0
    aggregate_oos_total_return = round(sum(total_return_values), 6)
    aggregate_oos_total_return_mean = round(mean(total_return_values), 6) if total_return_values else 0.0
    median_window_return = round(sorted(total_return_values)[len(total_return_values) // 2], 6) if total_return_values else 0.0
    max_drawdown = float(aggregate.get("max_drawdown_max") or 0.0)
    profit_factor = aggregate.get("profit_factor_mean")
    profit_factor_status = "OK" if profit_factor is not None and float(profit_factor) > 1.0 else "NOT_ELIGIBLE"
    ranking_status = str(getattr(result, "parameter_stability", {}).get("ranking_status") or "INSUFFICIENT_EVIDENCE")
    return {
        "candidate_id": candidate.candidate_id,
        "previous_status": current_status,
        "proposed_status": ValidationStatus.PENDING_WALK_FORWARD.value,
        "final_status": ValidationStatus.PENDING_WALK_FORWARD.value if dry_run else ValidationStatus.WALK_FORWARD_COMPLETE.value,
        "operator": operator,
        "reason": reason,
        "started_at": started_at,
        "completed_at": completed_at,
        "symbol": candidate.symbol,
        "benchmarks": list(candidate.benchmarks),
        "timeframe": candidate.timeframe,
        "strategy_family": candidate.strategy_family,
        "parameter_registry_version": parameter_registry_version,
        "walk_forward_engine_version": walk_forward_engine_version,
        "strategy_version": strategy_version,
        "training_window": training_window,
        "test_window": {
            "train_size": walk_forward_config.train_size,
            "validation_size": walk_forward_config.validation_size,
            "test_size": walk_forward_config.test_size,
            "step_size": walk_forward_config.step_size,
            "purge_gap": walk_forward_config.purge_gap,
            "embargo_gap": walk_forward_config.embargo_gap,
            "random_seed": walk_forward_config.random_seed,
        },
        "resolved_parameter_grid": parameter_grid,
        "output_dir": str(output_root),
        "quote_api_used": False,
        "trade_api_used": False,
        "broker_used": False,
        "trade_context_initialized": False,
        "applied": bool(not dry_run),
        "success": success if not dry_run else False,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "candidate_current_status": current_status if dry_run else (ValidationStatus.WALK_FORWARD_COMPLETE.value if success else ValidationStatus.WALK_FORWARD_FAILED.value),
        "current_status_after": current_status if dry_run else (ValidationStatus.WALK_FORWARD_COMPLETE.value if success else ValidationStatus.WALK_FORWARD_FAILED.value),
        "candidate_status_after": current_status if dry_run else (ValidationStatus.WALK_FORWARD_COMPLETE.value if success else ValidationStatus.WALK_FORWARD_FAILED.value),
        "benchmark_status": benchmark_status,
        "reconciliation_status": str(comparison_summary.get("reconciliation_status") or "OK").upper(),
        "evidence_status": evidence_status,
        "profitability_status": profitability_status,
        "deployment_status": deployment_status,
        "walk_forward_metrics": {
            "windows": total_windows,
            "window_failures": len(window_failures),
            "active_windows": len(active_windows),
            "active_window_ratio": active_window_ratio,
            "no_trade_windows": len(no_trade_windows),
            "no_trade_window_ratio": no_trade_window_ratio,
            "total_trade_count": int(sum(int(window.trade_count or 0) for window in getattr(result, "windows", []) or [])),
            "aggregate_oos_total_return": aggregate_oos_total_return,
            "aggregate_oos_total_return_mean": aggregate_oos_total_return_mean,
            "median_window_return": median_window_return,
            "positive_window_ratio": positive_window_ratio,
            "max_drawdown": round(max_drawdown, 6),
            "Sharpe": aggregate.get("sharpe_mean"),
            "Sortino": aggregate.get("sortino_mean"),
            "Calmar": aggregate.get("calmar_mean"),
            "profit_factor": profit_factor,
            "turnover": aggregate.get("turnover_mean"),
            "instability_score": float((getattr(result, "parameter_stability", {}) or {}).get("instability_score") or 0.0),
            "selected_parameter_frequency": selected_parameter_frequency,
            "regime_dependent": bool((getattr(result, "parameter_stability", {}) or {}).get("regime_dependent", False)),
            "isolated_optimum": bool((getattr(result, "parameter_stability", {}) or {}).get("isolated_optimum", False)),
            "too_few_trades": bool((getattr(result, "parameter_stability", {}) or {}).get("too_few_trades", False)),
            "high_turnover": bool((getattr(result, "parameter_stability", {}) or {}).get("high_turnover", False)),
            "drawdown_sensitive": bool((getattr(result, "parameter_stability", {}) or {}).get("drawdown_sensitive", False)),
            "benchmark_sensitive": bool(benchmark_status != "VALID"),
            "ranking_status": ranking_status,
            "profit_factor_status": profit_factor_status,
        },
        "backtest_audit_path": str(backtest_audit_path),
        "backtest_run_dir": str(backtest_run_dir),
        "backtest_summary_path": str(backtest_run_dir / "backtest_run_summary.csv"),
        "backtest_evidence_summary": {
            "benchmark_status": benchmark_status,
            "reconciliation_status": str(comparison_summary.get("reconciliation_status") or "OK").upper(),
            "completed_versions": completed_versions,
            "strategy_metrics_rows": len(strategy_metrics),
            "strategy_ranking_rows": len(strategy_ranking),
            "result_warning_count": len(getattr(result, "warnings", []) or []),
        },
        "artifacts": {
            "walk_forward_run_audit_json": str(output_root / "walk_forward_run_audit.json"),
            "walk_forward_run_summary_csv": str(output_root / "walk_forward_run_summary.csv"),
            "walk_forward_metrics_json": str(output_root / "walk_forward_metrics.json"),
            "window_results_csv": str(output_root / "window_results.csv"),
            "selected_parameters_csv": str(output_root / "selected_parameters.csv"),
            "oos_equity_csv": str(output_root / "oos_equity.csv"),
            "oos_drawdown_csv": str(output_root / "oos_drawdown.csv"),
            "window_failures_csv": str(output_root / "window_failures.csv"),
            "parameter_stability_json": str(output_root / "parameter_stability.json"),
            "report_md": str(output_root / "report.md"),
        },
        "all_trading_flags_false": _candidate_flags_false(candidate),
    }


def _validate_common_candidate(candidate: CandidateRecord, policy: WalkForwardFamilyPolicy) -> list[str]:
    errors: list[str] = []
    if not _candidate_flags_false(candidate):
        errors.append("candidate_write_flag_enabled")
    if not str(candidate.candidate_id or "").strip():
        errors.append("candidate_id_required")
    if not str(candidate.symbol or "").strip():
        errors.append("symbol_required")
    if not candidate.benchmarks:
        errors.append("benchmark_required")
    if not str(candidate.strategy_family or "").strip():
        errors.append("strategy_family_required")
    if not str(candidate.timeframe or "").strip():
        errors.append("timeframe_required")
    if candidate.validation_status != ValidationStatus.PENDING_WALK_FORWARD.value:
        errors.append("candidate_must_be_pending_walk_forward")
    if candidate.is_leveraged_or_inverse() and candidate.risk_profile not in {"strict", "very_strict"}:
        errors.append("missing_or_weak_risk_profile")
    if policy.requires_strict_risk_profile and candidate.risk_profile not in {"strict", "very_strict"}:
        errors.append("missing_or_weak_risk_profile")
    return errors


def _resolve_primary_benchmark(candidate: CandidateRecord, backtest_summary: dict[str, Any]) -> str:
    benchmark_symbol = str(backtest_summary.get("benchmark_symbol") or "").strip()
    if benchmark_symbol:
        return benchmark_symbol
    if candidate.benchmarks:
        return str(candidate.benchmarks[0]).strip()
    raise CandidateTransitionError("benchmark_required")


def _derive_walk_forward_artifacts(result: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    window_results: list[dict[str, Any]] = []
    selected_parameters: list[dict[str, Any]] = []
    window_failures: list[dict[str, Any]] = []
    for index, window in enumerate(getattr(result, "windows", []) or [], start=1):
        params = dict(getattr(window, "selected_parameters", {}) or {})
        test_metrics = dict(getattr(window, "test_metrics", {}) or {})
        parameter_key = "|".join(f"{k}={params[k]}" for k in sorted(params))
        selected_parameters.append(
            {
                "window_index": index,
                "parameter_key": parameter_key,
                "selected_parameters": json.dumps(_jsonable(params), ensure_ascii=False),
                "validation_score": getattr(window, "validation_score", None),
                "test_total_return": test_metrics.get("total_return"),
                "test_trade_count": test_metrics.get("trade_count"),
            }
        )
        window_results.append(
            {
                "window_index": index,
                "train_start": window.train_range.get("start"),
                "train_end": window.train_range.get("end"),
                "validation_start": window.validation_range.get("start"),
                "validation_end": window.validation_range.get("end"),
                "test_start": window.test_range.get("start"),
                "test_end": window.test_range.get("end"),
                "validation_score": getattr(window, "validation_score", None),
                "trade_count": getattr(window, "trade_count", None),
                "selected_parameters": json.dumps(_jsonable(params), ensure_ascii=False),
                "test_total_return": test_metrics.get("total_return"),
                "test_max_drawdown": test_metrics.get("max_drawdown"),
                "test_sharpe": test_metrics.get("sharpe"),
                "test_sortino": test_metrics.get("sortino"),
                "test_calmar": test_metrics.get("calmar"),
                "reconciliation_status": test_metrics.get("reconciliation_status"),
                "benchmark_status": test_metrics.get("benchmark_status"),
            }
        )
        if int(getattr(window, "trade_count", 0) or 0) == 0:
            window_failures.append(
                {
                    "window_index": index,
                    "reason": "no_trade",
                    "train_start": window.train_range.get("start"),
                    "train_end": window.train_range.get("end"),
                    "validation_start": window.validation_range.get("start"),
                    "validation_end": window.validation_range.get("end"),
                    "test_start": window.test_range.get("start"),
                    "test_end": window.test_range.get("end"),
                }
            )
        for warning in getattr(window, "warnings", []) or []:
            window_failures.append(
                {
                    "window_index": index,
                    "reason": str(warning),
                    "train_start": window.train_range.get("start"),
                    "train_end": window.train_range.get("end"),
                    "validation_start": window.validation_range.get("start"),
                    "validation_end": window.validation_range.get("end"),
                    "test_start": window.test_range.get("start"),
                    "test_end": window.test_range.get("end"),
                }
            )
    return window_results, selected_parameters, window_failures


def admit_candidate_to_walk_forward(
    *,
    candidate_id: str,
    candidate_store: str | Path,
    output_dir: str | Path,
    dry_run: bool = True,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    _ensure_no_parent_refs(candidate_store, label="candidate_store")
    _ensure_no_parent_refs(output_dir, label="output_dir")
    store = CandidateValidationStore(_resolve_candidate_store_root(candidate_store))
    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise CandidateTransitionError(f"candidate_not_found:{candidate_id}")
    if candidate.validation_status != ValidationStatus.PENDING_WALK_FORWARD.value:
        raise CandidateTransitionError("candidate_must_be_pending_walk_forward")
    if not operator or not str(operator).strip():
        raise CandidateTransitionError("operator_required")
    if not reason or not str(reason).strip():
        raise CandidateTransitionError("reason_required")

    policy = _strategy_family_policy(candidate.strategy_family)
    admission_errors = _validate_common_candidate(candidate, policy)
    if admission_errors:
        raise CandidateTransitionError(", ".join(admission_errors))

    admission_audit_path = _resolve_candidate_admission_path(candidate, store)
    admission_audit = _load_json(admission_audit_path)
    if str(admission_audit.get("candidate_id") or "").strip() != candidate.candidate_id:
        raise CandidateTransitionError("walk_forward_admission_candidate_mismatch")
    if str(admission_audit.get("applied") if "applied" in admission_audit else True).lower() not in {"true", "1"}:
        raise CandidateTransitionError("walk_forward_admission_not_applied")
    if str(admission_audit.get("previous_status") or "").strip().upper() != ValidationStatus.BACKTEST_COMPLETE.value:
        raise CandidateTransitionError("walk_forward_admission_previous_status_mismatch")
    if str(admission_audit.get("proposed_status") or "").strip().upper() != ValidationStatus.PENDING_WALK_FORWARD.value:
        raise CandidateTransitionError("walk_forward_admission_status_mismatch")

    backtest_audit_path, backtest_run_dir, backtest_summary, backtest_comparison_summary, backtest_metrics, strategy_metrics, strategy_ranking, completed_versions = None, None, None, None, None, None, None, None
    try:
        (
            backtest_audit,
            backtest_audit_path,
            backtest_run_dir,
            backtest_comparison_summary,
            backtest_summary,
            backtest_metrics,
            strategy_metrics,
            strategy_ranking,
            completed_versions,
        ) = _read_backtest_evidence(candidate, store)
    except CandidateTransitionError:
        raise
    except Exception as exc:
        raise CandidateTransitionError(f"backtest_preflight_failed:{type(exc).__name__}:{exc}") from exc

    benchmark_status = str(backtest_comparison_summary.get("benchmark_status") or "").strip().upper()
    if benchmark_status != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")

    future_data_risk = bool((backtest_audit.get("candidate_metadata") or {}).get("future_data_risk", False))
    if future_data_risk:
        raise CandidateTransitionError("future_data_risk_must_be_false")

    if any(str(row.get("reconciliation_status") or "").strip().upper() != "OK" for row in backtest_metrics):
        raise CandidateTransitionError("reconciliation_must_be_ok")

    data_root = DEFAULT_DATA_ROOT
    symbol_path = _find_local_data_file(data_root, candidate.symbol, candidate.timeframe)
    benchmark_symbol = _resolve_primary_benchmark(candidate, backtest_comparison_summary)
    benchmark_path = _find_local_data_file(data_root, benchmark_symbol, candidate.timeframe)
    if symbol_path is None or benchmark_path is None:
        raise CandidateTransitionError("missing_local_walk_forward_data")

    if not _validate_benchmark_and_data_alignment(candidate, symbol_path, benchmark_path):
        raise CandidateTransitionError("benchmark_status_invalid")

    training_window = {
        "train_size": int(policy.walk_forward_config.train_size),
        "validation_size": int(policy.walk_forward_config.validation_size),
        "test_size": int(policy.walk_forward_config.test_size),
        "step_size": int(policy.walk_forward_config.step_size),
        "purge_gap": int(policy.walk_forward_config.purge_gap),
        "embargo_gap": int(policy.walk_forward_config.embargo_gap),
        "random_seed": int(policy.walk_forward_config.random_seed),
    }
    parameter_grid = [dict(params) for params in policy.parameter_grid]
    resolved_strategy_version = _resolve_strategy_version(backtest_comparison_summary)

    output_root = Path(output_dir).expanduser() / _safe_component(candidate.candidate_id)
    started_at = _utc_now_iso()
    if dry_run:
        return {
            "candidate_id": candidate.candidate_id,
            "current_status": candidate.validation_status,
            "candidate_current_status": candidate.validation_status,
            "current_status_after": candidate.validation_status,
            "candidate_status_after": candidate.validation_status,
            "proposed_status": ValidationStatus.PENDING_WALK_FORWARD.value,
            "proposed_action": "run_walk_forward",
            "resolved_symbol": candidate.symbol,
            "resolved_benchmarks": list(candidate.benchmarks),
            "resolved_timeframe": candidate.timeframe,
            "resolved_strategy_family": policy.resolved_family,
            "resolved_strategy_version": resolved_strategy_version,
            "resolved_parameter_grid": parameter_grid,
            "train_test_window_configuration": training_window,
            "output_dir": str(output_root),
            "applied": False,
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "admission_audit_path": str(admission_audit_path),
            "backtest_audit_path": str(backtest_audit_path),
            "benchmark_status": benchmark_status,
            "reconciliation_status": "OK",
            "all_trading_flags_false": _candidate_flags_false(candidate),
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
            "quote_api_used": False,
            "walk_forward_started": False,
            "started_at": started_at,
            "completed_at": started_at,
            "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
            "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
        }

    output_root.mkdir(parents=True, exist_ok=True)
    failure_stage = "preflight"
    failure_reason = ""
    success = False
    result = None
    walk_forward_audit_path = output_root / "walk_forward_run_audit.json"
    walk_forward_summary_path = output_root / "walk_forward_run_summary.csv"
    try:
        failure_stage = "execution"
        feed = BacktestDataFeed()
        bars = feed.load_csv(symbol_path, symbol=candidate.symbol)
        benchmark_bars = feed.load_csv(benchmark_path, symbol=benchmark_symbol)
        if not bars or not benchmark_bars:
            raise CandidateTransitionError("missing_local_walk_forward_data")
        evaluator = WalkForwardEvaluator(policy.walk_forward_config)
        result = evaluator.evaluate(
            bars,
            symbol=candidate.symbol,
            strategy=resolved_strategy_version,
            parameter_grid=parameter_grid,
            benchmark_bars=benchmark_bars,
            initial_cash=10_000.0,
            max_candidates=len(parameter_grid),
        )
        failure_stage = "artifact_verification"
        if not getattr(result, "windows", None):
            raise CandidateTransitionError("walk_forward_result_empty")
        if any(str(window.test_metrics.get("reconciliation_status") or "").strip().upper() != "OK" for window in result.windows):
            raise CandidateTransitionError("reconciliation_must_be_ok")
        if str((result.parameter_stability or {}).get("ranking_status") or "").strip().upper() == "INVALID_BENCHMARK":
            raise CandidateTransitionError("benchmark_status_invalid")
        window_results, selected_parameters, window_failures = _derive_walk_forward_artifacts(result)
        if not window_results:
            raise CandidateTransitionError("walk_forward_windows_missing")
        if not any(int(row.get("trade_count", 0) or 0) > 0 for row in window_results):
            # still technically complete, but must have at least one meaningful window
            raise CandidateTransitionError("walk_forward_no_meaningful_windows")

        aggregate = dict(getattr(result, "aggregate_oos_metrics", {}) or {})
        evidence_status = str((result.parameter_stability or {}).get("evidence_status") or "INSUFFICIENT_EVIDENCE")
        profitability_status = str((result.parameter_stability or {}).get("profitability_status") or "INELIGIBLE")
        deployment_status = str((result.parameter_stability or {}).get("deployment_status") or "INELIGIBLE")
        summary_payload = {
            "candidate_id": candidate.candidate_id,
            "previous_status": candidate.validation_status,
            "final_status": ValidationStatus.WALK_FORWARD_COMPLETE.value,
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "started_at": started_at,
            "completed_at": _utc_now_iso(),
            "symbol": candidate.symbol,
            "benchmarks": list(candidate.benchmarks),
            "timeframe": candidate.timeframe,
            "strategy_family": candidate.strategy_family,
            "resolved_strategy_family": policy.resolved_family,
            "resolved_strategy_version": resolved_strategy_version,
            "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
            "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
            "training_window": training_window,
            "parameter_grid": parameter_grid,
            "walk_forward_metrics": {
                "windows": len(result.windows),
                "window_failures": len(window_failures),
                "active_windows": sum(1 for window in result.windows if int(window.trade_count or 0) > 0),
                "active_window_ratio": round(sum(1 for window in result.windows if int(window.trade_count or 0) > 0) / len(result.windows), 6) if result.windows else 0.0,
                "no_trade_windows": sum(1 for window in result.windows if int(window.trade_count or 0) == 0),
                "no_trade_window_ratio": round(sum(1 for window in result.windows if int(window.trade_count or 0) == 0) / len(result.windows), 6) if result.windows else 0.0,
                "total_trade_count": int(sum(int(window.trade_count or 0) for window in result.windows)),
                "aggregate_oos_total_return": round(sum(float(window.test_metrics.get("total_return") or 0.0) for window in result.windows), 6),
                "aggregate_oos_total_return_mean": round(mean(float(window.test_metrics.get("total_return") or 0.0) for window in result.windows), 6) if result.windows else 0.0,
                "median_window_return": sorted(float(window.test_metrics.get("total_return") or 0.0) for window in result.windows)[len(result.windows) // 2] if result.windows else 0.0,
                "positive_window_ratio": round(sum(1 for window in result.windows if float(window.test_metrics.get("total_return") or 0.0) > 0) / max(1, sum(1 for window in result.windows if int(window.trade_count or 0) > 0)), 6),
                "max_drawdown": round(float(aggregate.get("max_drawdown_max") or 0.0), 6),
                "Sharpe": aggregate.get("sharpe_mean"),
                "Sortino": aggregate.get("sortino_mean"),
                "Calmar": aggregate.get("calmar_mean"),
                "profit_factor": aggregate.get("profit_factor_mean"),
                "turnover": aggregate.get("turnover_mean"),
                "instability_score": float((result.parameter_stability or {}).get("instability_score") or 0.0),
                "selected_parameter_frequency": (result.parameter_stability or {}).get("selection_counts") or {},
                "regime_dependent": bool((result.parameter_stability or {}).get("regime_dependent", False)),
                "isolated_optimum": bool((result.parameter_stability or {}).get("isolated_optimum", False)),
                "too_few_trades": bool((result.parameter_stability or {}).get("too_few_trades", False)),
                "high_turnover": bool((result.parameter_stability or {}).get("high_turnover", False)),
                "drawdown_sensitive": bool((result.parameter_stability or {}).get("drawdown_sensitive", False)),
                "benchmark_sensitive": bool((result.parameter_stability or {}).get("benchmark_validation", {}).get("status") != "VALID"),
                "evidence_status": evidence_status,
                "profitability_status": profitability_status,
                "deployment_status": deployment_status,
            },
            "parameter_stability": result.parameter_stability,
            "window_results": window_results,
            "selected_parameters": selected_parameters,
            "window_failures": window_failures,
            "oos_equity": result.stitched_oos_equity,
            "oos_drawdown": _compute_drawdown_rows(result.stitched_oos_equity),
            "evidence_status": evidence_status,
            "profitability_status": profitability_status,
            "deployment_status": deployment_status,
            "benchmark_status": benchmark_status,
            "reconciliation_status": "OK",
            "quote_api_used": False,
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
            "applied": True,
            "success": True,
            "failure_stage": "",
            "failure_reason": "",
            "all_trading_flags_false": _candidate_flags_false(candidate),
            "backtest_audit_path": str(backtest_audit_path),
            "backtest_run_dir": str(backtest_run_dir),
            "admission_audit_path": str(admission_audit_path),
            "data_paths": {"symbol": str(symbol_path), "benchmark": str(benchmark_path)},
            "walk_forward_run_dir": str(output_root),
        }
        _write_walk_forward_artifacts(output_root, summary_payload, result)
        updated = store.transition(
            candidate.candidate_id,
            ValidationStatus.WALK_FORWARD_COMPLETE,
            reason=str(reason).strip(),
            metadata={
                "walk_forward_complete": True,
                "operator": str(operator).strip(),
                "reason": str(reason).strip(),
                "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
                "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
                "previous_status": candidate.validation_status,
                "started_at": started_at,
                "completed_at": summary_payload["completed_at"],
                "symbol": candidate.symbol,
                "benchmarks": list(candidate.benchmarks),
                "timeframe": candidate.timeframe,
                "strategy_family": candidate.strategy_family,
                "walk_forward_audit_path": str(walk_forward_audit_path),
                "walk_forward_summary_path": str(walk_forward_summary_path),
                "backtest_audit_path": str(backtest_audit_path),
                "backtest_run_dir": str(backtest_run_dir),
                "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
                "trade_api_used": False,
                "broker_used": False,
                "trade_context_initialized": False,
                "quote_api_used": False,
                "all_trading_flags_false": _candidate_flags_false(candidate),
                "evidence_status": evidence_status,
                "profitability_status": profitability_status,
                "deployment_status": deployment_status,
                "walk_forward_metrics": summary_payload["walk_forward_metrics"],
            },
        )
        summary_payload["candidate_current_status"] = updated.validation_status
        summary_payload["current_status_after"] = updated.validation_status
        summary_payload["candidate_status_after"] = updated.validation_status
        _atomic_write_text(walk_forward_audit_path, json.dumps(_jsonable(summary_payload), indent=2, ensure_ascii=False))
        _atomic_write_csv(
            walk_forward_summary_path,
            [
                {
                    "candidate_id": candidate.candidate_id,
                    "previous_status": ValidationStatus.PENDING_WALK_FORWARD.value,
                    "final_status": ValidationStatus.WALK_FORWARD_COMPLETE.value,
                    "operator": str(operator).strip(),
                    "reason": str(reason).strip(),
                    "started_at": started_at,
                    "completed_at": summary_payload["completed_at"],
                    "symbol": candidate.symbol,
                    "benchmarks": "|".join(candidate.benchmarks),
                    "timeframe": candidate.timeframe,
                    "strategy_family": candidate.strategy_family,
                    "resolved_strategy_family": policy.resolved_family,
                    "resolved_strategy_version": resolved_strategy_version,
                    "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
                    "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
                    "quote_api_used": False,
                    "trade_api_used": False,
                    "broker_used": False,
                    "trade_context_initialized": False,
                    "applied": True,
                    "success": True,
                }
            ],
        )
        return summary_payload
    except Exception as exc:
        failure_stage = failure_stage or "execution"
        failure_reason = f"{type(exc).__name__}:{exc}"
        summary_payload = {
            "candidate_id": candidate.candidate_id,
            "previous_status": candidate.validation_status,
            "final_status": ValidationStatus.WALK_FORWARD_FAILED.value,
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "started_at": started_at,
            "completed_at": _utc_now_iso(),
            "symbol": candidate.symbol,
            "benchmarks": list(candidate.benchmarks),
            "timeframe": candidate.timeframe,
            "strategy_family": candidate.strategy_family,
            "resolved_strategy_family": policy.resolved_family,
            "resolved_strategy_version": resolved_strategy_version,
            "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
            "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
            "training_window": training_window,
            "parameter_grid": parameter_grid,
            "evidence_status": "INSUFFICIENT_EVIDENCE",
            "profitability_status": "INELIGIBLE",
            "deployment_status": "INELIGIBLE",
            "quote_api_used": False,
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
            "applied": True,
            "success": False,
            "failure_stage": failure_stage,
            "failure_reason": failure_reason,
            "all_trading_flags_false": _candidate_flags_false(candidate),
            "backtest_audit_path": str(backtest_audit_path),
            "backtest_run_dir": str(backtest_run_dir),
            "admission_audit_path": str(admission_audit_path),
            "data_paths": {"symbol": str(symbol_path), "benchmark": str(benchmark_path)},
            "walk_forward_run_dir": str(output_root),
        }
        _write_walk_forward_artifacts(output_root, summary_payload, None)
        try:
            transitioned = store.transition(
                candidate.candidate_id,
                ValidationStatus.WALK_FORWARD_FAILED,
                reason=failure_reason,
                metadata={
                    "operator": str(operator).strip(),
                    "reason": str(reason).strip(),
                    "failure_reason": failure_reason,
                    "failure_stage": failure_stage,
                    "previous_status": candidate.validation_status,
                    "started_at": started_at,
                    "completed_at": summary_payload["completed_at"],
                    "symbol": candidate.symbol,
                    "benchmarks": list(candidate.benchmarks),
                    "timeframe": candidate.timeframe,
                    "strategy_family": candidate.strategy_family,
                    "walk_forward_audit_path": str(walk_forward_audit_path),
                    "walk_forward_summary_path": str(walk_forward_summary_path),
                    "backtest_audit_path": str(backtest_audit_path),
                    "backtest_run_dir": str(backtest_run_dir),
                    "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
                    "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
                    "trade_api_used": False,
                    "broker_used": False,
                    "trade_context_initialized": False,
                    "quote_api_used": False,
                    "all_trading_flags_false": _candidate_flags_false(candidate),
                    "evidence_status": "INSUFFICIENT_EVIDENCE",
                    "profitability_status": "INELIGIBLE",
                    "deployment_status": "INELIGIBLE",
                },
            )
        except Exception:
            transitioned = None
        if transitioned is not None:
            summary_payload["candidate_current_status"] = transitioned.validation_status
            summary_payload["current_status_after"] = transitioned.validation_status
            summary_payload["candidate_status_after"] = transitioned.validation_status
        _atomic_write_text(walk_forward_audit_path, json.dumps(_jsonable(summary_payload), indent=2, ensure_ascii=False))
        _atomic_write_csv(
            walk_forward_summary_path,
            [
                {
                    "candidate_id": candidate.candidate_id,
                    "previous_status": candidate.validation_status,
                    "final_status": ValidationStatus.WALK_FORWARD_FAILED.value,
                    "operator": str(operator).strip(),
                    "reason": str(reason).strip(),
                    "started_at": started_at,
                    "completed_at": summary_payload["completed_at"],
                    "symbol": candidate.symbol,
                    "benchmarks": "|".join(candidate.benchmarks),
                    "timeframe": candidate.timeframe,
                    "strategy_family": candidate.strategy_family,
                    "resolved_strategy_family": policy.resolved_family,
                    "resolved_strategy_version": resolved_strategy_version,
                    "parameter_registry_version": WALK_FORWARD_PARAMETER_REGISTRY_VERSION,
                    "walk_forward_engine_version": WALK_FORWARD_ENGINE_VERSION,
                    "quote_api_used": False,
                    "trade_api_used": False,
                    "broker_used": False,
                    "trade_context_initialized": False,
                    "applied": True,
                    "success": False,
                    "failure_stage": failure_stage,
                    "failure_reason": failure_reason,
                }
            ],
        )
        return summary_payload


def _validate_benchmark_and_data_alignment(candidate: CandidateRecord, symbol_path: Path, benchmark_path: Path) -> bool:
    feed = BacktestDataFeed()
    symbol_bars = feed.load_csv(symbol_path, symbol=candidate.symbol)
    benchmark_symbol = candidate.benchmarks[0] if candidate.benchmarks else ""
    benchmark_bars = feed.load_csv(benchmark_path, symbol=benchmark_symbol)
    validation = validate_benchmark_alignment(candidate.symbol, symbol_bars, benchmark_bars)
    return validation.status == "VALID"


def _resolve_strategy_version(backtest_summary: dict[str, Any]) -> str:
    best = backtest_summary.get("best_version_all") or backtest_summary.get("best_version")
    if isinstance(best, dict):
        version = str(best.get("version") or "").strip().lower()
        if version in {"baseline", "a", "b", "c"}:
            return version
    for version in ("a", "b", "baseline", "c"):
        if version in {str(item.get("version") or "").strip().lower() for item in (backtest_summary.get("comparison") or []) if isinstance(item, dict)}:
            return version
    return "baseline"


def _compute_drawdown_rows(equity_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    peak = None
    for row in equity_rows or []:
        equity = float(row.get("equity") or 0.0)
        if peak is None or equity > peak:
            peak = equity
        drawdown = max(0.0, (peak or equity) - equity)
        rows.append(
            {
                "timestamp": row.get("timestamp"),
                "drawdown": round(drawdown, 6),
                "peak_equity": round(peak or equity, 6),
                "equity": round(equity, 6),
                "window_start": row.get("window_start"),
                "window_end": row.get("window_end"),
            }
        )
    return rows


def _write_walk_forward_artifacts(root: Path, payload: dict[str, Any], result: Any | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    audit_path = root / "walk_forward_run_audit.json"
    summary_csv_path = root / "walk_forward_run_summary.csv"
    metrics_path = root / "walk_forward_metrics.json"
    window_results_path = root / "window_results.csv"
    selected_parameters_path = root / "selected_parameters.csv"
    oos_equity_path = root / "oos_equity.csv"
    oos_drawdown_path = root / "oos_drawdown.csv"
    window_failures_path = root / "window_failures.csv"
    stability_path = root / "parameter_stability.json"
    report_path = root / "report.md"

    _atomic_write_text(audit_path, json.dumps(_jsonable(payload), indent=2, ensure_ascii=False))
    summary_row = {
        "candidate_id": payload.get("candidate_id"),
        "previous_status": payload.get("previous_status"),
        "final_status": payload.get("final_status"),
        "operator": payload.get("operator"),
        "reason": payload.get("reason"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "symbol": payload.get("symbol"),
        "benchmarks": "|".join(payload.get("benchmarks") or []),
        "timeframe": payload.get("timeframe"),
        "strategy_family": payload.get("strategy_family"),
        "resolved_strategy_family": payload.get("resolved_strategy_family"),
        "resolved_strategy_version": payload.get("resolved_strategy_version"),
        "parameter_registry_version": payload.get("parameter_registry_version"),
        "walk_forward_engine_version": payload.get("walk_forward_engine_version"),
        "quote_api_used": False,
        "trade_api_used": False,
        "broker_used": False,
        "trade_context_initialized": False,
        "applied": payload.get("applied"),
        "success": payload.get("success"),
        "failure_stage": payload.get("failure_stage", ""),
        "failure_reason": payload.get("failure_reason", ""),
    }
    _atomic_write_csv(summary_csv_path, [summary_row])
    _atomic_write_text(metrics_path, json.dumps(_jsonable(payload.get("walk_forward_metrics") or {}), indent=2, ensure_ascii=False))
    _atomic_write_csv(window_results_path, list(payload.get("window_results") or []))
    _atomic_write_csv(selected_parameters_path, list(payload.get("selected_parameters") or []))
    _atomic_write_csv(oos_equity_path, list(payload.get("oos_equity") or []))
    _atomic_write_csv(oos_drawdown_path, list(payload.get("oos_drawdown") or []))
    _atomic_write_csv(window_failures_path, list(payload.get("window_failures") or []))
    _atomic_write_text(stability_path, json.dumps(_jsonable(payload.get("parameter_stability") or {}), indent=2, ensure_ascii=False))
    lines = [
        "# Candidate Walk-Forward Report",
        "",
        f"- Candidate: {payload.get('candidate_id')}",
        f"- Symbol: {payload.get('symbol')}",
        f"- Strategy family: {payload.get('strategy_family')} -> {payload.get('resolved_strategy_family')}",
        f"- Status: {payload.get('final_status')}",
        f"- Benchmark status: {payload.get('benchmark_status')}",
        f"- Reconciliation status: {payload.get('reconciliation_status', 'OK')}",
        f"- Applied: {payload.get('applied')}",
        f"- Success: {payload.get('success')}",
        "",
        "## Metrics",
    ]
    for key, value in sorted((payload.get("walk_forward_metrics") or {}).items()):
        lines.append(f"- {key}: {value}")
    _atomic_write_text(report_path, "\n".join(lines) + "\n")
