from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[2]

_LONGBRIDGE_KEY_MAP = {
    "LONGBRIDGE_APP_KEY": "app_key",
    "LONGBRIDGE_API_KEY": "app_key",
    "LONGBRIDGE_APP_SECRET": "app_secret",
    "LONGBRIDGE_API_SECRET": "app_secret",
    "LONGBRIDGE_ACCESS_TOKEN": "access_token",
    "LONGBRIDGE_REGION": "region",
    "LONGBRIDGE_ENABLED": "enabled",
    "LONGBRIDGE_ENV": "environment",
    "LONGBRIDGE_HTTP_URL": "http_url",
    "LONGBRIDGE_BASE_URL": "http_url",
    "LONGBRIDGE_QUOTE_WS_URL": "quote_ws_url",
    "LONGBRIDGE_TRADE_WS_URL": "trade_ws_url",
    "LONGBRIDGE_LOG_PATH": "log_path",
}


def _candidate_config_paths() -> list[Path]:
    return [
        PROJECT_DIR / "config.local.yaml",
        PROJECT_DIR / "config.yaml",
    ]


@lru_cache(maxsize=1)
def load_private_longbridge_config() -> dict[str, Any]:
    for path in _candidate_config_paths():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        broker = raw.get("broker") if isinstance(raw, dict) else {}
        longbridge = broker.get("longbridge") if isinstance(broker, dict) else {}
        if isinstance(longbridge, dict):
            return dict(longbridge)
    return {}


def clear_private_config_cache() -> None:
    load_private_longbridge_config.cache_clear()


def _launchctl_getenv(name: str) -> str:
    try:
        proc = subprocess.run(
            ["launchctl", "getenv", name],
            capture_output=True,
            text=True,
            check=False,
        )
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def get_runtime_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    mapped_key = _LONGBRIDGE_KEY_MAP.get(name)
    if mapped_key:
        config = load_private_longbridge_config()
        candidate = config.get(mapped_key)
        if candidate not in (None, ""):
            return str(candidate).strip()

    launchd_value = _launchctl_getenv(name)
    if launchd_value:
        return launchd_value
    return default.strip()


def has_longbridge_runtime_credentials() -> bool:
    return bool(
        get_runtime_env("LONGBRIDGE_ACCESS_TOKEN")
        and get_runtime_env("LONGBRIDGE_APP_KEY")
        and get_runtime_env("LONGBRIDGE_APP_SECRET")
    )
