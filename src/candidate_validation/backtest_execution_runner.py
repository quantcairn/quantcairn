from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backtest import BacktestDataFeed
from src.backtest.comparison import compare_versions
from src.backtest.data_feed import BacktestDataError

from .models import CandidateRecord, CandidateTransitionError, ValidationStatus
from .store import CandidateValidationStore


PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
DEFAULT_DATA_ROOT = PROJECT_DIR / "data" / "market" / "longbridge"
DEFAULT_VALIDATION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "validation"
DEFAULT_ADMISSION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "admission"
DEFAULT_BACKTEST_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "backtests"
BACKTEST_ENGINE_VERSION = "manual_candidate_backtest_v1"
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

@dataclass(frozen=True, slots=True)
class StrategyFamilyPolicy:
    family: str
    strategy_versions: tuple[str, ...]
    supported: bool = True
    alias_of: str | None = None
    direction_mode: str = "generic"
    requires_benchmark: bool = True
    requires_strict_risk_profile: bool = False
    notes: str = ""


STRATEGY_FAMILY_REGISTRY: dict[str, StrategyFamilyPolicy] = {
    "range_etf": StrategyFamilyPolicy(
        family="range_etf",
        strategy_versions=("baseline", "a", "b", "c"),
        direction_mode="range_etf",
        requires_benchmark=True,
        notes="Primary inverse/leveraged range family used by SOXS shadow universe defaults.",
    ),
    "inverse_range": StrategyFamilyPolicy(
        family="inverse_range",
        strategy_versions=("baseline", "a", "b", "c"),
        direction_mode="inverse_range",
        requires_benchmark=True,
        requires_strict_risk_profile=True,
        notes="Inverse ETF range policy with explicit strict risk handling.",
    ),
    "inverse_etf_range": StrategyFamilyPolicy(
        family="inverse_etf_range",
        strategy_versions=("baseline", "a", "b", "c"),
        alias_of="inverse_range",
        direction_mode="inverse_range",
        requires_benchmark=True,
        requires_strict_risk_profile=True,
        notes="Explicit alias for inverse_range; no separate engine implementation.",
    ),
    "leveraged_range": StrategyFamilyPolicy(
        family="leveraged_range",
        strategy_versions=("baseline", "a", "b", "c"),
        direction_mode="leveraged_range",
        requires_benchmark=True,
        requires_strict_risk_profile=True,
        notes="Leveraged ETF range policy with strict risk handling.",
    ),
    "equity_mean_reversion": StrategyFamilyPolicy(
        family="equity_mean_reversion",
        strategy_versions=("baseline", "a", "b", "c"),
        direction_mode="equity_mean_reversion",
        requires_benchmark=True,
        notes="Common-stock mean reversion family.",
    ),
    "mean_reversion": StrategyFamilyPolicy(
        family="mean_reversion",
        strategy_versions=("baseline", "a", "b", "c"),
        alias_of="equity_mean_reversion",
        direction_mode="equity_mean_reversion",
        requires_benchmark=True,
        notes="Explicit alias for equity_mean_reversion.",
    ),
    "benchmark": StrategyFamilyPolicy(
        family="benchmark",
        strategy_versions=("baseline", "a", "b", "c"),
        direction_mode="benchmark",
        requires_benchmark=True,
        notes="Benchmark/index-follow families run as benchmark-aligned baselines.",
    ),
    "index_follow": StrategyFamilyPolicy(
        family="index_follow",
        strategy_versions=("baseline", "a", "b", "c"),
        alias_of="benchmark",
        direction_mode="benchmark",
        requires_benchmark=True,
        notes="Explicit alias for benchmark family.",
    ),
}

UNSUPPORTED_STRATEGY_FAMILIES = {
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
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
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


def _candidate_dir(root: Path, candidate_id: str) -> Path:
    return Path(root) / _safe_component(candidate_id)


def _candidate_files(symbol: str, timeframe: str) -> list[str]:
    cleaned_symbol = str(symbol or "").strip().upper().replace(".", "_")
    cleaned_tf = str(timeframe or "").strip().lower() or "15m"
    patterns = [f"{cleaned_symbol}_{cleaned_tf}.csv", f"{cleaned_symbol}_{cleaned_tf}_latest.csv"]
    if cleaned_tf == "daily":
        patterns.extend([f"{cleaned_symbol}_daily.csv", f"{cleaned_symbol}_daily_latest.csv"])
    return patterns


def _find_local_data_file(root: Path, symbol: str, timeframe: str) -> Path | None:
    root = Path(root)
    for name in _candidate_files(symbol, timeframe):
        candidate = root / name
        if candidate.exists():
            return candidate
    slug = str(symbol or "").strip().upper().replace(".", "_")
    freq = str(timeframe or "").strip().lower() or "15m"
    matches = sorted(root.glob(f"{slug}_{freq}*.csv"))
    if matches:
        return matches[0]
    return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CandidateTransitionError(f"dataset_report_invalid:{path.name}:{exc}") from exc
    if not isinstance(payload, dict):
        raise CandidateTransitionError(f"dataset_report_invalid:{path.name}:root_must_be_object")
    return payload


def _resolve_path(
    raw_path: str | None,
    *,
    candidate_id: str,
    candidate_root: Path,
    project_roots: list[Path],
) -> Path | None:
    if not raw_path:
        return None
    _ensure_no_parent_refs(raw_path, label="path")
    path = Path(str(raw_path))
    allowed_roots = [
        Path(candidate_root).resolve(),
        DEFAULT_VALIDATION_ROOT.resolve(),
        DEFAULT_ADMISSION_ROOT.resolve(),
        DEFAULT_BACKTEST_ROOT.resolve(),
        PROJECT_DIR.resolve(),
    ]
    allowed_roots.extend(Path(root).resolve() for root in project_roots)
    candidates: list[Path] = []
    if path.is_absolute():
        resolved = path.resolve(strict=False)
        if any(
            resolved == root or resolved.is_relative_to(root)
            for root in allowed_roots
        ):
            candidates.append(resolved)
    if not path.is_absolute():
        candidates.extend(
            [
                candidate_root / path,
                candidate_root / candidate_id / path,
                DEFAULT_VALIDATION_ROOT / path,
                DEFAULT_VALIDATION_ROOT / candidate_id / path.name,
                DEFAULT_ADMISSION_ROOT / path,
                DEFAULT_ADMISSION_ROOT / candidate_id / path.name,
                DEFAULT_BACKTEST_ROOT / path,
                DEFAULT_BACKTEST_ROOT / candidate_id / path.name,
                DEFAULT_DATA_ROOT / path,
            ]
        )
        for root in project_roots:
            candidates.append(Path(root) / path)
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


def _load_candidate(store: CandidateValidationStore, candidate_id: str) -> CandidateRecord:
    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise CandidateTransitionError(f"candidate_not_found:{candidate_id}")
    return candidate


def _candidate_all_flags_false(candidate: CandidateRecord) -> bool:
    return not any(
        bool(flag)
        for flag in (
            candidate.trading_enabled,
            candidate.shadow_enabled,
            candidate.paper_enabled,
            candidate.live_enabled,
        )
    )


def _resolve_candidate_store_root(candidate_store: str | Path) -> Path:
    path = Path(candidate_store).expanduser()
    if path.exists() and path.is_file():
        return path.parent
    if path.name == "candidates.jsonl":
        return path.parent
    return path


def _resolve_strategy_family_policy(strategy_family: str) -> StrategyFamilyPolicy:
    family = str(strategy_family or "").strip().lower()
    if family in UNSUPPORTED_STRATEGY_FAMILIES:
        raise CandidateTransitionError(UNSUPPORTED_STRATEGY_FAMILIES[family])
    policy = STRATEGY_FAMILY_REGISTRY.get(family)
    if policy is None or not policy.supported:
        raise CandidateTransitionError(f"unknown_strategy_family:{family}")
    return policy


def _validate_backtest_candidate(candidate: CandidateRecord, *, policy: StrategyFamilyPolicy | None = None) -> list[str]:
    errors: list[str] = []
    if candidate.validation_status != ValidationStatus.PENDING_BACKTEST.value:
        errors.append("candidate_must_be_pending_backtest")
    if not _candidate_all_flags_false(candidate):
        errors.append("candidate_write_flag_enabled")
    if not str(candidate.symbol or "").strip():
        errors.append("symbol_required")
    if not candidate.benchmarks:
        errors.append("benchmark_required")
    if not str(candidate.timeframe or "").strip():
        errors.append("timeframe_required")
    if not str(candidate.strategy_family or "").strip():
        errors.append("strategy_family_required")
    resolved_policy = policy or _resolve_strategy_family_policy(candidate.strategy_family)
    if not resolved_policy.requires_benchmark:
        errors.append("benchmark_required")
    if resolved_policy.requires_strict_risk_profile and candidate.risk_profile not in {"strict", "very_strict"}:
        errors.append("missing_or_weak_risk_profile")
    return errors


def _resolve_backtest_admission_paths(candidate: CandidateRecord, store: CandidateValidationStore) -> tuple[Path, Path]:
    metadata = dict(candidate.metadata or {})
    candidate_id = _safe_component(candidate.candidate_id)
    audit_path = _resolve_path(
        str(metadata.get("backtest_admission_audit_path") or metadata.get("admission_audit_path") or ""),
        candidate_id=candidate_id,
        candidate_root=store.root_dir.parent,
        project_roots=[PROJECT_DIR, DEFAULT_ADMISSION_ROOT.parent],
    )
    if audit_path is None:
        default_audit = DEFAULT_ADMISSION_ROOT / candidate_id / "backtest_admission_audit.json"
        if default_audit.exists():
            audit_path = default_audit
    if audit_path is None:
        raise CandidateTransitionError("backtest_admission_audit_missing")
    audit_payload = _load_json(audit_path)
    if str(audit_payload.get("candidate_id") or "").strip() != candidate.candidate_id:
        raise CandidateTransitionError("backtest_admission_candidate_mismatch")
    if not bool(audit_payload.get("applied", False)):
        raise CandidateTransitionError("backtest_admission_not_applied")
    if str(audit_payload.get("new_status") or "").strip().upper() != ValidationStatus.PENDING_BACKTEST.value:
        raise CandidateTransitionError("backtest_admission_status_mismatch")
    dataset_report_raw = str(audit_payload.get("dataset_report_path") or metadata.get("dataset_validation_report_path") or "")
    dataset_report_path = _resolve_path(
        dataset_report_raw,
        candidate_id=candidate_id,
        candidate_root=store.root_dir.parent,
        project_roots=[PROJECT_DIR, DEFAULT_ADMISSION_ROOT.parent, DEFAULT_VALIDATION_ROOT.parent],
    )
    if dataset_report_path is None:
        raise CandidateTransitionError("dataset_report_missing")
    return audit_path, dataset_report_path


def _validate_dataset_report(candidate: CandidateRecord, report_path: Path) -> dict[str, Any]:
    payload = _load_json(report_path)
    if str(payload.get("candidate_id") or "").strip() != candidate.candidate_id:
        raise CandidateTransitionError("dataset_report_candidate_mismatch")
    if str(payload.get("symbol") or "").strip().upper() != candidate.symbol:
        raise CandidateTransitionError("dataset_report_symbol_mismatch")
    if str(payload.get("candidate_current_status") or "").strip().upper() != ValidationStatus.PENDING_DATA_VALIDATION.value:
        raise CandidateTransitionError("dataset_report_status_mismatch")
    if str(payload.get("validation_status_after") or "").strip().upper() != ValidationStatus.DATA_VALID.value:
        raise CandidateTransitionError("dataset_report_not_data_valid")
    overall = payload.get("overall") or {}
    if not isinstance(overall, dict):
        raise CandidateTransitionError("dataset_report_invalid:overall_must_be_object")
    if str(overall.get("status") or "").strip().upper() != ValidationStatus.DATA_VALID.value:
        raise CandidateTransitionError("dataset_report_not_data_valid")
    if str(overall.get("rejection_reason") or "").strip():
        raise CandidateTransitionError(f"dataset_report_not_eligible:{overall.get('rejection_reason')}")
    if not bool(overall.get("benchmark_valid", False)):
        raise CandidateTransitionError("benchmark_status_invalid")
    if str(overall.get("benchmark_status") or "VALID").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if not bool(overall.get("eligible_for_backtest", False)):
        raise CandidateTransitionError("eligible_for_backtest_required")
    if bool(overall.get("future_data_risk", True)):
        raise CandidateTransitionError("future_data_risk_must_be_false")
    if str(overall.get("reconciliation_status") or "").strip().upper() != "OK":
        raise CandidateTransitionError("reconciliation_must_be_ok")
    validations = payload.get("validations") or []
    if not isinstance(validations, list) or not validations:
        raise CandidateTransitionError("dataset_report_missing_validations")
    first = validations[0]
    if not isinstance(first, dict):
        raise CandidateTransitionError("dataset_report_invalid_validation_entry")
    if str(first.get("benchmark_status") or "").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if not bool(first.get("eligible_for_backtest", False)):
        raise CandidateTransitionError("eligible_for_backtest_required")
    if bool(first.get("future_data_risk", True)):
        raise CandidateTransitionError("future_data_risk_must_be_false")
    if int(first.get("duplicate_count", 0) or 0) != 0:
        raise CandidateTransitionError("duplicate_count_must_be_zero")
    if int(first.get("invalid_ohlc_count", 0) or 0) != 0:
        raise CandidateTransitionError("invalid_ohlc_count_must_be_zero")
    if not bool(first.get("frequency_match", False)):
        raise CandidateTransitionError("frequency_mismatch")
    report = first.get("report") or {}
    if not isinstance(report, dict):
        raise CandidateTransitionError("dataset_report_invalid:report_must_be_object")
    if str(report.get("benchmark_mapping_status") or "").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    return payload


def _resolve_strategy_versions(strategy_family: str) -> tuple[str, ...]:
    policy = _resolve_strategy_family_policy(strategy_family)
    versions = policy.strategy_versions
    if not versions:
        raise CandidateTransitionError(f"unknown_strategy_family:{str(strategy_family or '').strip().lower()}")
    return versions


def _build_strategy_family_descriptor(strategy_family: str) -> dict[str, Any]:
    policy = _resolve_strategy_family_policy(strategy_family)
    return {
        "requested_family": str(strategy_family or "").strip().lower(),
        "resolved_family": policy.alias_of or policy.family,
        "alias_of": policy.alias_of,
        "supported": policy.supported,
        "direction_mode": policy.direction_mode,
        "requires_benchmark": policy.requires_benchmark,
        "requires_strict_risk_profile": policy.requires_strict_risk_profile,
        "strategy_versions": list(policy.strategy_versions),
        "notes": policy.notes,
    }


def _load_bars(path: Path, *, symbol: str, timeframe: str) -> list[Any]:
    feed = BacktestDataFeed()
    bars = feed.load_csv(path, symbol=symbol)
    if not bars:
        raise BacktestDataError(f"empty_dataset:{path.name}")
    inferred_frequency = feed.infer_frequency(bars)
    if str(timeframe or "").strip().lower() == "daily" and inferred_frequency != "daily":
        raise BacktestDataError("frequency_mismatch")
    if str(timeframe or "").strip().lower() != "daily" and inferred_frequency == "daily":
        raise BacktestDataError("frequency_mismatch")
    return bars


def _collect_backtest_result_rows(result) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in result.comparison:
        rows.append(
            {
                "version": row.get("version"),
                "strategy": row.get("strategy"),
                "trade_count_definition": row.get("trade_count_definition"),
                "order_count": row.get("order_count"),
                "fill_count": row.get("fill_count"),
                "closed_trade_count": row.get("closed_trade_count"),
                "open_position_count": row.get("open_position_count"),
                "open_position_quantity": row.get("open_position_quantity"),
                "winning_trade_count": row.get("winning_trade_count"),
                "losing_trade_count": row.get("losing_trade_count"),
                "win_rate": row.get("win_rate"),
                "profit_factor": row.get("profit_factor"),
                "profit_factor_status": row.get("profit_factor_status"),
                "total_return": row.get("total_return"),
                "max_drawdown": row.get("max_drawdown"),
                "sharpe": row.get("sharpe"),
                "sortino": row.get("sortino"),
                "calmar": row.get("calmar"),
                "turnover": row.get("turnover"),
                "exposure": row.get("exposure"),
                "commission": row.get("total_commission"),
                "slippage": row.get("total_slippage"),
                "spread_cost": row.get("spread_cost"),
                "blocked_by_trend_count": row.get("blocked_by_trend_count"),
                "blocked_by_cost_count": row.get("blocked_by_cost_count"),
                "blocked_by_inventory_count": row.get("blocked_by_inventory_count"),
                "time_stop_evaluation_count": row.get("time_stop_evaluation_count"),
                "time_stop_signal_count": row.get("time_stop_signal_count"),
                "time_stop_exit_count": row.get("time_stop_exit_count"),
                "time_stop_blocked_count": row.get("time_stop_blocked_count"),
                "reconciliation_status": row.get("reconciliation_status"),
                "reconciliation_difference": row.get("reconciliation_difference"),
                "evidence_status": row.get("evidence_status"),
                "profitability_status": row.get("profitability_status"),
                "deployment_status": row.get("deployment_status"),
                "benchmark_status": row.get("benchmark_status"),
            }
        )
    return rows


def _verify_backtest_artifacts(result_dir: Path, versions: tuple[str, ...]) -> None:
    required = [
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
    missing: list[str] = []
    for name in required:
        if not (result_dir / name).exists():
            missing.append(name)
    for version in versions:
        for name in (f"trades_{version}.csv", f"orders_{version}.csv", f"equity_{version}.csv", f"drawdown_{version}.csv", f"rejected_{version}.csv"):
            if not (result_dir / name).exists():
                missing.append(name)
    if missing:
        raise CandidateTransitionError(f"backtest_artifacts_missing:{','.join(missing)}")


def run_candidate_backtest(
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
    candidate = _load_candidate(store, candidate_id)
    if candidate.validation_status != ValidationStatus.PENDING_BACKTEST.value:
        raise CandidateTransitionError("candidate_must_be_pending_backtest")
    if not operator or not str(operator).strip():
        raise CandidateTransitionError("operator_required")
    if not reason or not str(reason).strip():
        raise CandidateTransitionError("reason_required")
    strategy_policy = _resolve_strategy_family_policy(candidate.strategy_family)
    admission_errors = _validate_backtest_candidate(candidate, policy=strategy_policy)
    if admission_errors:
        raise CandidateTransitionError(", ".join(admission_errors))

    admission_audit_path, dataset_report_path = _resolve_backtest_admission_paths(candidate, store)
    dataset_report = _validate_dataset_report(candidate, dataset_report_path)
    validations = dataset_report.get("validations") or []
    benchmark_symbol = ""
    if isinstance(validations, list) and validations:
        first_validation = validations[0] if isinstance(validations[0], dict) else {}
        benchmark_symbol = str(first_validation.get("benchmark") or "").strip()
    if not benchmark_symbol and candidate.benchmarks:
        benchmark_symbol = str(candidate.benchmarks[0]).strip()
    if not benchmark_symbol:
        raise CandidateTransitionError("benchmark_required")
    if candidate.benchmarks and benchmark_symbol not in candidate.benchmarks:
        raise CandidateTransitionError("benchmark_symbol_mismatch")
    strategy_versions = strategy_policy.strategy_versions
    resolved_timeframe = str(candidate.timeframe or "15m").strip().lower() or "15m"
    symbol_path = _find_local_data_file(DEFAULT_DATA_ROOT, candidate.symbol, resolved_timeframe)
    benchmark_path = _find_local_data_file(DEFAULT_DATA_ROOT, benchmark_symbol, resolved_timeframe)
    if symbol_path is None or benchmark_path is None:
        raise CandidateTransitionError("missing_local_backtest_data")
    resolved_dataset_paths = {
        "symbol": str(symbol_path),
        "benchmark": str(benchmark_path),
        "timeframe": resolved_timeframe,
    }
    output_root = Path(output_dir).expanduser() / _safe_component(candidate.candidate_id)
    run_dir = output_root
    now = _utc_now_iso()
    summary_result: dict[str, Any] | None = None
    backtest_result_path: Path | None = None
    applied = False
    success = False
    failure_reason = ""
    failure_stage = "precheck"
    comparison_result = None

    if dry_run:
        return {
            "candidate_id": candidate.candidate_id,
            "current_status": candidate.validation_status,
            "proposed_action": "run_offline_backtest",
            "resolved_symbol": candidate.symbol,
            "resolved_benchmarks": list(candidate.benchmarks),
            "resolved_strategy_family": candidate.strategy_family,
            "strategy_family_policy": _build_strategy_family_descriptor(candidate.strategy_family),
            "resolved_timeframe": resolved_timeframe,
            "resolved_dataset_paths": resolved_dataset_paths,
            "output_dir": str(output_root),
            "applied": False,
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "dataset_report_path": str(dataset_report_path),
            "admission_audit_path": str(admission_audit_path),
            "strategy_versions": list(strategy_versions),
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
            "quote_api_used": False,
            "started_at": now,
            "completed_at": now,
        }

    try:
        failure_stage = "artifact_verification"
        output_root.mkdir(parents=True, exist_ok=True)
        bars = _load_bars(symbol_path, symbol=candidate.symbol, timeframe=resolved_timeframe)
        benchmark_bars = _load_bars(benchmark_path, symbol=benchmark_symbol, timeframe=resolved_timeframe)
        benchmark_status = str((dataset_report.get("overall") or {}).get("benchmark_status") or "VALID").upper()
        if benchmark_status != "VALID":
            raise CandidateTransitionError("benchmark_status_invalid")
        failure_stage = "backtest_execution"
        comparison_result = compare_versions(
            bars,
            symbol=candidate.symbol,
            benchmark_bars=benchmark_bars,
            versions=strategy_versions,
            initial_cash=10_000.0,
            output_dir=output_root,
            scenario_name=f"candidate:{candidate.candidate_id}",
            minimum_trade_count_for_ranking=20,
        )
        backtest_result_path = output_root / comparison_result.run_id
        failure_stage = "artifact_verification"
        _verify_backtest_artifacts(backtest_result_path, strategy_versions)
        comparison_summary = comparison_result.summary or {}
        all_rows = _collect_backtest_result_rows(comparison_result)
        if not all_rows:
            raise CandidateTransitionError("backtest_result_empty")
        if any(str(row.get("reconciliation_status") or "").upper() != "OK" for row in all_rows):
            raise CandidateTransitionError("reconciliation_failed")
        if any(str(row.get("benchmark_status") or "").upper() != "VALID" for row in all_rows):
            raise CandidateTransitionError("benchmark_status_invalid")
        success = True
        summary_result = {
            "candidate_id": candidate.candidate_id,
            "previous_status": candidate.validation_status,
            "proposed_status": ValidationStatus.BACKTEST_COMPLETE.value,
            "final_status": ValidationStatus.BACKTEST_COMPLETE.value,
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "started_at": now,
            "completed_at": _utc_now_iso(),
            "symbol": candidate.symbol,
            "benchmarks": list(candidate.benchmarks),
            "timeframe": resolved_timeframe,
            "strategy_family": candidate.strategy_family,
            "strategy_family_policy": _build_strategy_family_descriptor(candidate.strategy_family),
            "strategy_versions": list(strategy_versions),
            "dataset_report_path": str(dataset_report_path),
            "admission_audit_path": str(admission_audit_path),
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "quote_api_used": False,
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
            "applied": True,
            "success": True,
            "failure_reason": "",
            "output_dir": str(output_root),
            "backtest_run_dir": str(backtest_result_path),
            "comparison_summary": comparison_summary,
            "backtest_metrics": all_rows,
            "evidence_status": comparison_summary.get("evidence_status") or "INSUFFICIENT_EVIDENCE",
            "profitability_status": comparison_summary.get("profitability_status") or "INELIGIBLE",
            "deployment_status": comparison_summary.get("deployment_status") or "INELIGIBLE",
            "reconciliation_status": "OK",
        }
        audit_path = output_root / "backtest_run_audit.json"
        summary_csv_path = output_root / "backtest_run_summary.csv"
        _atomic_write_text(audit_path, json.dumps(_jsonable(summary_result), indent=2, ensure_ascii=False))
        _atomic_write_csv(
            summary_csv_path,
            [
                {
                    "candidate_id": candidate.candidate_id,
                    "previous_status": candidate.validation_status,
                    "final_status": ValidationStatus.BACKTEST_COMPLETE.value,
                    "operator": str(operator).strip(),
                    "reason": str(reason).strip(),
                    "started_at": now,
                    "completed_at": summary_result["completed_at"],
                    "symbol": candidate.symbol,
                    "benchmarks": "|".join(candidate.benchmarks),
                "timeframe": resolved_timeframe,
                "strategy_family": candidate.strategy_family,
                "strategy_family_policy": _build_strategy_family_descriptor(candidate.strategy_family),
                "strategy_versions": list(strategy_versions),
                "dataset_report_path": str(dataset_report_path),
                "admission_audit_path": str(admission_audit_path),
                "backtest_engine_version": BACKTEST_ENGINE_VERSION,
                    "quote_api_used": False,
                    "trade_api_used": False,
                    "broker_used": False,
                    "trade_context_initialized": False,
                    "applied": True,
                    "success": True,
                    "failure_reason": "",
                }
            ],
        )
        updated = store.transition(
            candidate.candidate_id,
            ValidationStatus.BACKTEST_COMPLETE,
            reason=str(reason).strip(),
            metadata={
                "backtest_complete": True,
                "operator": str(operator).strip(),
                "reason": str(reason).strip(),
                "previous_status": candidate.validation_status,
                "backtest_engine_version": BACKTEST_ENGINE_VERSION,
                "started_at": now,
                "completed_at": summary_result["completed_at"],
                "symbol": candidate.symbol,
                "benchmarks": list(candidate.benchmarks),
                "timeframe": resolved_timeframe,
                "strategy_family": candidate.strategy_family,
                "strategy_family_policy": _build_strategy_family_descriptor(candidate.strategy_family),
                "dataset_report_path": str(dataset_report_path),
                "admission_audit_path": str(admission_audit_path),
                "backtest_run_path": str(backtest_result_path),
                "quote_api_used": False,
                "trade_api_used": False,
                "broker_used": False,
                "trade_context_initialized": False,
                "applied": True,
                "success": True,
                "failure_reason": "",
                "evidence_status": summary_result["evidence_status"],
                "profitability_status": summary_result["profitability_status"],
                "deployment_status": summary_result["deployment_status"],
                "reconciliation_status": "OK",
                "backtest_summary_path": str(summary_csv_path),
                "backtest_audit_path": str(audit_path),
            },
        )
        summary_result["candidate_current_status"] = updated.validation_status
        summary_result["current_status_after"] = updated.validation_status
        summary_result["candidate_metadata"] = dict(updated.metadata or {})
        _atomic_write_text(audit_path, json.dumps(_jsonable(summary_result), indent=2, ensure_ascii=False))
        return summary_result
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}:{exc}"
        exception_type = type(exc).__name__
        audit_path = output_root / "backtest_run_audit.json"
        summary_csv_path = output_root / "backtest_run_summary.csv"
        failure_payload = {
            "candidate_id": candidate.candidate_id,
            "previous_status": candidate.validation_status,
            "proposed_status": ValidationStatus.BACKTEST_FAILED.value,
            "final_status": ValidationStatus.BACKTEST_FAILED.value,
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "started_at": now,
            "completed_at": _utc_now_iso(),
            "symbol": candidate.symbol,
            "benchmarks": list(candidate.benchmarks),
            "timeframe": resolved_timeframe,
            "strategy_family": candidate.strategy_family,
            "strategy_family_policy": _build_strategy_family_descriptor(candidate.strategy_family),
            "strategy_versions": list(strategy_versions),
            "dataset_report_path": str(dataset_report_path),
            "admission_audit_path": str(admission_audit_path),
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "quote_api_used": False,
            "trade_api_used": False,
            "broker_used": False,
            "trade_context_initialized": False,
            "applied": True,
            "success": False,
            "failure_reason": failure_reason,
            "failure_stage": failure_stage,
            "exception_type": exception_type,
            "partial_artifacts": {
                "backtest_run_dir": str(backtest_result_path) if backtest_result_path else None,
                "audit_path": str(audit_path),
                "summary_path": str(summary_csv_path),
            },
            "output_dir": str(output_root),
            "backtest_run_dir": str(backtest_result_path) if backtest_result_path else None,
            "reconciliation_status": "FAILED",
        }
        transitioned = None
        try:
            _atomic_write_text(audit_path, json.dumps(_jsonable(failure_payload), indent=2, ensure_ascii=False))
            _atomic_write_csv(
                summary_csv_path,
                [
                    {
                        "candidate_id": candidate.candidate_id,
                        "previous_status": candidate.validation_status,
                        "final_status": ValidationStatus.BACKTEST_FAILED.value,
                        "operator": str(operator).strip(),
                        "reason": str(reason).strip(),
                        "started_at": now,
                        "completed_at": failure_payload["completed_at"],
                        "symbol": candidate.symbol,
                        "benchmarks": "|".join(candidate.benchmarks),
                        "timeframe": resolved_timeframe,
                        "strategy_family": candidate.strategy_family,
                        "strategy_family_policy": _build_strategy_family_descriptor(candidate.strategy_family),
                        "strategy_versions": list(strategy_versions),
                        "dataset_report_path": str(dataset_report_path),
                        "admission_audit_path": str(admission_audit_path),
                        "backtest_engine_version": BACKTEST_ENGINE_VERSION,
                        "quote_api_used": False,
                        "trade_api_used": False,
                        "broker_used": False,
                        "trade_context_initialized": False,
                        "applied": True,
                        "success": False,
                        "failure_reason": failure_reason,
                        "failure_stage": failure_stage,
                    }
                ],
            )
            transitioned = store.transition(
                candidate.candidate_id,
                ValidationStatus.BACKTEST_FAILED,
                reason=failure_reason,
                metadata={
                    "operator": str(operator).strip(),
                    "reason": str(reason).strip(),
                    "failure_reason": failure_reason,
                    "previous_status": candidate.validation_status,
                    "backtest_engine_version": BACKTEST_ENGINE_VERSION,
                    "started_at": now,
                    "completed_at": failure_payload["completed_at"],
                    "symbol": candidate.symbol,
                    "benchmarks": list(candidate.benchmarks),
                    "timeframe": resolved_timeframe,
                    "strategy_family": candidate.strategy_family,
                    "dataset_report_path": str(dataset_report_path),
                    "admission_audit_path": str(admission_audit_path),
                    "backtest_run_path": str(backtest_result_path) if backtest_result_path else None,
                    "quote_api_used": False,
                    "trade_api_used": False,
                    "broker_used": False,
                    "trade_context_initialized": False,
                    "applied": True,
                    "success": False,
                    "reconciliation_status": "FAILED",
                    "failure_stage": failure_stage,
                    "backtest_summary_path": str(summary_csv_path),
                    "backtest_audit_path": str(audit_path),
                },
            )
        except Exception:
            pass
        if transitioned is not None:
            failure_payload["candidate_current_status"] = transitioned.validation_status
            failure_payload["current_status_after"] = transitioned.validation_status
            failure_payload["candidate_metadata"] = dict(transitioned.metadata or {})
            try:
                _atomic_write_text(audit_path, json.dumps(_jsonable(failure_payload), indent=2, ensure_ascii=False))
            except Exception:
                pass
            return failure_payload
        if isinstance(exc, CandidateTransitionError):
            raise
        raise CandidateTransitionError(failure_reason) from exc
