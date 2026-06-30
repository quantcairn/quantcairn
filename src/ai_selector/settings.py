from __future__ import annotations

import json
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
SETTINGS_PATH = PROJECT_DIR / "state" / "ai_selector_settings.json"


def load_runtime_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_runtime_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_float_setting(name: str, default: float) -> float:
    data = load_runtime_settings()
    value = data.get(name)
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)
