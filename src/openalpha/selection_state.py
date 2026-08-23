from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config.runtime_paths import resolve_state_dir

import yaml
from src.openalpha.config import load_runtime_config

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))


def _state_dir() -> Path:
    raw = os.environ.get("SOXS_STATE_DIR")
    return resolve_state_dir(PROJECT_DIR) if not raw else Path(raw).expanduser().resolve()


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


def _normalized_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _nested_selection(config: dict[str, Any]) -> dict[str, Any]:
    selection = config.get("selection")
    return selection if isinstance(selection, dict) else {}


def _normalize_path_value(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        return text
    parts = list(path.parts)
    for marker in ("reports", "state", "configs", "artifacts"):
        if marker in parts:
            index = parts.index(marker)
            return str(Path(*parts[index:]))
    try:
        return str(path.relative_to(PROJECT_DIR))
    except Exception:
        return path.name


def current_top_config_slots(limit: int | None = None) -> list[dict[str, Any]]:
    slot_limit = _top_slot_limit(limit)
    slots: list[dict[str, Any]] = []
    for index in range(1, slot_limit + 1):
        path = PROJECT_DIR / "configs" / f"TOP{index}.yaml"
        exists = path.exists()
        config = _read_top_config(path) if exists else {}
        selection = _nested_selection(config)
        enabled_raw = config.get("enabled")
        enabled = bool(True if enabled_raw is None else enabled_raw)
        ticker = _normalized_symbol(config.get("ticker"))
        reason = str(config.get("reason") or "").strip()
        mode = str(config.get("mode") or "").strip().lower()
        selection_date = str(config.get("selection_date") or "").strip()
        nested_selection_date = str(selection.get("selection_date") or "").strip()
        nested_symbol = _normalized_symbol(selection.get("symbol") or selection.get("ticker"))
        nested_mode = str(selection.get("mode") or "").strip().lower()
        metadata_mismatches: list[str] = []
        if selection_date and nested_selection_date and selection_date != nested_selection_date:
            metadata_mismatches.append("nested_selection_date_mismatch")
        if ticker and nested_symbol and ticker != nested_symbol:
            metadata_mismatches.append("nested_symbol_mismatch")
        if mode and nested_mode and mode != nested_mode:
            metadata_mismatches.append("nested_mode_mismatch")
        slots.append(
            {
                "slot": index,
                "path": path,
                "exists": exists,
                "enabled": enabled and bool(ticker),
                "ticker": ticker,
                "mode": mode,
                "reason": reason,
                "selection_run_id": str(config.get("selection_run_id") or ""),
                "top_sync_run_id": str(config.get("top_sync_run_id") or config.get("selection_run_id") or ""),
                "selection_date": selection_date,
                "nested_selection_date": nested_selection_date,
                "nested_symbol": nested_symbol,
                "nested_mode": nested_mode,
                "selection_metadata_mismatches": metadata_mismatches,
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


def validate_top_config_sync(
    *,
    selected_symbols: list[str] | None = None,
    selection_date: str | None = None,
    selection_run_id: str | None = None,
    top_items: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    limit: int | None = None,
    slots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state_payload = dict(state or {}) if isinstance(state, dict) else {}
    expected_symbols = [
        _normalized_symbol(item)
        for item in (
            selected_symbols
            if selected_symbols is not None
            else state_payload.get("selected_symbols") or state_payload.get("selection_symbols") or []
        )
        if _normalized_symbol(item)
    ]
    expected_date = str(selection_date or state_payload.get("et_date") or state_payload.get("selection_date") or "").strip()
    expected_run_id = str(selection_run_id or state_payload.get("selection_run_id") or "").strip()
    items = [dict(item or {}) for item in list(top_items or [])]
    expected_modes: list[str] = []
    for item in items:
        expected_modes.append(str(item.get("mode") or "").strip().lower())
    slot_limit = _top_slot_limit(limit or max(configured_top_count(), len(expected_symbols), len(items)))
    slots = list(slots) if slots is not None else current_top_config_slots(limit=slot_limit)
    mismatches: list[str] = []

    for slot in slots:
        index = int(slot.get("slot") or 0)
        label = f"TOP{index}"
        expected_enabled = index <= len(expected_symbols)
        expected_symbol = expected_symbols[index - 1] if expected_enabled else ""
        actual_enabled = bool(slot.get("enabled"))
        actual_symbol = _normalized_symbol(slot.get("ticker"))
        actual_mode = str(slot.get("mode") or "").strip().lower()
        actual_date = str(slot.get("selection_date") or "").strip()
        nested_date = str(slot.get("nested_selection_date") or "").strip()
        nested_symbol = _normalized_symbol(slot.get("nested_symbol"))
        nested_mode = str(slot.get("nested_mode") or "").strip().lower()

        if not slot.get("exists"):
            mismatches.append(f"{label}:missing")
            continue
        if actual_enabled != expected_enabled:
            mismatches.append(f"{label}:enabled_state_mismatch:expected={expected_enabled}:actual={actual_enabled}")
        if expected_enabled and actual_symbol != expected_symbol:
            mismatches.append(f"{label}:symbol_mismatch:expected={expected_symbol}:actual={actual_symbol or 'NONE'}")
        if not expected_enabled and actual_symbol:
            mismatches.append(f"{label}:stale_symbol:{actual_symbol}")
        if expected_date and actual_date != expected_date:
            mismatches.append(f"{label}:selection_date_mismatch:expected={expected_date}:actual={actual_date or 'missing'}")
        if expected_date and nested_date and nested_date != expected_date:
            mismatches.append(f"{label}:nested_selection_date_mismatch:expected={expected_date}:actual={nested_date}")
        if expected_enabled and nested_symbol and nested_symbol != expected_symbol:
            mismatches.append(f"{label}:nested_symbol_mismatch:expected={expected_symbol}:actual={nested_symbol}")
        if not expected_enabled and nested_symbol:
            mismatches.append(f"{label}:nested_stale_symbol:{nested_symbol}")
        expected_mode = expected_modes[index - 1] if expected_enabled and index <= len(expected_modes) else ""
        if expected_enabled and expected_mode and actual_mode != expected_mode:
            mismatches.append(f"{label}:mode_mismatch:expected={expected_mode}:actual={actual_mode or 'missing'}")
        if not expected_enabled and actual_mode != "paper":
            mismatches.append(f"{label}:disabled_mode_mismatch:expected=paper:actual={actual_mode or 'missing'}")
        if nested_mode and actual_mode and nested_mode != actual_mode:
            mismatches.append(f"{label}:nested_mode_mismatch:expected={actual_mode}:actual={nested_mode}")
        if expected_run_id:
            actual_run_id = str(slot.get("selection_run_id") or "").strip()
            if actual_run_id != expected_run_id:
                mismatches.append(f"{label}:selection_run_id_mismatch:expected={expected_run_id}:actual={actual_run_id or 'missing'}")
        for metadata_mismatch in slot.get("selection_metadata_mismatches") or []:
            mismatches.append(f"{label}:{metadata_mismatch}")

    deduped_mismatches = list(dict.fromkeys(mismatches))
    return {
        "ok": not deduped_mismatches,
        "top_sync_status": "OK" if not deduped_mismatches else "NOT_OK",
        "top_sync_error": "" if not deduped_mismatches else ",".join(deduped_mismatches),
        "mismatches": deduped_mismatches,
        "expected_symbols": expected_symbols,
        "actual_symbols": [
            _normalized_symbol(slot.get("ticker"))
            for slot in slots
            if slot.get("enabled") and _normalized_symbol(slot.get("ticker"))
        ],
        "selection_date": expected_date,
        "selection_run_id": expected_run_id,
        "slots": slots,
    }


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
    selection_bundle_manifest_path: str | None = None,
    selection_bundle_hash: str | None = None,
    selection_bundle_version: str | None = None,
    selection_bundle_root_path: str | None = None,
    selection_bundle_report_path: str | None = None,
    selection_bundle_state_path: str | None = None,
    selection_bundle_audit_path: str | None = None,
    requested_top_n: int | None = None,
    selected_top_n: int | None = None,
    top_slot_count: int | None = None,
    enabled_slots: list[int] | None = None,
    research_requested_top_n: int | None = None,
    research_selected_top_n: int | None = None,
    tradable_requested_top_n: int | None = None,
    tradable_selected_top_n: int | None = None,
) -> Path:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    selection_symbols = list(selection_symbols or selected_symbols or [])
    configured_top_symbols = list(configured_top_symbols or current_top_config_symbols(limit=max(configured_top_count(), len(selected_symbols))))
    disabled_slots = list(disabled_slots or current_top_config_disabled_slots(limit=max(configured_top_count(), len(selected_symbols))))
    selection_run_id = str(selection_run_id or "")
    top_sync_run_id = str(top_sync_run_id or selection_run_id or "")
    selected_top_n_value = int(selected_top_n or len(selected_symbols))
    requested_top_n_value = int(requested_top_n or max(configured_top_count(), len(selected_symbols)))
    top_slot_count_value = int(top_slot_count or max(configured_top_count(), len(selected_symbols)))
    enabled_slots_value = list(enabled_slots or list(range(1, selected_top_n_value + 1)))
    research_requested_top_n_value = int(research_requested_top_n or requested_top_n_value)
    research_selected_top_n_value = int(research_selected_top_n or 0)
    tradable_requested_top_n_value = int(tradable_requested_top_n or requested_top_n_value)
    tradable_selected_top_n_value = int(tradable_selected_top_n if tradable_selected_top_n is not None else selected_top_n_value)
    payload = {
        "et_date": et_date,
        "generated_at": generated_at,
        "selected_symbols": list(selected_symbols),
        "selection_symbols": selection_symbols,
        "top_config_symbols": configured_top_symbols,
        "configured_top_symbols": configured_top_symbols,
        "disabled_slots": disabled_slots,
        "enabled_slots": enabled_slots_value,
        "requested_top_n": requested_top_n_value,
        "selected_top_n": selected_top_n_value,
        "research_requested_top_n": research_requested_top_n_value,
        "research_selected_top_n": research_selected_top_n_value,
        "tradable_requested_top_n": tradable_requested_top_n_value,
        "tradable_selected_top_n": tradable_selected_top_n_value,
        "top_slot_count": top_slot_count_value,
        "report_path": _normalize_path_value(report_path),
        "selection_stage": str(selection_stage or ""),
        "processing_phase": str(processing_phase or ""),
        "selection_run_id": selection_run_id,
        "top_sync_run_id": top_sync_run_id,
        "top_sync_status": str(top_sync_status or ""),
        "top_sync_error": str(top_sync_error or ""),
        "synced_at": str(synced_at or generated_at),
        "result_quality": str(result_quality or ""),
        "research_admission": str(research_admission or ""),
        "selection_bundle_manifest_path": _normalize_path_value(selection_bundle_manifest_path),
        "selection_bundle_hash": str(selection_bundle_hash or ""),
        "selection_bundle_version": str(selection_bundle_version or ""),
        "selection_bundle_root_path": _normalize_path_value(selection_bundle_root_path),
        "selection_bundle_report_path": _normalize_path_value(selection_bundle_report_path),
        "selection_bundle_state_path": _normalize_path_value(selection_bundle_state_path),
        "selection_bundle_audit_path": _normalize_path_value(selection_bundle_audit_path),
        "updated_at": datetime.now().isoformat(),
    }
    path = selection_state_path()
    tmp_path = path.with_name(f"{path.name}.tmp-{selection_run_id}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def verify_selection_state(
    required_et_date: str | None = None,
    *,
    state: dict[str, Any] | None = None,
    top_slots: list[dict[str, Any]] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    state = dict(state) if isinstance(state, dict) else load_selection_state()
    if not state:
        return False, "selection_state_missing", None

    state = dict(state)
    state_date = str(state.get("et_date") or "").strip()
    if required_et_date and state_date != required_et_date:
        slots_for_mismatch = list(top_slots) if top_slots is not None else None
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
        if slots_for_mismatch is None:
            state["current_top_config_symbols"] = current_top_config_symbols(
                limit=max(configured_top_count(), len(state.get("selected_symbols") or state.get("selection_symbols") or []))
            )
            state["disabled_slots"] = list(state.get("disabled_slots") or current_top_config_disabled_slots())
        else:
            state["current_top_config_symbols"] = [
                str(slot.get("ticker") or "").strip().upper()
                for slot in slots_for_mismatch
                if slot.get("enabled") and str(slot.get("ticker") or "").strip()
            ]
            state["disabled_slots"] = [
                int(slot.get("slot") or 0)
                for slot in slots_for_mismatch
                if not slot.get("enabled")
            ]
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
    slots = list(top_slots) if top_slots is not None else current_top_config_slots(limit=max(configured_top_count(), len(expected_selected), len(expected_top_configs)))
    actual = [str(slot.get("ticker") or "").strip().upper() for slot in slots if slot.get("enabled") and slot.get("ticker")]
    missing_slots = [int(slot["slot"]) for slot in slots if not slot.get("exists")]
    disabled_slots = [int(slot["slot"]) for slot in slots if slot.get("exists") and not slot.get("enabled")]
    run_ids = [str(slot.get("selection_run_id") or "") for slot in slots if str(slot.get("selection_run_id") or "").strip()]
    actual_run_id = run_ids[0] if run_ids and all(run_id == run_ids[0] for run_id in run_ids) else None
    state["selection_state_symbols"] = expected_selected
    state["state_top_config_symbols"] = expected_top_configs
    state["current_top_config_symbols"] = actual
    state["disabled_slots"] = disabled_slots
    state["selection_run_id"] = str(state.get("selection_run_id") or "")
    state["top_sync_run_id"] = str(state.get("top_sync_run_id") or "")
    state["selection_symbols"] = expected_selected
    state["configured_top_symbols"] = expected_top_configs or actual
    state["current_top_config_run_id"] = actual_run_id
    result_qualities = [str(slot.get("result_quality") or "").strip().upper() for slot in slots if str(slot.get("result_quality") or "").strip()]
    research_admissions = [str(slot.get("research_admission") or "").strip().upper() for slot in slots if str(slot.get("research_admission") or "").strip()]
    state["current_top_config_result_quality"] = result_qualities[0] if result_qualities and all(item == result_qualities[0] for item in result_qualities) else ""
    state["current_top_config_research_admission"] = research_admissions[0] if research_admissions and all(item == research_admissions[0] for item in research_admissions) else ""
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
    sync_result = validate_top_config_sync(
        selected_symbols=expected_selected,
        selection_date=state_date,
        selection_run_id=str(state.get("selection_run_id") or ""),
        state=state,
        limit=len(slots),
        slots=slots,
    )
    state["top_sync_validation"] = {
        "top_sync_status": str(sync_result.get("top_sync_status") or ""),
        "top_sync_error": str(sync_result.get("top_sync_error") or ""),
        "mismatches": list(sync_result.get("mismatches") or []),
        "expected_symbols": list(sync_result.get("expected_symbols") or []),
        "actual_symbols": list(sync_result.get("actual_symbols") or []),
    }
    if str(sync_result.get("top_sync_status") or "OK").upper() != "OK":
        state["top_sync_status"] = "NOT_OK"
        state["top_sync_error"] = str(sync_result.get("top_sync_error") or "top_config_sync_mismatch")
        return False, "top_config_sync_mismatch", state
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
