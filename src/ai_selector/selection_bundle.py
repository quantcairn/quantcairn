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

    configured = str(os.environ.get("SOXS_PROJECT_DIR") or "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(PROJECT_DIR).resolve()


def _state_dir() -> Path:
    from src.ai_selector.selection_state import _state_dir

    return _state_dir()


def _bundle_root_dir(selection_run_id: str, bundle_version: str) -> Path:
    return _state_dir() / "selection_bundles" / selection_run_id / bundle_version


def _bundle_staging_dir(selection_run_id: str, bundle_version: str) -> Path:
    staging_root = _state_dir() / "selection_bundles" / ".staging" / selection_run_id
    staging_root.mkdir(parents=True, exist_ok=True)
    return staging_root / f"{bundle_version}.tmp-{uuid.uuid4().hex}"


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


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


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
    def bundle_root_path(self) -> Path:
        return _bundle_root_dir(self.selection_run_id, self.bundle_version)

    @property
    def bundle_report_path(self) -> Path:
        return self.bundle_root_path / "ai_selection_report.json"

    @property
    def bundle_state_path(self) -> Path:
        return self.bundle_root_path / "ai_selection_state.json"

    @property
    def bundle_audit_path(self) -> Path:
        return self.bundle_root_path / "selection_sync_audit.json"

    @property
    def bundle_metadata_path(self) -> Path:
        return self.bundle_root_path / "bundle_metadata.json"

    @property
    def top_paths(self) -> list[Path]:
        return [_project_dir() / "configs" / f"TOP{i}.yaml" for i in range(1, self.slot_count + 1)]

    @property
    def bundle_top_paths(self) -> list[Path]:
        return [self.bundle_root_path / f"TOP{i}.yaml" for i in range(1, self.slot_count + 1)]

    @property
    def selection_bundle_hash(self) -> str:
        return _selection_bundle_fingerprint(self)

    def report_payload(self) -> dict[str, Any]:
        payload = dict(self.summary or {})
        selected_top_n = self.selected_top_n
        requested_top_n = int(self.requested_top_n or self.slot_count)
        execution_status = str(payload.get("execution_status") or payload.get("selection_execution_status") or "COMPLETED")
        if execution_status == "FAILED":
            selection_outcome = "FAILED"
        elif selected_top_n <= 0:
            selection_outcome = "NO_TRADABLE_SELECTION"
        elif selected_top_n < requested_top_n:
            selection_outcome = "PARTIAL"
        else:
            selection_outcome = "SUCCESS"
        payload.update(
            {
                "selection_run_id": self.selection_run_id,
                "selection_date": self.selection_date,
                "generated_at": self.generated_at,
                "selection_stage": self.selection_stage,
                "processing_phase": self.processing_phase,
                "pipeline_status": str(payload.get("pipeline_status") or execution_status),
                "execution_status": execution_status,
                "selection_outcome": str(payload.get("selection_outcome") or selection_outcome),
                "completed_with_selection": bool(payload.get("completed_with_selection", selection_outcome in {"SUCCESS", "PARTIAL"})),
                "result_quality": str(payload.get("result_quality") or "COMPLETE"),
                "research_admission": str(payload.get("research_admission") or "RESEARCH_READY"),
                "top_sync_run_id": self.selection_run_id,
                "top_sync_status": self.top_sync_status,
                "top_sync_error": self.top_sync_error,
                "selection_bundle_version": self.bundle_version,
                "selection_bundle_hash": self.selection_bundle_hash,
                "selection_bundle_manifest_path": _relative_path(self.manifest_path),
                "selection_bundle_root_path": _relative_path(self.bundle_root_path),
                "selection_bundle_report_path": _relative_path(self.bundle_report_path),
                "requested_top_n": requested_top_n,
                "selected_top_n": selected_top_n,
                "top_slot_count": self.slot_count,
                "tradable_top_candidates": list(payload.get("tradable_top_candidates") or self.top_items),
                "tradable_requested_top_n": requested_top_n,
                "tradable_selected_top_n": selected_top_n,
                "research_top_candidates": list(payload.get("research_top_candidates") or []),
                "research_requested_top_n": int(payload.get("research_requested_top_n") or requested_top_n),
                "research_selected_top_n": int(payload.get("research_selected_top_n") or len(payload.get("research_top_candidates") or [])),
                "validation_pipeline_summary": dict(payload.get("validation_pipeline_summary") or {}),
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
        payload.setdefault("selection_bundle_root_path", _relative_path(self.bundle_root_path))
        payload.setdefault("selection_bundle_report_path", _relative_path(self.bundle_report_path))
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
                "tradable_requested_top_n": int(self.requested_top_n or self.slot_count),
                "tradable_selected_top_n": self.selected_top_n,
                "research_requested_top_n": int((self.summary or {}).get("research_requested_top_n") or self.requested_top_n or self.slot_count),
                "research_selected_top_n": int((self.summary or {}).get("research_selected_top_n") or 0),
                "top_slot_count": self.slot_count,
                "enabled_slots": list(self.enabled_slots),
                "synced_at": self.generated_at,
                "selection_bundle_version": self.bundle_version,
                "selection_bundle_hash": self.selection_bundle_hash,
                "selection_bundle_manifest_path": _relative_path(self.manifest_path),
                "selection_bundle_root_path": _relative_path(self.bundle_root_path),
                "selection_bundle_state_path": _relative_path(self.bundle_state_path),
            }
        )
        payload.setdefault("selection_symbols", list(self.selected_symbols))
        payload.setdefault("configured_top_symbols", list(self.selected_symbols))
        payload.setdefault("selection_bundle_version", self.bundle_version)
        payload.setdefault("selection_bundle_hash", self.selection_bundle_hash)
        payload.setdefault("selection_bundle_manifest_path", _relative_path(self.manifest_path))
        payload.setdefault("selection_bundle_root_path", _relative_path(self.bundle_root_path))
        payload.setdefault("selection_bundle_state_path", _relative_path(self.bundle_state_path))
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
            "tradable_requested_top_n": int(self.requested_top_n or self.slot_count),
            "tradable_selected_top_n": self.selected_top_n,
            "research_requested_top_n": int((self.summary or {}).get("research_requested_top_n") or self.requested_top_n or self.slot_count),
            "research_selected_top_n": int((self.summary or {}).get("research_selected_top_n") or 0),
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
            "selection_bundle_root_path": _relative_path(self.bundle_root_path),
            "selection_bundle_audit_path": _relative_path(self.bundle_audit_path),
        }

    def manifest_payload(self, bundle_root: Path | None = None) -> dict[str, Any]:
        report_payload = self.report_payload()
        state_payload = self.state_payload()
        audit_payload = self.audit_payload()
        bundle_root = Path(bundle_root) if bundle_root is not None else self.bundle_root_path
        bundle_report_path = bundle_root / "ai_selection_report.json"
        bundle_state_path = bundle_root / "ai_selection_state.json"
        bundle_audit_path = bundle_root / "selection_sync_audit.json"
        bundle_metadata_path = bundle_root / "bundle_metadata.json"
        top_payloads = {}
        bundle_top_paths = [bundle_root / f"TOP{i}.yaml" for i in range(1, self.slot_count + 1)]
        for index, path in enumerate(bundle_top_paths, start=1):
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
            "pipeline_status": str(report_payload.get("pipeline_status") or report_payload.get("execution_status") or "COMPLETED"),
            "selection_outcome": str(report_payload.get("selection_outcome") or ""),
            "completed_with_selection": bool(report_payload.get("completed_with_selection", False)),
            "result_quality": self.result_quality,
            "research_admission": self.research_admission,
            "requested_top_n": int(self.requested_top_n or self.slot_count),
            "selected_top_n": self.selected_top_n,
            "tradable_requested_top_n": int(report_payload.get("tradable_requested_top_n") or self.requested_top_n or self.slot_count),
            "tradable_selected_top_n": int(report_payload.get("tradable_selected_top_n") or self.selected_top_n),
            "research_requested_top_n": int(report_payload.get("research_requested_top_n") or self.requested_top_n or self.slot_count),
            "research_selected_top_n": int(report_payload.get("research_selected_top_n") or 0),
            "selection_bundle_hash": self.selection_bundle_hash,
            "bundle_root": _relative_path(bundle_root),
            "bundle_metadata_path": _relative_path(bundle_metadata_path),
            "selection_symbols": list(self.selected_symbols),
            "disabled_slots": list(self.disabled_slots),
            "enabled_slots": list(self.enabled_slots),
            "research_top_candidates": list(report_payload.get("research_top_candidates") or []),
            "tradable_top_candidates": list(report_payload.get("tradable_top_candidates") or self.top_items),
            "next_validation_stage": str(report_payload.get("next_validation_stage") or ""),
            "next_validation_stage_label": str(report_payload.get("next_validation_stage_label") or ""),
            "validation_pipeline_summary": dict(report_payload.get("validation_pipeline_summary") or {}),
            "selection_funnel": dict(report_payload.get("selection_funnel") or {}),
            "rejection_reason_counts": dict(report_payload.get("rejection_reason_counts") or {}),
            "nearest_rejected_candidates": list(report_payload.get("nearest_rejected_candidates") or []),
            "slot_count": self.slot_count,
            "top_slot_count": self.slot_count,
            "top_count": len(self.top_items),
            "paths": {
                "report_latest": _relative_path(self.report_latest_path),
                "report_dated": _relative_path(self.report_dated_path),
                "state": _relative_path(self.state_path),
                "audit": _relative_path(self.audit_path),
                "manifest": _relative_path(self.manifest_path),
                "bundle_root": _relative_path(bundle_root),
                "bundle_report": _relative_path(bundle_report_path),
                "bundle_state": _relative_path(bundle_state_path),
                "bundle_audit": _relative_path(bundle_audit_path),
                "bundle_metadata": _relative_path(bundle_metadata_path),
                "top": top_payloads,
            },
            "hashes": {
                "report_latest": _sha256_text(_json_dump(report_payload)),
                "report_dated": _sha256_text(_json_dump(report_payload)),
                "state": _sha256_text(_json_dump(state_payload)),
                "audit": _sha256_text(_json_dump(audit_payload)),
                "top": {
                    f"TOP{index}.yaml": _sha256_file(path)
                    for index, path in enumerate(bundle_top_paths, start=1)
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


def load_selection_bundle_manifest(project_dir: Path | None = None) -> dict[str, Any] | None:
    root = Path(project_dir) if project_dir is not None else _project_dir()
    manifest = _load_json(root / "state" / "selection_bundle_manifest.json")
    return manifest if isinstance(manifest, dict) else None


def load_committed_selection_bundle(project_dir: Path | None = None) -> dict[str, Any] | None:
    root = Path(project_dir) if project_dir is not None else _project_dir()
    manifest = load_selection_bundle_manifest(root)
    if not isinstance(manifest, dict):
        return None

    bundle_root_raw = str(
        manifest.get("bundle_root")
        or manifest.get("selection_bundle_root_path")
        or manifest.get("bundle_metadata_path")
        or ""
    ).strip()
    bundle_root = Path(bundle_root_raw)
    if bundle_root_raw and not bundle_root.is_absolute():
        bundle_root = root / bundle_root_raw

    report_candidates = [bundle_root / "ai_selection_report.json"]
    state_candidates = [bundle_root / "ai_selection_state.json"]
    audit_candidates = [bundle_root / "selection_sync_audit.json"]
    metadata_candidates = [bundle_root / "bundle_metadata.json"]

    report = next((data for path in report_candidates if (data := _load_json(path)) is not None), None)
    state = next((data for path in state_candidates if (data := _load_json(path)) is not None), None)
    audit = next((data for path in audit_candidates if (data := _load_json(path)) is not None), None)
    metadata = next((data for path in metadata_candidates if (data := _load_json(path)) is not None), None)

    if not isinstance(report, dict):
        return None

    bundle_root_rel = _relative_path(bundle_root)
    manifest_rel = _relative_path(root / "state" / "selection_bundle_manifest.json")
    report = dict(report)
    report.setdefault("selection_bundle_manifest_path", manifest_rel)
    report.setdefault("selection_bundle_root_path", bundle_root_rel)
    report.setdefault("selection_bundle_version", str(manifest.get("bundle_version") or report.get("selection_bundle_version") or ""))
    report.setdefault("selection_bundle_hash", str(manifest.get("selection_bundle_hash") or report.get("selection_bundle_hash") or ""))
    report.setdefault("selection_bundle_bundle_path", bundle_root_rel)
    report.setdefault("source_bundle_root", bundle_root_rel)
    report.setdefault("source_bundle_manifest_path", manifest_rel)
    if isinstance(state, dict):
        state = dict(state)
        state.setdefault("selection_bundle_manifest_path", manifest_rel)
        state.setdefault("selection_bundle_root_path", bundle_root_rel)
        state.setdefault("selection_bundle_version", str(manifest.get("bundle_version") or state.get("selection_bundle_version") or ""))
        state.setdefault("selection_bundle_hash", str(manifest.get("selection_bundle_hash") or state.get("selection_bundle_hash") or ""))
    if isinstance(audit, dict):
        audit = dict(audit)
        audit.setdefault("selection_bundle_manifest_path", manifest_rel)
        audit.setdefault("selection_bundle_root_path", bundle_root_rel)
    result: dict[str, Any] = {
        "manifest": dict(manifest),
        "report": report,
        "state": state if isinstance(state, dict) else {},
        "audit": audit if isinstance(audit, dict) else {},
        "metadata": metadata if isinstance(metadata, dict) else {},
        "bundle_root": bundle_root,
    }
    if isinstance(metadata, dict):
        result["metadata"] = dict(metadata)
    return result


def _bundle_validation_errors(bundle: SelectionBundle) -> list[str]:
    from src.ai_selector.data_quality import formal_selection_ineligibility_reasons, is_formal_selection_eligible

    errors: list[str] = []
    provider_audit = bundle.summary.get("provider_audit") if isinstance(bundle.summary, dict) else {}
    provider_contributors = []
    provider_attempts = 0
    provider_successes = 0
    provider_failures = 0
    if isinstance(provider_audit, dict):
        provider_contributors = list(provider_audit.get("provider_contributors") or provider_audit.get("contributors") or [])
        provider_attempts = int(provider_audit.get("provider_attempts", 0) or 0)
        provider_successes = int(provider_audit.get("provider_successes", 0) or 0)
        provider_failures = int(provider_audit.get("provider_failures", 0) or 0)
        records = provider_audit.get("records") if isinstance(provider_audit.get("records"), list) else None
        if records is None:
            records = [value for value in provider_audit.values() if isinstance(value, dict)]
        if records and provider_attempts <= 0:
            for record in records:
                attempted = int(record.get("attempted", 0) or 0)
                success = int(record.get("success", 0) or 0)
                provider_attempts += attempted
                provider_successes += success
                provider_failures += int(record.get("failure", 0) or max(0, attempted - success))
                provider_contributors.extend(str(field) for field in (record.get("contributed_fields") or []) if str(field).strip())
            provider_contributors = sorted(set(provider_contributors))
    all_providers_failed = provider_attempts > 0 and provider_successes <= 0 and provider_failures >= provider_attempts
    for bucket_name in ("top3", "top5", "top10", "research_candidates", "ranked_candidates"):
        bucket = bundle.summary.get(bucket_name) if isinstance(bundle.summary, dict) else None
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or item.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN"
            if all_providers_failed and item.get("research_complete") is True:
                errors.append(f"research_complete_with_provider_failure:{ticker}")
            score_provider = str(item.get("score_provider") or item.get("source") or "").strip().lower()
            score_source = str(item.get("score_source") or "").strip().lower()
            if not provider_contributors and score_provider in {"ai", "tradingagents", "finrobot", "openbb"} and score_source in {"", "ai", "provider", "research_provider"}:
                errors.append(f"ai_score_without_provider_contribution:{ticker}")
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

    for item in bundle.top_items:
        ticker = str(item.get("ticker") or "").strip().upper() or "UNKNOWN"
        if not is_formal_selection_eligible(item):
            reasons = formal_selection_ineligibility_reasons(item)
            errors.append(f"formal_top_ineligible:{ticker}")
            errors.extend(f"{ticker}:{reason}" for reason in reasons[:3])
            break
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
    bundle_root = bundle.bundle_root_path
    bundle_work_root = _bundle_staging_dir(bundle.selection_run_id, bundle.bundle_version)
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
        if bundle_work_root.exists():
            shutil.rmtree(bundle_work_root, ignore_errors=True)
        bundle_work_root.parent.mkdir(parents=True, exist_ok=True)
        bundle_work_root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(bundle_work_root / "ai_selection_report.json", report_payload)
        _write_json_atomic(bundle_work_root / "ai_selection_state.json", state_payload)
        _write_json_atomic(bundle_work_root / "selection_sync_audit.json", audit_payload)
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
            output_dir=bundle_work_root,
        )
        bundle_root.parent.mkdir(parents=True, exist_ok=True)
        if bundle_root.exists():
            shutil.rmtree(bundle_root, ignore_errors=True)
        os.replace(bundle_work_root, bundle_root)
        manifest_payload = bundle.manifest_payload(bundle_root=bundle_root)
        _write_json_atomic(bundle_root / "bundle_metadata.json", manifest_payload)
        _write_json_atomic(manifest_path, manifest_payload)

        _write_json_atomic(latest_report_path, report_payload)
        _write_json_atomic(dated_report_path, report_payload)
        selection_state.write_selection_state(
            et_date=str(state_payload.get("et_date") or bundle.selection_date),
            generated_at=str(state_payload.get("generated_at") or bundle.generated_at),
            selected_symbols=list(state_payload.get("selected_symbols") or []),
            report_path=_relative_path(latest_report_path),
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
            selection_bundle_root_path=_relative_path(bundle_root),
            selection_bundle_report_path=_relative_path(bundle_root / "ai_selection_report.json"),
            selection_bundle_state_path=_relative_path(bundle_root / "ai_selection_state.json"),
            selection_bundle_audit_path=_relative_path(bundle_root / "selection_sync_audit.json"),
            requested_top_n=int(state_payload.get("requested_top_n") or bundle.slot_count),
            selected_top_n=int(state_payload.get("selected_top_n") or bundle.selected_top_n),
            top_slot_count=int(state_payload.get("top_slot_count") or bundle.slot_count),
            enabled_slots=list(state_payload.get("enabled_slots") or bundle.enabled_slots),
            research_requested_top_n=int(state_payload.get("research_requested_top_n") or bundle.requested_top_n or bundle.slot_count),
            research_selected_top_n=int(state_payload.get("research_selected_top_n") or 0),
            tradable_requested_top_n=int(state_payload.get("tradable_requested_top_n") or bundle.requested_top_n or bundle.slot_count),
            tradable_selected_top_n=int(state_payload.get("tradable_selected_top_n") or state_payload.get("selected_top_n") or bundle.selected_top_n),
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
    except Exception:
        _restore_backups(backups)
        try:
            if bundle_root.exists():
                shutil.rmtree(bundle_root, ignore_errors=True)
        except Exception:
            pass
        try:
            if bundle_work_root.exists():
                shutil.rmtree(bundle_work_root, ignore_errors=True)
        except Exception:
            pass
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
        "bundle_root_path": _relative_path(bundle_root),
        "bundle_report_path": _relative_path(bundle_root / "ai_selection_report.json"),
        "bundle_state_path": _relative_path(bundle_root / "ai_selection_state.json"),
        "bundle_audit_path": _relative_path(bundle_root / "selection_sync_audit.json"),
        "bundle_metadata_path": _relative_path(bundle_root / "bundle_metadata.json"),
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
