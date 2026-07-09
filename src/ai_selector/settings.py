from __future__ import annotations

import json
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))
SETTINGS_PATH = STATE_DIR / "ai_selector_settings.json"
DEFAULT_MIN_PRICE = 4.0
DEFAULT_MAX_PRICE = 50.0


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


def resolve_price_band(settings: dict | None = None) -> tuple[float, float]:
    data = dict(settings or {})
    try:
        min_price = float(data.get("min_price", DEFAULT_MIN_PRICE) or DEFAULT_MIN_PRICE)
    except (TypeError, ValueError):
        min_price = DEFAULT_MIN_PRICE
    try:
        max_price = float(data.get("max_price", DEFAULT_MAX_PRICE) or DEFAULT_MAX_PRICE)
    except (TypeError, ValueError):
        max_price = DEFAULT_MAX_PRICE
    if min_price > max_price:
        min_price, max_price = max_price, min_price
    return round(min_price, 2), round(max_price, 2)
