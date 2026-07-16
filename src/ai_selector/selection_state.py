from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from src.ai_selector.config import load_runtime_config

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))


def _state_dir() -> Path:
    raw = os.environ.get("SOXS_STATE_DIR")
    return Path(raw) if raw else PROJECT_DIR / "state"


def selection_state_path() -> Path:
    return _state_dir() / "ai_selection_state.json"


def configured_top_count() -> int:
    try:
        return max(1, int(load_runtime_config().top_n))
    except Exception:
        return 3


def _top_slot_limit(limit: int | None = None) -> int:
    if limit is None:
        limit = configured_top_count()
    try:
        return max(3, int(limit))
    except Exception:
        return max(3, configured_top_count())


def _read_top_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        config = {}
    return config if isinstance(config, dict) else {}


def current_top_config_slots(limit: int | None = None) -> list[dict[str, Any]]:
    slot_limit = _top_slot_limit(limit)
    slots: list[dict[str, Any]] = []
    for index in range(1, slot_limit + 1):
        path = PROJECT_DIR / "configs" / f"TOP{index}.yaml"
        exists = path.exists()
        config = _read_top_config(path) if exists else {}
        enabled_raw = config.get("enabled")
        enabled = bool(True if enabled_raw is None else enabled_raw)
        ticker = str(config.get("ticker") or "").strip().upper()
        reason = str(config.get("reason") or "").strip()
        slots.append(
            {
                "slot": index,
                "path": path,
                "exists": exists,
                "enabled": enabled and bool(ticker),
                "ticker": ticker,
                "reason": reason,
                "selection_run_id": str(config.get("selection_run_id") or ""),
                "top_sync_run_id": str(config.get("top_sync_run_id") or config.get("selection_run_id") or ""),
                "selection_date": str(config.get("selection_date") or ""),
                "generated_at": str(config.get("generated_at") or ""),
                "result_quality": str(config.get("result_quality") or ""),
                "research_admission": str(config.get("research_admission") or ""),
                "top_sync_status": str(config.get("top_sync_status") or ""),
                "top_sync_error": str(config.get("top_sync_error") or ""),
            }
        )
    return slots


def current_top_config_disabled_slots(limit: int | None = None) -> list[int]:
    return [int(slot["slot"]) for slot in current_top_config_slots(limit=limit) if not slot.get("enabled")]


def current_top_config_run_id(limit: int | None = None) -> str | None:
    run_ids = [str(slot.get("selection_run_id") or "") for slot in current_top_config_slots(limit=limit) if str(slot.get("selection_run_id") or "").strip()]
    if not run_ids:
        return None
    first = run_ids[0]
    if all(run_id == first for run_id in run_ids):
        return first
    return None


def current_top_config_result_quality(limit: int | None = None) -> str | None:
    qualities = [str(slot.get("result_quality") or "").strip().upper() for slot in current_top_config_slots(limit=limit) if str(slot.get("result_quality") or "").strip()]
    if not qualities:
        return None
    first = qualities[0]
    if all(item == first for item in qualities):
        return first
    return None


def current_top_config_research_admission(limit: int | None = None) -> str | None:
    admissions = [str(slot.get("research_admission") or "").strip().upper() for slot in current_top_config_slots(limit=limit) if str(slot.get("research_admission") or "").strip()]
    if not admissions:
        return None
    first = admissions[0]
    if all(item == first for item in admissions):
        return first
    return None


def current_top_config_file_state(limit: int | None = None) -> list[dict[str, Any]]:
    return current_top_config_slots(limit=limit)


def current_top_config_symbols_and_slots(limit: int | None = None) -> tuple[list[str], list[int]]:
    slots = current_top_config_slots(limit=limit)
    return (
        [str(slot.get("ticker") or "").strip().upper() for slot in slots if slot.get("enabled") and slot.get("ticker")],
        [int(slot["slot"]) for slot in slots if not slot.get("enabled")],
    )


def current_top_config_symbols(limit: int | None = None) -> list[str]:
    if limit is None:
        limit = configured_top_count()
    symbols: list[str] = []
    for slot in current_top_config_slots(limit=limit):
        if slot.get("enabled") and slot.get("ticker"):
            symbols.append(str(slot.get("ticker") or "").strip().upper())
    return symbols


def has_live_top_configs(limit: int | None = None) -> bool:
    for slot in current_top_config_slots(limit=limit):
        if not slot.get("enabled"):
            continue
        path = slot.get("path")
        try:
            config = _read_top_config(Path(path))
        except Exception:
            config = {}
        if str(config.get("mode") or "").strip().lower() == "live":
            return True
    return False


def load_selection_state() -> dict[str, Any] | None:
    path = selection_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_selection_state(
    *,
    et_date: str,
    generated_at: str,
    selected_symbols: list[str],
    report_path: str | None = None,
    selection_stage: str | None = None,
    processing_phase: str | None = None,
    selection_run_id: str | None = None,
    top_sync_run_id: str | None = None,
    top_sync_status: str | None = None,
    top_sync_error: str | None = None,
    selection_symbols: list[str] | None = None,
    configured_top_symbols: list[str] | None = None,
    disabled_slots: list[int] | None = None,
    synced_at: str | None = None,
    result_quality: str | None = None,
    research_admission: str | None = None,
) -> Path:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    selection_symbols = list(selection_symbols or selected_symbols or [])
    configured_top_symbols = list(configured_top_symbols or current_top_config_symbols(limit=max(configured_top_count(), len(selected_symbols))))
    disabled_slots = list(disabled_slots or current_top_config_disabled_slots(limit=max(configured_top_count(), len(selected_symbols))))
    selection_run_id = str(selection_run_id or "")
    top_sync_run_id = str(top_sync_run_id or selection_run_id or "")
    payload = {
        "et_date": et_date,
        "generated_at": generated_at,
        "selected_symbols": list(selected_symbols),
        "selection_symbols": selection_symbols,
        "top_config_symbols": configured_top_symbols,
        "configured_top_symbols": configured_top_symbols,
        "disabled_slots": disabled_slots,
        "report_path": report_path,
        "selection_stage": str(selection_stage or ""),
        "processing_phase": str(processing_phase or ""),
        "selection_run_id": selection_run_id,
        "top_sync_run_id": top_sync_run_id,
        "top_sync_status": str(top_sync_status or ""),
        "top_sync_error": str(top_sync_error or ""),
        "synced_at": str(synced_at or generated_at),
        "result_quality": str(result_quality or ""),
        "research_admission": str(research_admission or ""),
        "updated_at": datetime.now().isoformat(),
    }
    path = selection_state_path()
    tmp_path = path.with_name(f"{path.name}.tmp-{selection_run_id}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def verify_selection_state(required_et_date: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
    state = load_selection_state()
    if not state:
        return False, "selection_state_missing", None

    state = dict(state)
    state_date = str(state.get("et_date") or "").strip()
    if required_et_date and state_date != required_et_date:
        state["required_et_date"] = required_et_date
        state["selection_state_symbols"] = [
            str(item or "").strip().upper()
            for item in state.get("selected_symbols") or state.get("selection_symbols") or []
            if str(item or "").strip()
        ]
        state["state_top_config_symbols"] = [
            str(item or "").strip().upper()
            for item in state.get("top_config_symbols") or state.get("configured_top_symbols") or []
            if str(item or "").strip()
        ]
        state["current_top_config_symbols"] = current_top_config_symbols(
            limit=max(configured_top_count(), len(state.get("selected_symbols") or state.get("selection_symbols") or []))
        )
        state["disabled_slots"] = list(state.get("disabled_slots") or current_top_config_disabled_slots())
        state["selection_run_id"] = str(state.get("selection_run_id") or "")
        state["top_sync_run_id"] = str(state.get("top_sync_run_id") or "")
        return False, f"selection_state_date_mismatch:{state_date or 'missing'}", state

    expected_selected = [
        str(item or "").strip().upper()
        for item in state.get("selected_symbols") or state.get("selection_symbols") or []
        if str(item or "").strip()
    ]
    expected_top_configs = [
        str(item or "").strip().upper()
        for item in state.get("top_config_symbols") or state.get("configured_top_symbols") or []
        if str(item or "").strip()
    ]
    slots = current_top_config_slots(limit=max(configured_top_count(), len(expected_selected), len(expected_top_configs)))
    actual = [str(slot.get("ticker") or "").strip().upper() for slot in slots if slot.get("enabled") and slot.get("ticker")]
    missing_slots = [int(slot["slot"]) for slot in slots if not slot.get("exists")]
    disabled_slots = [int(slot["slot"]) for slot in slots if slot.get("exists") and not slot.get("enabled")]
    actual_run_id = current_top_config_run_id(limit=len(slots))
    state["selection_state_symbols"] = expected_selected
    state["state_top_config_symbols"] = expected_top_configs
    state["current_top_config_symbols"] = actual
    state["disabled_slots"] = disabled_slots
    state["selection_run_id"] = str(state.get("selection_run_id") or "")
    state["top_sync_run_id"] = str(state.get("top_sync_run_id") or "")
    state["selection_symbols"] = expected_selected
    state["configured_top_symbols"] = expected_top_configs or actual
    state["current_top_config_run_id"] = current_top_config_run_id(limit=len(slots))
    state["current_top_config_result_quality"] = current_top_config_result_quality(limit=len(slots)) or ""
    state["current_top_config_research_admission"] = current_top_config_research_admission(limit=len(slots)) or ""
    state_disabled_slots = [
        int(item)
        for item in state.get("disabled_slots") or []
        if str(item).strip()
    ]
    if missing_slots:
        return False, "missing_top_slot", state
    if not state.get("selection_run_id") or not state.get("top_sync_run_id"):
        # Legacy files can still validate if all symbols line up, but not as an
        # atomic bundle.
        pass
    if state.get("selection_run_id") and actual_run_id and str(state.get("selection_run_id")) != str(actual_run_id):
        return False, "run_id_mismatch", state
    if state.get("top_sync_run_id") and actual_run_id and str(state.get("top_sync_run_id")) != str(actual_run_id):
        return False, "run_id_mismatch", state
    if (state.get("selection_run_id") or state.get("top_sync_run_id")) and not actual_run_id:
        return False, "bundle_incomplete", state
    if expected_selected != actual:
        return False, "top_config_symbols_mismatch", state
    if expected_top_configs and expected_top_configs != actual:
        return False, "top_config_symbols_mismatch", state
    if state_disabled_slots and state_disabled_slots != disabled_slots:
        return False, "disabled_slot_mismatch", state
    state_result_quality = str(state.get("result_quality") or "").strip().upper()
    actual_result_quality = str(state.get("current_top_config_result_quality") or "").strip().upper()
    if state_result_quality and actual_result_quality and state_result_quality != actual_result_quality:
        return False, "result_quality_mismatch", state
    state_research_admission = str(state.get("research_admission") or "").strip().upper()
    actual_research_admission = str(state.get("current_top_config_research_admission") or "").strip().upper()
    if state_research_admission and actual_research_admission and state_research_admission != actual_research_admission:
        return False, "research_admission_mismatch", state

    return True, "ok", state


def verify_live_startup_selection(required_et_date: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
    if not has_live_top_configs():
        return True, "no_live_top_configs", load_selection_state()
    return verify_selection_state(required_et_date=required_et_date)
