from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[2]


def _state_dir() -> Path:
    raw = os.environ.get("SOXS_STATE_DIR")
    return Path(raw) if raw else PROJECT_DIR / "state"


def selection_state_path() -> Path:
    return _state_dir() / "ai_selection_state.json"


def current_top_config_symbols(limit: int = 5) -> list[str]:
    symbols: list[str] = []
    for index in range(1, limit + 1):
        path = PROJECT_DIR / "configs" / f"TOP{index}.yaml"
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            config = {}
        symbol = str(config.get("ticker") or "").strip().upper()
        if symbol:
            symbols.append(symbol)
    return symbols


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
) -> Path:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "et_date": et_date,
        "generated_at": generated_at,
        "selected_symbols": list(selected_symbols),
        "top_config_symbols": current_top_config_symbols(limit=max(5, len(selected_symbols))),
        "report_path": report_path,
        "updated_at": datetime.now().isoformat(),
    }
    path = selection_state_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def verify_selection_state(required_et_date: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
    state = load_selection_state()
    if not state:
        return False, "selection_state_missing", None

    state_date = str(state.get("et_date") or "").strip()
    if required_et_date and state_date != required_et_date:
        return False, f"selection_state_date_mismatch:{state_date or 'missing'}", state

    expected = [str(item or "").strip().upper() for item in state.get("selected_symbols") or [] if str(item or "").strip()]
    actual = current_top_config_symbols(limit=max(5, len(expected)))
    if expected != actual:
        return False, "top_config_symbols_mismatch", state

    return True, "ok", state
