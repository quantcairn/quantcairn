from __future__ import annotations

import json
import os
import importlib
import shutil
import uuid
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any

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


def _call_write_top_configs(writer, top_items: list[dict[str, Any]], **kwargs) -> Any:
    try:
        signature = inspect.signature(writer)
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        supports_named = "selection_run_id" in signature.parameters or accepts_kwargs
    except Exception:
        supports_named = True

    if supports_named:
        return writer(list(top_items or []), **kwargs)
    return writer(list(top_items or []))


def write_selection_bundle_atomic(
    *,
    selection_state_payload: dict[str, Any],
    top_items: list[dict[str, Any]],
    selection_run_id: str | None = None,
    selection_date: str | None = None,
    generated_at: str | None = None,
    result_quality: str | None = None,
    research_admission: str | None = None,
    processing_phase: str | None = None,
    top_sync_status: str = "OK",
    top_sync_error: str = "",
    disabled_reason: str | None = None,
) -> dict[str, Any]:
    """Atomically persist the selector state bundle.

    The bundle consists of the selection state, the fixed TOP files and a
    sync audit record. If any write fails, previously updated files are
    restored from backups so the user never sees a half-updated bundle.
    """

    selection_run_id = _selection_run_id(selection_run_id)
    generated_at = str(generated_at or datetime.now().isoformat())
    selection_date = str(selection_date or required_selection_date())
    disabled_reason = str(disabled_reason or ("selection_blocked" if result_quality == "INVALID" or research_admission == "BLOCKED" else "top_n_not_filled"))

    config_writer = importlib.import_module("src.ai_selector.config_writer")
    selection_state = importlib.import_module("src.ai_selector.selection_state")

    state_path = selection_state.selection_state_path()
    audit_path = _state_dir() / "selection_sync_audit.json"
    slot_count = max(3, selection_state.configured_top_count(), len(list(top_items or [])))
    top_paths = [Path(_project_dir()) / "configs" / f"TOP{i}.yaml" for i in range(1, slot_count + 1)]

    backup_root = _state_dir() / ".selection_bundle_backups" / selection_run_id
    backups = _backup_files([state_path, audit_path, *top_paths], backup_root)

    state_payload = dict(selection_state_payload or {})
    state_payload.update(
        {
            "selection_run_id": selection_run_id,
            "top_sync_run_id": selection_run_id,
            "top_sync_status": str(top_sync_status or "OK"),
            "top_sync_error": str(top_sync_error or ""),
            "processing_phase": str(processing_phase or state_payload.get("processing_phase") or ""),
            "selection_symbols": [
                str(item.get("ticker") or "").strip().upper()
                for item in list(top_items or [])
                if str(item.get("ticker") or "").strip()
            ],
            "configured_top_symbols": [
                str(item.get("ticker") or "").strip().upper()
                for item in list(top_items or [])
                if str(item.get("ticker") or "").strip()
            ],
            "disabled_slots": [
                i
                for i in range(1, slot_count + 1)
                if i > len(list(top_items or []))
            ],
            "synced_at": generated_at,
        }
    )
    state_payload.setdefault("selection_symbols", list(state_payload.get("selected_symbols") or []))
    state_payload.setdefault("configured_top_symbols", list(state_payload.get("selection_symbols") or []))

    audit_payload = {
        "selection_run_id": selection_run_id,
        "top_sync_run_id": selection_run_id,
        "top_sync_status": str(top_sync_status or "OK"),
        "top_sync_error": str(top_sync_error or ""),
        "selection_date": selection_date,
        "generated_at": generated_at,
        "selection_stage": str(state_payload.get("selection_stage") or ""),
        "top_count": len(list(top_items or [])),
        "slot_count": slot_count,
        "disabled_slots": state_payload["disabled_slots"],
        "selection_symbols": state_payload["selection_symbols"],
        "configured_top_symbols": state_payload["configured_top_symbols"],
        "result_quality": str(result_quality or ""),
        "research_admission": str(research_admission or ""),
    }

    try:
        _call_write_top_configs(
            config_writer.write_top_configs,
            list(top_items or []),
            selection_run_id=selection_run_id,
            selection_date=selection_date,
            generated_at=generated_at,
            disabled_reason=disabled_reason,
            result_quality=result_quality,
            research_admission=research_admission,
            top_sync_status=top_sync_status,
            top_sync_error=top_sync_error,
        )
        selection_state.write_selection_state(
            et_date=str(state_payload.get("et_date") or selection_date),
            generated_at=str(state_payload.get("generated_at") or generated_at),
            selected_symbols=list(state_payload.get("selected_symbols") or state_payload.get("selection_symbols") or []),
            report_path=state_payload.get("report_path"),
            selection_stage=str(state_payload.get("selection_stage") or ""),
            selection_run_id=selection_run_id,
            top_sync_run_id=selection_run_id,
            top_sync_status=top_sync_status,
            top_sync_error=top_sync_error,
            selection_symbols=list(state_payload.get("selection_symbols") or []),
            configured_top_symbols=list(state_payload.get("configured_top_symbols") or []),
            disabled_slots=list(state_payload.get("disabled_slots") or []),
            synced_at=generated_at,
            result_quality=result_quality,
            research_admission=research_admission,
            processing_phase=str(state_payload.get("processing_phase") or processing_phase or ""),
        )
        audit_path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        _restore_backups(backups)
        raise
    else:
        _cleanup_backups(backups)

    return {
        "selection_run_id": selection_run_id,
        "top_sync_run_id": selection_run_id,
        "top_sync_status": top_sync_status,
        "top_sync_error": top_sync_error,
        "selection_date": selection_date,
        "generated_at": generated_at,
        "selection_stage": str(state_payload.get("selection_stage") or ""),
        "disabled_slots": state_payload["disabled_slots"],
        "audit_path": str(audit_path),
        "state_path": str(state_path),
        "top_paths": [str(path) for path in top_paths],
    }
