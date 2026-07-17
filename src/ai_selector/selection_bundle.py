from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.utils.market_calendar import required_selection_date


def _project_dir() -> Path:
    from src.ai_selector.selection_state import PROJECT_DIR

    return PROJECT_DIR


def _state_dir() -> Path:
    from src.ai_selector.selection_state import _state_dir

    return _state_dir()


def _selection_run_id(value: str | None = None) -> str:
    return str(value or uuid.uuid4().hex)


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _backup_files(paths: list[Path], backup_dir: Path) -> dict[Path, Path]:
    backups: dict[Path, Path] = {}
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if not path.exists():
            continue
        backup_path = backup_dir / path.name
        shutil.copy2(path, backup_path)
        backups[path] = backup_path
    return backups


def _restore_backups(backups: dict[Path, Path]) -> None:
    for target, backup in backups.items():
        try:
            shutil.copy2(backup, target)
        except Exception:
            continue


def _cleanup_backups(backups: dict[Path, Path]) -> None:
    for backup in backups.values():
        try:
            backup.unlink(missing_ok=True)
        except Exception:
            pass


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str, sort_keys=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp_path.write_text(_json_dump(payload), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _write_yaml_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    os.replace(tmp_path, path)
    return path


def _slot_disabled_reason(result_quality: str | None, research_admission: str | None, disabled_reason: str | None) -> str:
    if disabled_reason:
        return str(disabled_reason)
    if str(result_quality or "").strip().upper() == "INVALID" or str(research_admission or "").strip().upper() == "BLOCKED":
        return "selection_blocked"
    return "top_n_not_filled"


def _slot_count(top_items: list[dict[str, Any]], requested_top_n: int | None = None) -> int:
    from src.ai_selector.selection_state import configured_top_count

    if requested_top_n is not None:
        try:
            return max(1, int(requested_top_n))
        except Exception:
            pass
    configured = configured_top_count()
    return max(3, configured, len(list(top_items or [])))


def _selection_bundle_fingerprint(bundle: "SelectionBundle") -> str:
    payload = {
        "bundle_version": bundle.bundle_version,
        "selection_run_id": bundle.selection_run_id,
        "selection_date": bundle.selection_date,
        "generated_at": bundle.generated_at,
        "processing_phase": bundle.processing_phase,
        "selection_stage": bundle.selection_stage,
        "result_quality": bundle.result_quality,
        "research_admission": bundle.research_admission,
        "top_sync_status": bundle.top_sync_status,
        "top_sync_error": bundle.top_sync_error,
        "selected_symbols": bundle.selected_symbols,
        "disabled_slots": bundle.disabled_slots,
        "top_items": [
            {
                "ticker": str(item.get("ticker") or "").strip().upper(),
                "score": item.get("score"),
                "final_score": item.get("final_score"),
                "ai_score": item.get("ai_score"),
                "selection_date": item.get("selection_date"),
                "fallback_used": bool(item.get("fallback_used", False)),
                "reduce_only": bool(item.get("reduce_only", False)),
            }
            for item in bundle.top_items
        ],
        "selection_state": {
            "et_date": str(bundle.selection_state_payload.get("et_date") or ""),
            "selected_symbols": [str(item or "").strip().upper() for item in bundle.selection_state_payload.get("selected_symbols") or []],
            "selection_stage": str(bundle.selection_state_payload.get("selection_stage") or ""),
            "processing_phase": str(bundle.selection_state_payload.get("processing_phase") or ""),
        },
    }
    return _sha256_text(_json_dump(payload))


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(_project_dir()))
    except Exception:
        return str(path)


@dataclass(slots=True)
class SelectionBundle:
    summary: dict[str, Any]
    selection_state_payload: dict[str, Any]
    top_items: list[dict[str, Any]]
    selection_run_id: str
    selection_date: str
    generated_at: str
    processing_phase: str
    selection_stage: str
    result_quality: str
    research_admission: str
    requested_top_n: int | None = None
    top_sync_status: str = "OK"
    top_sync_error: str = ""
    disabled_reason: str | None = None
    bundle_version: str = "selection_bundle_v1"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def selected_top_n(self) -> int:
        return len(self.top_items)

    @property
    def selected_symbols(self) -> list[str]:
        return [
            str(item.get("ticker") or "").strip().upper()
            for item in self.top_items
            if str(item.get("ticker") or "").strip()
        ]

    @property
    def slot_count(self) -> int:
        return _slot_count(self.top_items, requested_top_n=self.requested_top_n)

    @property
    def top_slot_count(self) -> int:
        return self.slot_count

    @property
    def enabled_slots(self) -> list[int]:
        return [i for i in range(1, self.slot_count + 1) if i <= self.selected_top_n]

    @property
    def disabled_slots(self) -> list[int]:
        return [i for i in range(1, self.slot_count + 1) if i > self.selected_top_n]

    @property
    def report_latest_path(self) -> Path:
        return _project_dir() / "reports" / "ai_selection_latest.json"

    @property
    def report_dated_path(self) -> Path:
        return _project_dir() / "reports" / f"ai_selection_{self.selection_date}.json"

    @property
    def state_path(self) -> Path:
        from src.ai_selector.selection_state import selection_state_path

        return selection_state_path()

    @property
    def audit_path(self) -> Path:
        return _state_dir() / "selection_sync_audit.json"

    @property
    def manifest_path(self) -> Path:
        return _state_dir() / "selection_bundle_manifest.json"

    @property
    def top_paths(self) -> list[Path]:
        return [_project_dir() / "configs" / f"TOP{i}.yaml" for i in range(1, self.slot_count + 1)]

    @property
    def selection_bundle_hash(self) -> str:
        return _selection_bundle_fingerprint(self)

    def report_payload(self) -> dict[str, Any]:
        payload = dict(self.summary or {})
        payload.update(
            {
                "selection_run_id": self.selection_run_id,
                "selection_date": self.selection_date,
                "generated_at": self.generated_at,
                "selection_stage": self.selection_stage,
                "processing_phase": self.processing_phase,
                "execution_status": str(payload.get("execution_status") or payload.get("selection_execution_status") or "COMPLETED"),
                "result_quality": str(payload.get("result_quality") or "COMPLETE"),
                "research_admission": str(payload.get("research_admission") or "RESEARCH_READY"),
                "top_sync_run_id": self.selection_run_id,
                "top_sync_status": self.top_sync_status,
                "top_sync_error": self.top_sync_error,
                "selection_bundle_version": self.bundle_version,
                "selection_bundle_hash": self.selection_bundle_hash,
                "selection_bundle_manifest_path": _relative_path(self.manifest_path),
                "requested_top_n": int(self.requested_top_n or self.slot_count),
                "selected_top_n": self.selected_top_n,
                "top_slot_count": self.slot_count,
                "selected_symbols": list(self.selected_symbols),
                "selection_symbols": list(self.selected_symbols),
                "configured_top_symbols": list(self.selected_symbols),
                "enabled_slots": list(self.enabled_slots),
                "disabled_slots": list(self.disabled_slots),
            }
        )
        payload.setdefault("selection_bundle_version", self.bundle_version)
        payload.setdefault("selection_bundle_hash", self.selection_bundle_hash)
        payload.setdefault("selection_bundle_manifest_path", _relative_path(self.manifest_path))
        return payload

    def state_payload(self) -> dict[str, Any]:
        payload = dict(self.selection_state_payload or {})
        payload.update(
            {
                "selection_run_id": self.selection_run_id,
                "top_sync_run_id": self.selection_run_id,
                "top_sync_status": self.top_sync_status,
                "top_sync_error": self.top_sync_error,
                "processing_phase": self.processing_phase,
                "selection_symbols": list(self.selected_symbols),
                "configured_top_symbols": list(self.selected_symbols),
                "disabled_slots": list(self.disabled_slots),
                "requested_top_n": int(self.requested_top_n or self.slot_count),
                "selected_top_n": self.selected_top_n,
                "top_slot_count": self.slot_count,
                "enabled_slots": list(self.enabled_slots),
                "synced_at": self.generated_at,
                "selection_bundle_version": self.bundle_version,
                "selection_bundle_hash": self.selection_bundle_hash,
                "selection_bundle_manifest_path": _relative_path(self.manifest_path),
            }
        )
        payload.setdefault("selection_symbols", list(self.selected_symbols))
        payload.setdefault("configured_top_symbols", list(self.selected_symbols))
        payload.setdefault("selection_bundle_version", self.bundle_version)
        payload.setdefault("selection_bundle_hash", self.selection_bundle_hash)
        payload.setdefault("selection_bundle_manifest_path", _relative_path(self.manifest_path))
        return payload

    def audit_payload(self) -> dict[str, Any]:
        return {
            "selection_run_id": self.selection_run_id,
            "top_sync_run_id": self.selection_run_id,
            "top_sync_status": self.top_sync_status,
            "top_sync_error": self.top_sync_error,
            "selection_date": self.selection_date,
            "generated_at": self.generated_at,
            "selection_stage": self.selection_stage,
            "processing_phase": self.processing_phase,
            "requested_top_n": int(self.requested_top_n or self.slot_count),
            "selected_top_n": self.selected_top_n,
            "top_slot_count": self.slot_count,
            "enabled_slots": list(self.enabled_slots),
            "top_count": len(self.top_items),
            "slot_count": self.slot_count,
            "disabled_slots": list(self.disabled_slots),
            "selection_symbols": list(self.selected_symbols),
            "configured_top_symbols": list(self.selected_symbols),
            "result_quality": self.result_quality,
            "research_admission": self.research_admission,
            "selection_bundle_version": self.bundle_version,
            "selection_bundle_hash": self.selection_bundle_hash,
            "selection_bundle_manifest_path": _relative_path(self.manifest_path),
        }

    def manifest_payload(self) -> dict[str, Any]:
        report_payload = self.report_payload()
        state_payload = self.state_payload()
        audit_payload = self.audit_payload()
        top_payloads = {}
        for index, path in enumerate(self.top_paths, start=1):
            top_payloads[f"TOP{index}.yaml"] = {
                "path": _relative_path(path),
                "exists": path.exists(),
                "sha256": _sha256_file(path),
            }
        return {
            "bundle_version": self.bundle_version,
            "selection_run_id": self.selection_run_id,
            "selection_date": self.selection_date,
            "generated_at": self.generated_at,
            "selection_stage": self.selection_stage,
            "processing_phase": self.processing_phase,
            "execution_status": str(report_payload.get("execution_status") or "COMPLETED"),
            "result_quality": self.result_quality,
            "research_admission": self.research_admission,
            "requested_top_n": int(self.requested_top_n or self.slot_count),
            "selected_top_n": self.selected_top_n,
            "selection_bundle_hash": self.selection_bundle_hash,
            "selection_symbols": list(self.selected_symbols),
            "disabled_slots": list(self.disabled_slots),
            "enabled_slots": list(self.enabled_slots),
            "slot_count": self.slot_count,
            "top_slot_count": self.slot_count,
            "top_count": len(self.top_items),
            "paths": {
                "report_latest": _relative_path(self.report_latest_path),
                "report_dated": _relative_path(self.report_dated_path),
                "state": _relative_path(self.state_path),
                "audit": _relative_path(self.audit_path),
                "manifest": _relative_path(self.manifest_path),
                "top": top_payloads,
            },
            "hashes": {
                "report_latest": _sha256_text(_json_dump(report_payload)),
                "report_dated": _sha256_text(_json_dump(report_payload)),
                "state": _sha256_text(_json_dump(state_payload)),
                "audit": _sha256_text(_json_dump(audit_payload)),
                "top": {
                    f"TOP{index}.yaml": _sha256_file(path)
                    for index, path in enumerate(self.top_paths, start=1)
                },
            },
            "published_at": self.generated_at,
        }


def build_selection_bundle(
    *,
    summary: dict[str, Any],
    selection_state_payload: dict[str, Any],
    top_items: list[dict[str, Any]],
    selection_run_id: str | None = None,
    selection_date: str | None = None,
    generated_at: str | None = None,
    result_quality: str | None = None,
    research_admission: str | None = None,
    processing_phase: str | None = None,
    requested_top_n: int | None = None,
    top_sync_status: str = "OK",
    top_sync_error: str = "",
    disabled_reason: str | None = None,
) -> SelectionBundle:
    selection_run_id = _selection_run_id(selection_run_id)
    generated_at = str(generated_at or datetime.now().isoformat())
    selection_date = str(selection_date or required_selection_date())
    processing_phase = str(processing_phase or summary.get("processing_phase") or selection_state_payload.get("processing_phase") or "")
    selection_stage = str(summary.get("selection_stage") or selection_state_payload.get("selection_stage") or "")
    result_quality = str(result_quality or summary.get("result_quality") or "")
    research_admission = str(research_admission or summary.get("research_admission") or "")
    requested_top_n = requested_top_n if requested_top_n is not None else summary.get("requested_top_n")
    if requested_top_n is None:
        requested_top_n = summary.get("target_top_n") or selection_state_payload.get("requested_top_n") or selection_state_payload.get("top_slot_count")
    try:
        requested_top_n = int(requested_top_n) if requested_top_n is not None else None
    except Exception:
        requested_top_n = None
    disabled_reason = _slot_disabled_reason(result_quality, research_admission, disabled_reason)
    return SelectionBundle(
        summary=dict(summary or {}),
        selection_state_payload=dict(selection_state_payload or {}),
        top_items=[dict(item or {}) for item in list(top_items or [])],
        selection_run_id=selection_run_id,
        selection_date=selection_date,
        generated_at=generated_at,
        processing_phase=processing_phase,
        selection_stage=selection_stage,
        result_quality=result_quality,
        research_admission=research_admission,
        requested_top_n=requested_top_n,
        top_sync_status=str(top_sync_status or "OK"),
        top_sync_error=str(top_sync_error or ""),
        disabled_reason=disabled_reason,
    )


def _bundle_validation_errors(bundle: SelectionBundle) -> list[str]:
    errors: list[str] = []
    requested_top_n = bundle.requested_top_n
    if requested_top_n is None:
        return errors
    try:
        requested_top_n = int(requested_top_n)
    except Exception:
        errors.append("invalid_top_slot_count")
        return errors
    if requested_top_n <= 0:
        errors.append("invalid_top_slot_count")
        return errors

    selected_top_n = bundle.selected_top_n
    slot_count = bundle.slot_count
    if selected_top_n > requested_top_n:
        errors.append("selected_top_n_exceeds_requested")
    if slot_count != requested_top_n:
        errors.append("invalid_top_slot_count")
    if len(bundle.top_paths) != requested_top_n:
        errors.append("invalid_top_slot_count")

    expected_disabled_slots = list(range(selected_top_n + 1, requested_top_n + 1))
    if list(bundle.disabled_slots) != expected_disabled_slots:
        errors.append("disabled_slot_mismatch")

    expected_selected_symbols = [
        str(item.get("ticker") or "").strip().upper()
        for item in bundle.top_items
        if str(item.get("ticker") or "").strip()
    ]
    if bundle.selected_symbols != expected_selected_symbols:
        errors.append("selected_symbol_mismatch")
    return errors


def persist_selection_bundle(bundle: SelectionBundle) -> dict[str, Any]:
    """Atomically persist the selector bundle.

    The bundle includes the formal report, state, TOP configs, audit and the
    bundle manifest. If any write fails, previously published files are
    restored so consumers never observe a partially upgraded selector state.
    """

    config_writer = importlib.import_module("src.ai_selector.config_writer")
    selection_state = importlib.import_module("src.ai_selector.selection_state")

    bundle = bundle if isinstance(bundle, SelectionBundle) else build_selection_bundle(
        summary=getattr(bundle, "summary", {}) or {},
        selection_state_payload=getattr(bundle, "selection_state_payload", {}) or {},
        top_items=getattr(bundle, "top_items", []) or [],
        selection_run_id=getattr(bundle, "selection_run_id", None),
        selection_date=getattr(bundle, "selection_date", None),
        generated_at=getattr(bundle, "generated_at", None),
        result_quality=getattr(bundle, "result_quality", None),
        research_admission=getattr(bundle, "research_admission", None),
        processing_phase=getattr(bundle, "processing_phase", None),
        top_sync_status=getattr(bundle, "top_sync_status", "OK"),
        top_sync_error=getattr(bundle, "top_sync_error", ""),
        disabled_reason=getattr(bundle, "disabled_reason", None),
    )

    state_path = selection_state.selection_state_path()
    audit_path = bundle.audit_path
    manifest_path = bundle.manifest_path
    manifest_reference = _relative_path(manifest_path)
    top_paths = bundle.top_paths
    latest_report_path = bundle.report_latest_path
    dated_report_path = bundle.report_dated_path

    report_payload = bundle.report_payload()
    state_payload = bundle.state_payload()
    audit_payload = bundle.audit_payload()
    disabled_reason = bundle.disabled_reason
    validation_errors = _bundle_validation_errors(bundle)
    if validation_errors:
        failure_payload = dict(audit_payload)
        failure_payload.update(
            {
                "validation_status": "failed",
                "validation_error_codes": list(validation_errors),
                "validation_error_reason": ",".join(validation_errors),
                "validation_error_message": "bundle_validation_failed",
                "selection_top_n": bundle.selected_top_n,
                "requested_top_n": int(bundle.requested_top_n or bundle.slot_count),
                "top_slot_count": bundle.slot_count,
                "enabled_slots": list(bundle.enabled_slots),
                "disabled_slots": list(bundle.disabled_slots),
            }
        )
        _write_json_atomic(audit_path, failure_payload)
        raise ValueError("bundle_validation_failed:" + ",".join(validation_errors))

    backup_root = _state_dir() / ".selection_bundle_backups" / bundle.selection_run_id
    backups = _backup_files([latest_report_path, dated_report_path, state_path, audit_path, manifest_path, *top_paths], backup_root)
    try:
        _write_json_atomic(latest_report_path, report_payload)
        _write_json_atomic(dated_report_path, report_payload)
        selection_state.write_selection_state(
            et_date=str(state_payload.get("et_date") or bundle.selection_date),
            generated_at=str(state_payload.get("generated_at") or bundle.generated_at),
            selected_symbols=list(state_payload.get("selected_symbols") or []),
            report_path=str(latest_report_path),
            selection_stage=str(state_payload.get("selection_stage") or bundle.selection_stage),
            processing_phase=str(state_payload.get("processing_phase") or bundle.processing_phase),
            selection_run_id=bundle.selection_run_id,
            top_sync_run_id=bundle.selection_run_id,
            top_sync_status=bundle.top_sync_status,
            top_sync_error=bundle.top_sync_error,
            selection_symbols=list(state_payload.get("selection_symbols") or []),
            configured_top_symbols=list(state_payload.get("configured_top_symbols") or []),
            disabled_slots=list(state_payload.get("disabled_slots") or []),
            synced_at=str(state_payload.get("synced_at") or bundle.generated_at),
            result_quality=str(state_payload.get("result_quality") or bundle.result_quality),
            research_admission=str(state_payload.get("research_admission") or bundle.research_admission),
            selection_bundle_manifest_path=manifest_reference,
            selection_bundle_hash=bundle.selection_bundle_hash,
            selection_bundle_version=bundle.bundle_version,
            requested_top_n=int(state_payload.get("requested_top_n") or bundle.slot_count),
            selected_top_n=int(state_payload.get("selected_top_n") or bundle.selected_top_n),
            top_slot_count=int(state_payload.get("top_slot_count") or bundle.slot_count),
            enabled_slots=list(state_payload.get("enabled_slots") or bundle.enabled_slots),
        )
        config_writer.write_top_configs(
            list(bundle.top_items or []),
            selection_run_id=bundle.selection_run_id,
            selection_date=bundle.selection_date,
            generated_at=bundle.generated_at,
            disabled_reason=disabled_reason,
            result_quality=bundle.result_quality,
            research_admission=bundle.research_admission,
            top_sync_status=bundle.top_sync_status,
            top_sync_error=bundle.top_sync_error,
            slot_limit=bundle.slot_count,
            selection_bundle_manifest_path=manifest_reference,
            selection_bundle_hash=bundle.selection_bundle_hash,
            selection_bundle_version=bundle.bundle_version,
        )
        _write_json_atomic(audit_path, audit_payload)
        manifest_payload = bundle.manifest_payload()
        _write_json_atomic(manifest_path, manifest_payload)
    except Exception:
        _restore_backups(backups)
        raise
    else:
        _cleanup_backups(backups)

    return {
        "bundle_version": bundle.bundle_version,
        "selection_bundle_hash": bundle.selection_bundle_hash,
        "selection_bundle_manifest_path": manifest_reference,
        "selection_run_id": bundle.selection_run_id,
        "top_sync_run_id": bundle.selection_run_id,
        "top_sync_status": bundle.top_sync_status,
        "top_sync_error": bundle.top_sync_error,
        "selection_date": bundle.selection_date,
        "generated_at": bundle.generated_at,
        "selection_stage": bundle.selection_stage,
        "requested_top_n": int(state_payload.get("requested_top_n") or bundle.slot_count),
        "selected_top_n": int(state_payload.get("selected_top_n") or bundle.selected_top_n),
        "top_slot_count": int(state_payload.get("top_slot_count") or bundle.slot_count),
        "disabled_slots": list(bundle.disabled_slots),
        "selected_symbols": list(bundle.selected_symbols),
        "audit_path": _relative_path(audit_path),
        "state_path": _relative_path(state_path),
        "report_path": _relative_path(latest_report_path),
        "top_paths": [_relative_path(path) for path in top_paths],
    }


def write_selection_bundle_atomic(
    *,
    summary: dict[str, Any],
    selection_state_payload: dict[str, Any],
    top_items: list[dict[str, Any]],
    selection_run_id: str | None = None,
    selection_date: str | None = None,
    generated_at: str | None = None,
    result_quality: str | None = None,
    research_admission: str | None = None,
    processing_phase: str | None = None,
    requested_top_n: int | None = None,
    top_sync_status: str = "OK",
    top_sync_error: str = "",
    disabled_reason: str | None = None,
) -> dict[str, Any]:
    bundle = build_selection_bundle(
        summary=summary,
        selection_state_payload=selection_state_payload,
        top_items=top_items,
        selection_run_id=selection_run_id,
        selection_date=selection_date,
        generated_at=generated_at,
        result_quality=result_quality,
        research_admission=research_admission,
        processing_phase=processing_phase,
        requested_top_n=requested_top_n,
        top_sync_status=top_sync_status,
        top_sync_error=top_sync_error,
        disabled_reason=disabled_reason,
    )
    return persist_selection_bundle(bundle)
