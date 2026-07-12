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
DEFAULT_VALIDATION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "validation"
DEFAULT_ADMISSION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "admission"
ADMISSION_VERSION = "manual_backtest_admission_v1"
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


def _candidate_admission_dir(candidate_id: str) -> Path:
    return DEFAULT_ADMISSION_ROOT / _safe_component(candidate_id)


def _candidate_status_name(candidate: CandidateRecord) -> str:
    return str(candidate.validation_status or "").strip().upper()


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


def _validate_admission_candidate(candidate: CandidateRecord) -> list[str]:
    errors: list[str] = []
    if not _candidate_flags_false(candidate):
        errors.append("candidate_write_flag_enabled")
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


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CandidateTransitionError(f"dataset_report_invalid:{path.name}:{exc}") from exc
    if not isinstance(payload, dict):
        raise CandidateTransitionError(f"dataset_report_invalid:{path.name}:root_must_be_object")
    return payload


def _metadata_path_candidates(candidate: CandidateRecord) -> list[str]:
    metadata = dict(candidate.metadata or {})
    candidates: list[str] = []
    for key in (
        "dataset_validation_report_path",
        "validation_report_path",
        "report_path",
    ):
        value = metadata.get(key)
        if value:
            candidates.append(str(value))
    return candidates


def _resolve_existing_report_path(candidate: CandidateRecord, store: CandidateValidationStore) -> Path:
    candidate_id = _safe_component(candidate.candidate_id)
    default_names = [
        "dataset_validation_report.json",
        "validation_report.json",
        "report.json",
    ]
    search_roots = [
        DEFAULT_VALIDATION_ROOT,
        DEFAULT_VALIDATION_ROOT / candidate_id,
        PROJECT_DIR / "artifacts" / "candidates" / "validation",
        PROJECT_DIR / "artifacts" / "candidates" / "validation" / candidate_id,
        store.root_dir.parent / "validation",
        store.root_dir.parent / "validation" / candidate_id,
    ]
    for raw_path in _metadata_path_candidates(candidate):
        path = Path(raw_path)
        candidates_to_try = []
        if path.is_absolute():
            candidates_to_try.append(path)
        else:
            candidates_to_try.append(path)
            candidates_to_try.append(DEFAULT_VALIDATION_ROOT / path)
            candidates_to_try.append(DEFAULT_VALIDATION_ROOT / candidate_id / path.name)
            candidates_to_try.append(PROJECT_DIR / path)
            candidates_to_try.append(PROJECT_DIR / "artifacts" / "candidates" / "validation" / path)
            candidates_to_try.append(PROJECT_DIR / "artifacts" / "candidates" / "validation" / candidate_id / path.name)
            candidates_to_try.append(store.root_dir.parent / path)
            candidates_to_try.append(store.root_dir.parent / "validation" / path)
            candidates_to_try.append(store.root_dir.parent / "validation" / candidate_id / path.name)
        for candidate_path in candidates_to_try:
            if candidate_path.exists():
                return candidate_path
    for root in search_roots:
        for name in default_names:
            candidate_path = root / name
            if candidate_path.exists():
                return candidate_path
    raise CandidateTransitionError("dataset_report_missing")


def _validate_dataset_report(candidate: CandidateRecord, store: CandidateValidationStore) -> tuple[dict[str, Any], Path, Path]:
    report_path = _resolve_existing_report_path(candidate, store)
    payload = _load_json_file(report_path)
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
    benchmark_valid = bool(overall.get("benchmark_valid", False))
    benchmark_status = str(overall.get("benchmark_status") or ("VALID" if benchmark_valid else "")).strip().upper()
    if not benchmark_valid or benchmark_status != "VALID":
        raise CandidateTransitionError("benchmark_status_invalid")
    if not bool(overall.get("eligible_for_backtest", False)):
        raise CandidateTransitionError("eligible_for_backtest_required")
    if bool(overall.get("future_data_risk", True)):
        raise CandidateTransitionError("future_data_risk_must_be_false")
    reconciliation_status = str(overall.get("reconciliation_status") or "").strip().upper()
    if reconciliation_status not in {"OK", "NOT_APPLICABLE"}:
        raise CandidateTransitionError("reconciliation_must_be_ok_or_not_applicable")

    validations = payload.get("validations") or []
    if not isinstance(validations, list) or not validations:
        raise CandidateTransitionError("dataset_report_missing_validations")
    for entry in validations:
        if not isinstance(entry, dict):
            raise CandidateTransitionError("dataset_report_invalid_validation_entry")
        if str(entry.get("benchmark_status") or "").strip().upper() != "VALID":
            raise CandidateTransitionError("benchmark_status_invalid")
        if not bool(entry.get("eligible_for_backtest", False)):
            raise CandidateTransitionError("eligible_for_backtest_required")
        if bool(entry.get("future_data_risk", True)):
            raise CandidateTransitionError("future_data_risk_must_be_false")
        if int(entry.get("duplicate_count", 0) or 0) != 0:
            raise CandidateTransitionError("duplicate_count_must_be_zero")
        if int(entry.get("invalid_ohlc_count", 0) or 0) != 0:
            raise CandidateTransitionError("invalid_ohlc_count_must_be_zero")
        if not bool(entry.get("frequency_match", False)):
            raise CandidateTransitionError("frequency_mismatch")

    summary_path = report_path.with_name("dataset_validation_summary.csv")
    return payload, report_path, summary_path


def admit_candidate_to_backtest(
    *,
    candidate_id: str,
    candidate_store: str | Path,
    dry_run: bool = True,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    _ensure_no_parent_refs(candidate_store, label="candidate_store")
    store = CandidateValidationStore(Path(candidate_store).expanduser())
    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise CandidateTransitionError(f"candidate_not_found:{candidate_id}")
    if _candidate_status_name(candidate) != ValidationStatus.DATA_VALID.value:
        raise CandidateTransitionError("candidate_must_be_data_valid")
    if not operator or not str(operator).strip():
        raise CandidateTransitionError("operator_required")
    if not reason or not str(reason).strip():
        raise CandidateTransitionError("reason_required")
    admission_errors = _validate_admission_candidate(candidate)
    if admission_errors:
        raise CandidateTransitionError(", ".join(admission_errors))

    report_payload, report_path, summary_path = _validate_dataset_report(candidate, store)
    now = _utc_now_iso()
    admission_dir = _candidate_admission_dir(candidate.candidate_id)
    admission_dir.mkdir(parents=True, exist_ok=True)
    audit_path = admission_dir / "backtest_admission_audit.json"
    summary_csv_path = admission_dir / "backtest_admission_summary.csv"

    validations = report_payload.get("validations") or []
    first_validation = validations[0] if validations else {}
    overall = report_payload.get("overall") or {}
    proposed_status = ValidationStatus.PENDING_BACKTEST.value
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
        "dataset_report_path": str(report_path),
        "dataset_summary_path": str(summary_path),
        "dataset_status": str(overall.get("status") or ValidationStatus.DATA_VALID.value),
        "benchmark_status": str(first_validation.get("benchmark_status") or "VALID"),
        "eligible_for_backtest": bool(first_validation.get("eligible_for_backtest", False)),
        "future_data_risk": bool(first_validation.get("future_data_risk", False)),
        "duplicate_count": int(first_validation.get("duplicate_count", 0) or 0),
        "invalid_ohlc_count": int(first_validation.get("invalid_ohlc_count", 0) or 0),
        "all_trading_flags_false": _candidate_flags_false(candidate),
        "trade_api_used": False,
        "broker_used": False,
        "quote_api_used": False,
        "validator_version": ADMISSION_VERSION,
        "report_path": str(audit_path),
        "summary_path": str(summary_csv_path),
        "candidate_metadata": dict(candidate.metadata or {}),
    }

    audit_payload = {
        **result,
        "validation_result": "APPROVED" if result["applied"] else "DRY_RUN",
        "previous_status": candidate.validation_status,
        "new_status": proposed_status,
        "current_status_after": candidate.validation_status if dry_run else proposed_status,
        "candidate_status_after": candidate.validation_status if dry_run else proposed_status,
        "dataset_report": report_payload,
    }

    summary_rows = [
        {
            "candidate_id": candidate.candidate_id,
            "previous_status": candidate.validation_status,
            "proposed_status": proposed_status,
            "applied": bool(not dry_run),
            "operator": str(operator).strip(),
            "reason": str(reason).strip(),
            "dataset_report_path": str(report_path),
            "dataset_status": str(overall.get("status") or ValidationStatus.DATA_VALID.value),
            "benchmark_status": str(first_validation.get("benchmark_status") or "VALID"),
            "eligible_for_backtest": bool(first_validation.get("eligible_for_backtest", False)),
            "all_trading_flags_false": _candidate_flags_false(candidate),
            "trade_api_used": False,
            "broker_used": False,
            "created_at": now,
        }
    ]

    _atomic_write_text(audit_path, json.dumps(_jsonable(audit_payload), indent=2, ensure_ascii=False))
    _atomic_write_csv(summary_csv_path, summary_rows)

    if not dry_run:
        updated = store.transition(
            candidate.candidate_id,
            ValidationStatus.PENDING_BACKTEST,
            reason=str(reason).strip(),
            metadata={
                "data_status": ValidationStatus.DATA_VALID.value,
                "operator": str(operator).strip(),
                "reason": str(reason).strip(),
                "admitted_at": now,
                "operator_action": "manual_backtest_admission",
                "dataset_report_path": str(report_path),
                "dataset_summary_path": str(summary_path),
                "dataset_status": str(overall.get("status") or ValidationStatus.DATA_VALID.value),
                "benchmark_status": str(first_validation.get("benchmark_status") or "VALID"),
                "eligible_for_backtest": bool(first_validation.get("eligible_for_backtest", False)),
                "future_data_risk": bool(first_validation.get("future_data_risk", False)),
                "duplicate_count": int(first_validation.get("duplicate_count", 0) or 0),
                "invalid_ohlc_count": int(first_validation.get("invalid_ohlc_count", 0) or 0),
                "all_trading_flags_false": _candidate_flags_false(candidate),
                "trade_api_used": False,
                "broker_used": False,
                "quote_api_used": False,
                "validator_version": ADMISSION_VERSION,
                "backtest_admission_audit_path": str(audit_path),
                "backtest_admission_summary_path": str(summary_csv_path),
                "validation_report_path": str(report_path),
                "validation_summary_path": str(summary_path),
                "backtest_admission_report_path": str(audit_path),
            },
        )
        result["candidate_current_status"] = updated.validation_status
        result["current_status_after"] = updated.validation_status
        result["candidate_status_after"] = updated.validation_status
        result["applied"] = True
        result["admitted_at"] = now
        result["candidate_metadata"] = dict(updated.metadata or {})
        audit_payload["applied"] = True
        audit_payload["validation_result"] = "APPROVED"
        audit_payload["candidate_status_after"] = updated.validation_status
        _atomic_write_text(audit_path, json.dumps(_jsonable(audit_payload), indent=2, ensure_ascii=False))
    return result
