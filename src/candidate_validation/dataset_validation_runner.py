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

from src.backtest.dataset_validation import validate_backtest_dataset

from .models import CandidateRecord, CandidateTransitionError, ValidationStatus
from .store import CandidateValidationStore


PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
DEFAULT_DATA_ROOT = PROJECT_DIR / "data" / "market" / "longbridge"
DEFAULT_VALIDATION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "validation"
VALIDATOR_VERSION = "manual_dataset_validation_v1"
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _safe_component(value: str) -> str:
    text = str(value or "").strip()
    if not text or not _SAFE_COMPONENT_RE.fullmatch(text):
        raise ValueError(f"unsafe_path_component:{text!r}")
    return text


def _candidate_output_dir(base_output_dir: Path, candidate_id: str) -> Path:
    base_output_dir = Path(base_output_dir).expanduser().resolve()
    safe_candidate_id = _safe_component(candidate_id)
    if base_output_dir.name == safe_candidate_id:
        return base_output_dir
    return base_output_dir / safe_candidate_id


def _candidate_store_root(path: str | Path) -> Path:
    store_path = Path(path).expanduser()
    if store_path.suffix.lower() == ".jsonl":
        return store_path.parent
    return store_path


def _ensure_no_parent_refs(path: str | Path, *, label: str) -> None:
    raw = Path(path)
    if any(part == ".." for part in raw.parts):
        raise CandidateTransitionError(f"unsafe_path_parameter:{label}")


def _symbol_slug(symbol: str) -> str:
    return str(symbol or "").strip().upper().replace(".", "_")


def _canonical_symbol(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw.split(".")[0]


def _frequency_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"1d", "daily", "day"}:
        return "daily"
    return raw or "unknown"


def _candidate_file_patterns(symbol: str, frequency: str) -> list[str]:
    slug = _symbol_slug(symbol)
    freq = _frequency_label(frequency)
    patterns = [
        f"{slug}_{freq}.csv",
        f"{slug}_{freq}_latest.csv",
        f"{slug}_{freq}_*.csv",
    ]
    if freq == "daily":
        patterns.extend([f"{slug}_daily*.csv"])
    return patterns


def _find_local_data_file(root: Path, symbol: str, frequency: str) -> Path | None:
    root = Path(root)
    exact_candidates = [
        root / f"{_symbol_slug(symbol)}_{_frequency_label(frequency)}.csv",
        root / f"{_symbol_slug(symbol)}_{_frequency_label(frequency)}_latest.csv",
    ]
    for candidate in exact_candidates:
        if candidate.exists():
            return candidate
    slug = _symbol_slug(symbol)
    freq = _frequency_label(frequency)
    matches = sorted(root.glob(f"{slug}_{freq}*.csv"))
    if matches:
        return matches[0]
    return None


def _count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def _relativize_path(path: str | Path | None) -> str | None:
    if path in (None, ""):
        return None
    return Path(path).name


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_data_manifest(data_root: Path) -> tuple[dict[str, Any] | None, str, str | None]:
    manifest_path = Path(data_root) / "manifest.json"
    if not manifest_path.exists():
        return None, "MISSING", None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, "INVALID", f"manifest_invalid:{exc}"
    if not isinstance(payload, dict):
        return None, "INVALID", "manifest_invalid:root_must_be_object"
    files = payload.get("files")
    if files is not None and not isinstance(files, list):
        return None, "INVALID", "manifest_invalid:files_must_be_list"
    return payload, "OK", None


def _manifest_adjustment_map(manifest: dict[str, Any] | None) -> dict[tuple[str, str], str]:
    if not manifest:
        return {}
    result: dict[tuple[str, str], str] = {}
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            continue
        symbol = _canonical_symbol(item.get("symbol") or "")
        frequency = _frequency_label(item.get("frequency") or "")
        adjustment = str(item.get("adjustment") or "").strip().lower()
        if symbol and frequency and adjustment:
            result[(symbol, frequency)] = adjustment
    return result


def _load_candidate(store: CandidateValidationStore, candidate_id: str) -> CandidateRecord:
    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise CandidateTransitionError(f"candidate_not_found:{candidate_id}")
    return candidate


def _ensure_pending_dataset_validation(candidate: CandidateRecord) -> None:
    if candidate.validation_status != ValidationStatus.PENDING_DATA_VALIDATION.value:
        raise CandidateTransitionError("candidate_must_be_pending_data_validation")


def _download_missing_data(
    *,
    symbol: str,
    frequency: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
) -> Path:
    try:
        from src.data.longbridge_history import LongbridgeHistoryDownloader
    except Exception as exc:  # pragma: no cover - exercised in hidden env
        raise RuntimeError(f"longbridge_downloader_unavailable:{exc}") from exc

    downloader = LongbridgeHistoryDownloader(output_dir=output_dir)
    artifact = downloader.download_symbol_frequency(
        symbol,
        frequency,
        start_date=datetime.fromisoformat(start_date).date(),
        end_date=datetime.fromisoformat(end_date).date(),
    )
    return Path(artifact.path)


def _default_start_date(frequency: str) -> str:
    return "2020-01-01" if _frequency_label(frequency) == "daily" else "2023-12-04"


def _data_file_for_symbol(
    *,
    symbol: str,
    frequency: str,
    local_data_root: Path,
    download_dir: Path,
    download_missing: bool,
    dry_run: bool,
) -> tuple[Path | None, dict[str, Any]]:
    details: dict[str, Any] = {
        "symbol": symbol,
        "frequency": _frequency_label(frequency),
        "source": "local",
        "download_attempted": False,
        "download_performed": False,
        "path": None,
        "exists": False,
        "sha256": None,
        "rows": None,
    }
    local_path = _find_local_data_file(local_data_root, symbol, frequency)
    if local_path is not None:
        details["exists"] = True
        details["path"] = _relativize_path(local_path)
        details["sha256"] = _sha256(local_path)
        details["rows"] = _count_csv_rows(local_path)
        return local_path, details
    details["download_attempted"] = bool(download_missing)
    if not download_missing or dry_run:
        return None, details
    download_dir.mkdir(parents=True, exist_ok=True)
    downloaded_path = _download_missing_data(
        symbol=symbol,
        frequency=frequency,
        output_dir=download_dir,
        start_date=_default_start_date(frequency),
        end_date=datetime.now(timezone.utc).date().isoformat(),
    )
    details["download_performed"] = True
    details["source"] = "longbridge_quote_api"
    details["path"] = _relativize_path(downloaded_path)
    details["exists"] = True
    details["sha256"] = _sha256(downloaded_path)
    details["rows"] = _count_csv_rows(downloaded_path)
    return downloaded_path, details


def _validation_report_path(output_dir: Path) -> Path:
    return output_dir / "dataset_validation_report.json"


def _summary_csv_path(output_dir: Path) -> Path:
    return output_dir / "dataset_validation_summary.csv"


def _manifest_path(output_dir: Path) -> Path:
    return output_dir / "data_manifest.json"


def _audit_path(output_dir: Path) -> Path:
    return output_dir / "validation_audit.json"


def _overall_status(validations: list[dict[str, Any]]) -> tuple[ValidationStatus, str, dict[str, Any]]:
    if not validations:
        return ValidationStatus.DATA_INVALID, "no_validations_ran", {"benchmark_valid": False, "eligible_for_backtest": False, "future_data_risk": True, "reconciliation_status": "FAILED"}
    invalid = [item for item in validations if item.get("benchmark_status") != "VALID" or not item.get("eligible_for_backtest", False)]
    if invalid:
        first = invalid[0]
        reason = str(first.get("fail_closed_reason") or first.get("benchmark_status") or "dataset_validation_failed")
        return ValidationStatus.DATA_INVALID, reason, {"benchmark_valid": False, "eligible_for_backtest": False, "future_data_risk": bool(first.get("future_data_risk", True)), "reconciliation_status": "FAILED"}
    return ValidationStatus.DATA_VALID, "", {"benchmark_valid": True, "eligible_for_backtest": True, "future_data_risk": False, "reconciliation_status": "OK"}


def validate_candidate_dataset(
    *,
    candidate_id: str,
    candidate_store: str | Path,
    output_dir: str | Path,
    download_missing: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    _ensure_no_parent_refs(candidate_store, label="candidate_store")
    _ensure_no_parent_refs(output_dir, label="output_dir")
    store = CandidateValidationStore(_candidate_store_root(candidate_store))
    candidate = _load_candidate(store, candidate_id)
    _ensure_pending_dataset_validation(candidate)
    if download_missing and dry_run:
        raise CandidateTransitionError("download_missing_requires_apply")

    output_root = _candidate_output_dir(Path(output_dir), candidate.candidate_id)
    output_root.mkdir(parents=True, exist_ok=True)
    download_dir = output_root / "downloads"
    relative_output_root = Path(candidate.candidate_id)
    relative_download_dir = relative_output_root / "downloads"
    relative_report_path = relative_output_root / "dataset_validation_report.json"
    relative_summary_path = relative_output_root / "dataset_validation_summary.csv"
    relative_manifest_path = relative_output_root / "data_manifest.json"
    relative_audit_path = relative_output_root / "validation_audit.json"

    started_at = _utc_now_iso()
    local_data_root = DEFAULT_DATA_ROOT
    manifest_payload, manifest_status, manifest_error = _load_data_manifest(local_data_root)
    manifest_adjustments = _manifest_adjustment_map(manifest_payload)
    validations: list[dict[str, Any]] = []
    data_sources: list[dict[str, Any]] = []
    symbol_freq = _frequency_label(candidate.timeframe)
    expected_adjustment = None
    if manifest_adjustments:
        symbol_adjustment = manifest_adjustments.get((_canonical_symbol(candidate.symbol), symbol_freq))
        if symbol_adjustment:
            expected_adjustment = symbol_adjustment

    if manifest_error:
        validations.append(
            {
                "symbol": candidate.symbol,
                "benchmark": candidate.benchmarks[0] if candidate.benchmarks else "",
                "frequency": symbol_freq,
                "benchmark_status": "INVALID_MANIFEST",
                "frequency_match": True,
                "overlap_ratio": 0.0,
                "future_data_risk": False,
                "duplicate_count": 0,
                "invalid_ohlc_count": 0,
                "missing_value_count": 0,
                "eligible_for_backtest": False,
                "fail_closed_reason": manifest_error,
            }
        )

    if not manifest_error:
        symbol_path, symbol_info = _data_file_for_symbol(
            symbol=candidate.symbol,
            frequency=symbol_freq,
            local_data_root=local_data_root,
            download_dir=download_dir,
            download_missing=download_missing,
            dry_run=dry_run,
        )
        data_sources.append(symbol_info)
        for benchmark in candidate.benchmarks:
            benchmark_path, benchmark_info = _data_file_for_symbol(
                symbol=benchmark,
                frequency=symbol_freq,
                local_data_root=local_data_root,
                download_dir=download_dir,
                download_missing=download_missing,
                dry_run=dry_run,
            )
            data_sources.append(benchmark_info)
            if symbol_path is None or benchmark_path is None:
                validations.append(
                    {
                        "symbol": candidate.symbol,
                        "benchmark": benchmark,
                        "frequency": symbol_freq,
                        "benchmark_status": "MISSING_DATA",
                        "frequency_match": False,
                        "overlap_ratio": 0.0,
                        "future_data_risk": False,
                        "duplicate_count": 0,
                        "invalid_ohlc_count": 0,
                        "missing_value_count": 0,
                        "eligible_for_backtest": False,
                        "fail_closed_reason": "missing_symbol_or_benchmark_data",
                    }
                )
                continue
            if expected_adjustment is not None:
                benchmark_adjustment = manifest_adjustments.get((_canonical_symbol(benchmark), symbol_freq))
                if benchmark_adjustment is None:
                    validations.append(
                        {
                            "symbol": candidate.symbol,
                            "benchmark": benchmark,
                            "frequency": symbol_freq,
                            "benchmark_status": "INVALID_MANIFEST",
                            "frequency_match": True,
                            "overlap_ratio": 0.0,
                            "future_data_risk": False,
                            "duplicate_count": 0,
                            "invalid_ohlc_count": 0,
                            "missing_value_count": 0,
                            "eligible_for_backtest": False,
                            "fail_closed_reason": "manifest_missing_benchmark_adjustment",
                        }
                    )
                    continue
                if benchmark_adjustment != expected_adjustment:
                    validations.append(
                        {
                            "symbol": candidate.symbol,
                            "benchmark": benchmark,
                            "frequency": symbol_freq,
                            "benchmark_status": "INVALID_MANIFEST",
                            "frequency_match": True,
                            "overlap_ratio": 0.0,
                            "future_data_risk": False,
                            "duplicate_count": 0,
                            "invalid_ohlc_count": 0,
                            "missing_value_count": 0,
                            "eligible_for_backtest": False,
                            "fail_closed_reason": "adjustment_type_mismatch",
                        }
                    )
                    continue
            report = validate_backtest_dataset(
                symbol_csv=symbol_path,
                benchmark_csv=benchmark_path,
                symbol=candidate.symbol,
                benchmark=benchmark,
                expected_frequency=symbol_freq,
            )
            validations.append(
                {
                    "symbol": report.symbol,
                    "benchmark": report.benchmark_symbol,
                    "frequency": report.expected_frequency,
                    "benchmark_status": report.benchmark_mapping_status,
                    "frequency_match": report.frequency_match,
                    "overlap_ratio": report.overlap_ratio,
                    "missing_bar_ratio": report.missing_bar_ratio,
                    "duplicate_count": report.duplicate_timestamp_count,
                    "invalid_ohlc_count": report.invalid_ohlc_count,
                    "missing_value_count": report.missing_value_count,
                    "future_data_risk": report.future_data_risk,
                    "eligible_for_backtest": report.eligible_for_backtest,
                    "session_completeness_status": report.session_completeness_status,
                    "fail_closed_reason": report.fail_closed_reason,
                    "report": report.to_dict(),
                }
            )

    status, reason, transition_metadata = _overall_status(validations)
    report_path = _validation_report_path(output_root)
    summary_path = _summary_csv_path(output_root)
    manifest_path = _manifest_path(output_root)
    audit_path = _audit_path(output_root)
    public_data_sources = [{k: v for k, v in item.items() if k != "path"} | {"path": item.get("path")} for item in data_sources]

    report_payload = {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "selected_at": candidate.selected_at,
        "candidate_current_status": candidate.validation_status,
        "validation_status_before": candidate.validation_status,
        "validation_status_after": status.value,
        "applied": bool(not dry_run),
        "dry_run": bool(dry_run),
        "download_missing": bool(download_missing),
        "validator_version": VALIDATOR_VERSION,
        "started_at": started_at,
        "completed_at": _utc_now_iso(),
        "candidate": candidate.to_dict(),
        "data_sources": public_data_sources,
        "validations": validations,
        "overall": {
            "status": status.value,
            "rejection_reason": reason,
            "manifest_status": manifest_status,
            **transition_metadata,
        },
    }
    summary_rows = [
        {
            "candidate_id": candidate.candidate_id,
            "symbol": candidate.symbol,
            "selected_at": candidate.selected_at,
            "benchmark_count": len(candidate.benchmarks),
            "benchmarks": "|".join(candidate.benchmarks),
            "timeframe": symbol_freq,
            "validation_status": status.value,
            "candidate_current_status": candidate.validation_status,
            "rejection_reason": reason,
            "applied": bool(not dry_run),
            "dry_run": bool(dry_run),
            "download_missing": bool(download_missing),
            "manifest_status": manifest_status,
            "benchmark_valid": transition_metadata["benchmark_valid"],
            "eligible_for_backtest": transition_metadata["eligible_for_backtest"],
            "future_data_risk": transition_metadata["future_data_risk"],
            "reconciliation_status": transition_metadata["reconciliation_status"],
            "report_path": str(relative_report_path),
            "validated_at": _utc_now_iso(),
            "validator_version": VALIDATOR_VERSION,
        }
    ]
    manifest = {
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "selected_at": candidate.selected_at,
        "timeframe": symbol_freq,
        "benchmarks": list(candidate.benchmarks),
        "asset_type": candidate.asset_type,
        "strategy_family": candidate.strategy_family,
        "risk_profile": candidate.risk_profile,
        "output_dir": str(relative_output_root),
        "generated_at": _utc_now_iso(),
        "files": public_data_sources,
        "manifest_status": manifest_status,
        "manifest_path": _relativize_path(Path(local_data_root) / "manifest.json") if manifest_payload else None,
        "time_range": {
            "symbol_start": validations[0].get("report", {}).get("data_start") if validations else None,
            "symbol_end": validations[0].get("report", {}).get("data_end") if validations else None,
            "benchmark_starts": [item.get("report", {}).get("benchmark_start") for item in validations if item.get("report")],
            "benchmark_ends": [item.get("report", {}).get("benchmark_end") for item in validations if item.get("report")],
        },
        "data_root": "data/market/longbridge",
        "download_dir": str(relative_download_dir),
        "checksums": {f"{item['symbol']}:{item['frequency']}": item.get("sha256") for item in data_sources if item.get("path")},
    }
    audit = {
        "candidate_id": candidate.candidate_id,
        "previous_status": candidate.validation_status,
        "new_status": status.value,
        "operator_action": "manual_dataset_validation",
        "started_at": started_at,
        "completed_at": report_payload["completed_at"],
        "data_sources": public_data_sources,
        "quote_api_used": any(str(item.get("source") or "") == "longbridge_quote_api" for item in data_sources),
        "trade_api_used": False,
        "report_path": str(relative_report_path),
        "validation_result": status.value,
        "dry_run": bool(dry_run),
        "download_missing": bool(download_missing),
        "validator_version": VALIDATOR_VERSION,
        "applied": bool(not dry_run),
        "manifest_status": manifest_status,
    }

    _atomic_write_text(report_path, json.dumps(_jsonable(report_payload), indent=2, ensure_ascii=False))
    _atomic_write_csv(summary_path, summary_rows)
    _atomic_write_text(manifest_path, json.dumps(_jsonable(manifest), indent=2, ensure_ascii=False))
    _atomic_write_text(audit_path, json.dumps(_jsonable(audit), indent=2, ensure_ascii=False))

    if not dry_run:
        if status == ValidationStatus.DATA_VALID:
            store.transition(
                candidate.candidate_id,
                ValidationStatus.DATA_VALID,
                reason=reason,
                metadata={
                    "benchmark_valid": True,
                    "eligible_for_backtest": True,
                    "future_data_risk": False,
                    "reconciliation_status": "OK",
                    "operator_action": "manual_dataset_validation",
                    "started_at": started_at,
                    "completed_at": report_payload["completed_at"],
                    "data_sources": public_data_sources,
                    "quote_api_used": any(str(item.get("source") or "") == "longbridge_quote_api" for item in data_sources),
                    "trade_api_used": False,
                    "report_path": str(relative_report_path),
                    "validation_result": status.value,
                    "validator_version": VALIDATOR_VERSION,
                    "validation_report_path": str(relative_report_path),
                    "dataset_validation_report_path": str(relative_report_path),
                },
            )
        else:
            store.transition(
                candidate.candidate_id,
                ValidationStatus.DATA_INVALID,
                reason=reason,
                metadata={
                    "reason": reason,
                    "operator_action": "manual_dataset_validation",
                    "started_at": started_at,
                    "completed_at": report_payload["completed_at"],
                    "data_sources": public_data_sources,
                    "quote_api_used": any(str(item.get("source") or "") == "longbridge_quote_api" for item in data_sources),
                    "trade_api_used": False,
                    "report_path": str(relative_report_path),
                    "validation_result": status.value,
                    "validator_version": VALIDATOR_VERSION,
                    "failure_metrics": validations,
                    "validation_report_path": str(relative_report_path),
                    "dataset_validation_report_path": str(relative_report_path),
                },
            )

    return {
        "candidate_id": candidate.candidate_id,
        "previous_status": candidate.validation_status,
        "new_status": status.value,
        "reason": reason,
        "candidate_current_status": candidate.validation_status,
        "applied": bool(not dry_run),
        "dry_run": bool(dry_run),
        "download_missing": bool(download_missing),
        "report_path": str(report_path),
        "summary_path": str(summary_path),
        "manifest_path": str(manifest_path),
        "audit_path": str(audit_path),
        "validations": validations,
        "data_sources": data_sources,
    }
