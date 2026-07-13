from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    ALLOWED_SYMBOL_CLASSES,
    ALLOWED_TIMEFRAMES,
    CandidateRecord,
    CandidateTransitionError,
    ValidationStatus,
)
from .store import CandidateValidationStore


PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
DEFAULT_BACKTEST_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "backtests"
DEFAULT_WALK_FORWARD_ADMISSION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "walk_forward_admission"
WALK_FORWARD_ADMISSION_VERSION = "manual_walk_forward_admission_v1"
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


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


def _resolve_candidate_store_root(candidate_store: str | Path) -> Path:
    path = Path(candidate_store).expanduser()
    if path.exists() and path.is_file():
        return path.parent
    if path.name == "candidates.jsonl":
        return path.parent
    return path


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CandidateTransitionError(f"backtest_audit_invalid:{path.name}:{exc}") from exc
    if not isinstance(payload, dict):
        raise CandidateTransitionError(f"backtest_audit_invalid:{path.name}:root_must_be_object")
    return payload


def _load_csv_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as exc:
        raise CandidateTransitionError(f"backtest_csv_invalid:{path.name}:{exc}") from exc


def _metadata_path_candidates(candidate: CandidateRecord) -> list[str]:
    metadata = dict(candidate.metadata or {})
    candidates: list[str] = []
    for key in (
        "backtest_audit_path",
        "backtest_run_audit_path",
        "backtest_run_path",
        "backtest_summary_path",
        "backtest_run_summary_path",
        "walk_forward_source_audit_path",
    ):
        value = metadata.get(key)
        if value:
            candidates.append(str(value))
    return candidates


def _resolve_existing_backtest_audit_path(candidate: CandidateRecord, store: CandidateValidationStore) -> Path:
    candidate_id = _safe_component(candidate.candidate_id)
    search_roots = [
        DEFAULT_BACKTEST_ROOT,
        DEFAULT_BACKTEST_ROOT / candidate_id,
        PROJECT_DIR / "artifacts" / "candidates" / "backtests",
        PROJECT_DIR / "artifacts" / "candidates" / "backtests" / candidate_id,
        store.root_dir.parent / "backtests",
        store.root_dir.parent / "backtests" / candidate_id,
    ]
    default_names = ["backtest_run_audit.json", "backtest_audit.json"]
    for raw_path in _metadata_path_candidates(candidate):
        path = Path(raw_path)
        candidates_to_try: list[Path] = []
        if path.is_absolute():
            candidates_to_try.append(path)
        else:
            candidates_to_try.extend(
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
        if path.is_dir():
            candidates_to_try.append(path / "backtest_run_audit.json")
            candidates_to_try.append(path / "backtest_audit.json")
        for candidate_path in candidates_to_try:
            if candidate_path.exists() and candidate_path.is_file():
                return candidate_path
    for root in search_roots:
        for name in default_names:
            candidate_path = root / name
            if candidate_path.exists() and candidate_path.is_file():
                return candidate_path
    raise CandidateTransitionError("backtest_audit_missing")


def _resolve_backtest_run_dir(candidate: CandidateRecord, audit_payload: dict[str, Any], audit_path: Path) -> Path:
    candidates: list[Path] = []
    for raw in (
        audit_payload.get("backtest_run_dir"),
        audit_payload.get("backtest_run_path"),
        audit_payload.get("output_dir"),
    ):
        if raw:
            path = Path(str(raw))
            candidates.append(path)
            if path.is_file():
                candidates.append(path.parent)
            if path.name in {"backtest_run_audit.json", "backtest_audit.json"}:
                candidates.append(path.parent)
            if path.suffix == ".json":
                candidates.append(path.parent)
    candidates.append(audit_path.parent)
    for path in candidates:
        if path.exists():
            if path.is_file():
                return path.parent
            return path
    raise CandidateTransitionError("backtest_run_dir_missing")


def _candidate_is_backtest_complete(candidate: CandidateRecord) -> bool:
    return str(candidate.validation_status or "").strip().upper() == ValidationStatus.BACKTEST_COMPLETE.value


def _validate_candidate(candidate: CandidateRecord) -> list[str]:
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
    if not str(candidate.timeframe or "").strip() or str(candidate.timeframe).strip().lower() not in ALLOWED_TIMEFRAMES:
        errors.append("invalid_timeframe")
    if not str(candidate.asset_type or "").strip() or str(candidate.asset_type).strip().lower() not in ALLOWED_SYMBOL_CLASSES:
        errors.append("invalid_asset_type")
    if candidate.is_leveraged_or_inverse() and candidate.risk_profile not in {"strict", "very_strict"}:
        errors.append("missing_or_weak_risk_profile")
    return errors


def _validate_backtest_audit(
    candidate: CandidateRecord,
    store: CandidateValidationStore,
) -> tuple[dict[str, Any], Path, Path, list[dict[str, Any]], dict[str, Any], list[str]]:
    audit_path = _resolve_existing_backtest_audit_path(candidate, store)
    audit_payload = _load_json_file(audit_path)
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

    summary = audit_payload.get("comparison_summary")
    if not isinstance(summary, dict):
        raise CandidateTransitionError("comparison_summary_missing")
    if str(summary.get("benchmark_status") or "").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if str(summary.get("reconciliation_status") or "").strip().upper() != "OK":
        raise CandidateTransitionError("reconciliation_must_be_ok")

    metrics = audit_payload.get("backtest_metrics")
    if not isinstance(metrics, list) or not metrics:
        raise CandidateTransitionError("backtest_metrics_missing")
    versions = []
    for row in metrics:
        if not isinstance(row, dict):
            raise CandidateTransitionError("backtest_metrics_invalid")
        version = str(row.get("version") or "").strip().lower()
        if version:
            versions.append(version)
    unique_versions = sorted(set(versions))
    required_versions = ["a", "b", "baseline", "c"]
    if not all(item in unique_versions for item in required_versions):
        raise CandidateTransitionError("backtest_versions_incomplete")
    if len(unique_versions) < 4:
        raise CandidateTransitionError("backtest_versions_incomplete")

    backtest_run_dir = _resolve_backtest_run_dir(candidate, audit_payload, audit_path)
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
    missing: list[str] = [name for name in required_files if not (backtest_run_dir / name).exists()]
    for version in required_versions:
        for name in (f"trades_{version}.csv", f"orders_{version}.csv", f"equity_{version}.csv", f"drawdown_{version}.csv", f"rejected_{version}.csv"):
            if not (backtest_run_dir / name).exists():
                missing.append(name)
    if missing:
        raise CandidateTransitionError(f"backtest_artifacts_missing:{','.join(missing)}")

    comparison_summary_path = backtest_run_dir / "comparison_summary.json"
    comparison_summary = _load_json_file(comparison_summary_path)
    if str(comparison_summary.get("benchmark_status") or "").strip().upper() != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if str(comparison_summary.get("reconciliation_status") or "").strip().upper() != "OK":
        raise CandidateTransitionError("reconciliation_must_be_ok")
    strategy_metrics_path = backtest_run_dir / "strategy_metrics.csv"
    strategy_metrics = _load_csv_rows(strategy_metrics_path)
    if not strategy_metrics:
        raise CandidateTransitionError("strategy_metrics_missing")
    strategy_ranking_path = backtest_run_dir / "strategy_ranking.csv"
    strategy_ranking = _load_csv_rows(strategy_ranking_path)
    if not strategy_ranking:
        raise CandidateTransitionError("strategy_ranking_missing")

    admission_audit_path = None
    for key in ("backtest_admission_audit_path", "admission_audit_path"):
        raw = candidate.metadata.get(key) if candidate.metadata else None
        if raw:
            path = Path(str(raw))
            if path.exists():
                admission_audit_path = path
                break
            candidates_to_try = [
                DEFAULT_BACKTEST_ROOT / candidate.candidate_id / path.name,
                PROJECT_DIR / "artifacts" / "candidates" / "admission" / candidate.candidate_id / path.name,
                store.root_dir.parent / "admission" / candidate.candidate_id / path.name,
            ]
            for candidate_path in candidates_to_try:
                if candidate_path.exists():
                    admission_audit_path = candidate_path
                    break
        if admission_audit_path:
            break
    if admission_audit_path is None or not admission_audit_path.exists():
        raise CandidateTransitionError("backtest_admission_audit_missing")
    admission_payload = _load_json_file(admission_audit_path)
    if str(admission_payload.get("applied") if "applied" in admission_payload else True).lower() not in {"true", "1"}:
        raise CandidateTransitionError("backtest_admission_not_applied")

    return audit_payload, audit_path, backtest_run_dir, strategy_metrics, comparison_summary, unique_versions


def admit_candidate_to_walk_forward(
    *,
    candidate_id: str,
    candidate_store: str | Path,
    dry_run: bool = True,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    _ensure_no_parent_refs(candidate_store, label="candidate_store")
    store = CandidateValidationStore(_resolve_candidate_store_root(candidate_store))
    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise CandidateTransitionError(f"candidate_not_found:{candidate_id}")
    if not _candidate_is_backtest_complete(candidate):
        raise CandidateTransitionError("candidate_must_be_backtest_complete")
    if not operator or not str(operator).strip():
        raise CandidateTransitionError("operator_required")
    if not reason or not str(reason).strip():
        raise CandidateTransitionError("reason_required")
    admission_errors = _validate_candidate(candidate)
    if admission_errors:
        raise CandidateTransitionError(", ".join(admission_errors))

    audit_payload, audit_path, backtest_run_dir, strategy_metrics, comparison_summary, completed_versions = _validate_backtest_audit(candidate, store)
    now = _utc_now_iso()
    admission_dir = DEFAULT_WALK_FORWARD_ADMISSION_ROOT / _safe_component(candidate.candidate_id)
    admission_dir.mkdir(parents=True, exist_ok=True)
    audit_output_path = admission_dir / "walk_forward_admission_audit.json"
    summary_csv_path = admission_dir / "walk_forward_admission_summary.csv"

    evidence_status = str(audit_payload.get("evidence_status") or comparison_summary.get("evidence_status") or "").strip() or "INSUFFICIENT_EVIDENCE"
    profitability_status = str(audit_payload.get("profitability_status") or comparison_summary.get("profitability_status") or "").strip() or "INELIGIBLE"
    deployment_status = str(audit_payload.get("deployment_status") or comparison_summary.get("deployment_status") or "").strip() or "INELIGIBLE"
    reconciliation_status = str(audit_payload.get("reconciliation_status") or comparison_summary.get("reconciliation_status") or "").strip().upper() or "OK"
    reconciliation_all_ok = reconciliation_status == "OK"

    proposed_status = ValidationStatus.PENDING_WALK_FORWARD.value
    result = {
        "candidate_id": candidate.candidate_id,
        "candidate_current_status": candidate.validation_status,
        "previous_status": candidate.validation_status,
        "proposed_status": proposed_status,
        "new_status": proposed_status,
        "applied": bool(not dry_run),
        "dry_run": bool(dry_run),
        "operator": str(operator).strip(),
        "reason": str(reason).strip(),
        "started_at": now,
        "completed_at": now,
        "admitted_at": now if not dry_run else None,
        "backtest_audit_path": str(audit_path),
        "backtest_run_dir": str(backtest_run_dir),
        "backtest_summary_path": str(backtest_run_dir / "backtest_run_summary.csv"),
        "benchmark_status": str(comparison_summary.get("benchmark_status") or "VALID").strip().upper() or "VALID",
        "reconciliation_all_ok": reconciliation_all_ok,
        "completed_versions": list(completed_versions),
        "evidence_status": evidence_status,
        "profitability_status": profitability_status,
        "deployment_status": deployment_status,
        "all_trading_flags_false": _candidate_flags_false(candidate),
        "trade_api_used": False,
        "broker_used": False,
        "quote_api_used": False,
        "walk_forward_started": False,
        "validator_version": WALK_FORWARD_ADMISSION_VERSION,
        "report_path": str(audit_output_path),
        "summary_path": str(summary_csv_path),
        "candidate_metadata": dict(candidate.metadata or {}),
        "backtest_evidence_summary": {
            "benchmark_status": str(comparison_summary.get("benchmark_status") or "VALID").strip().upper() or "VALID",
            "reconciliation_status": reconciliation_status,
            "completed_versions": list(completed_versions),
            "metrics_rows": len(strategy_metrics),
            "strategy_metrics_rows": len(strategy_metrics),
            "strategy_ranking_rows": len(_load_csv_rows(backtest_run_dir / "strategy_ranking.csv")),
            "comparison_summary": comparison_summary,
        },
    }

    audit_doc = {
        **result,
        "validation_result": "APPROVED" if result["applied"] else "DRY_RUN",
        "candidate_status_after": candidate.validation_status if dry_run else proposed_status,
        "current_status_after": candidate.validation_status if dry_run else proposed_status,
        "backtest_audit": audit_payload,
    }

    summary_rows = [
        {
            "candidate_id": candidate.candidate_id,
            "previous_status": candidate.validation_status,
            "proposed_status": proposed_status,
            "applied": bool(not dry_run),
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "backtest_audit_path": str(audit_path),
            "benchmark_status": str(comparison_summary.get("benchmark_status") or "VALID").strip().upper() or "VALID",
            "reconciliation_all_ok": reconciliation_all_ok,
            "completed_versions": "|".join(completed_versions),
            "evidence_status": evidence_status,
            "profitability_status": profitability_status,
            "deployment_status": deployment_status,
            "trade_api_used": False,
            "broker_used": False,
            "created_at": now,
        }
    ]

    _atomic_write_text(audit_output_path, json.dumps(_jsonable(audit_doc), indent=2, ensure_ascii=False))
    _atomic_write_csv(summary_csv_path, summary_rows)

    if not dry_run:
        updated = store.transition(
            candidate.candidate_id,
            ValidationStatus.PENDING_WALK_FORWARD,
            reason=str(reason).strip(),
            metadata={
                "backtest_status": ValidationStatus.BACKTEST_COMPLETE.value,
                "operator": str(operator).strip(),
                "reason": str(reason).strip(),
                "admitted_at": now,
                "operator_action": "manual_walk_forward_admission",
                "backtest_audit_path": str(audit_path),
                "backtest_run_dir": str(backtest_run_dir),
                "backtest_summary_path": str(backtest_run_dir / "backtest_run_summary.csv"),
                "walk_forward_admission_audit_path": str(audit_output_path),
                "walk_forward_admission_summary_path": str(summary_csv_path),
                "benchmark_status": str(comparison_summary.get("benchmark_status") or "VALID").strip().upper() or "VALID",
                "reconciliation_all_ok": reconciliation_all_ok,
                "completed_versions": list(completed_versions),
                "evidence_status": evidence_status,
                "profitability_status": profitability_status,
                "deployment_status": deployment_status,
                "all_trading_flags_false": _candidate_flags_false(candidate),
                "trade_api_used": False,
                "broker_used": False,
                "quote_api_used": False,
                "walk_forward_started": False,
                "validator_version": WALK_FORWARD_ADMISSION_VERSION,
                "backtest_evidence_summary": {
                    "benchmark_status": str(comparison_summary.get("benchmark_status") or "VALID").strip().upper() or "VALID",
                    "reconciliation_status": reconciliation_status,
                    "completed_versions": list(completed_versions),
                    "metrics_rows": len(strategy_metrics),
                },
            },
        )
        result["candidate_current_status"] = updated.validation_status
        result["current_status_after"] = updated.validation_status
        result["candidate_status_after"] = updated.validation_status
        result["applied"] = True
        result["admitted_at"] = now
        result["candidate_metadata"] = dict(updated.metadata or {})
        audit_doc["applied"] = True
        audit_doc["validation_result"] = "APPROVED"
        audit_doc["candidate_status_after"] = updated.validation_status
        audit_doc["current_status_after"] = updated.validation_status
        _atomic_write_text(audit_output_path, json.dumps(_jsonable(audit_doc), indent=2, ensure_ascii=False))
    return result

