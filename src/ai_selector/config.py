from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


AI_SELECTOR_ENABLED = False
TOP_N = 3
UNIVERSE = [
    "NVDA",
    "MSFT",
    "AAPL",
    "PLTR",
    "AMD",
    "TSLA",
    "META",
    "GOOGL",
    "AMZN",
    "AVGO",
]

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
LATEST_TOP10_PATH = REPORTS_DIR / "latest_top10.json"


TRUE_VALUES = {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class AISelectorRuntimeConfig:
    enabled: bool
    top_n: int
    universe: list[str]
    top10_path: Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in TRUE_VALUES


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    try:
        parsed = int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, parsed)


def _list_env(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name, "").strip()
    if not value:
        return list(default)
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def load_runtime_config() -> AISelectorRuntimeConfig:
    return AISelectorRuntimeConfig(
        enabled=_bool_env("SOXS_AI_SELECTOR_ENABLED", AI_SELECTOR_ENABLED),
        top_n=_int_env("SOXS_AI_SELECTOR_TOP_N", TOP_N),
        universe=_list_env("SOXS_AI_SELECTOR_UNIVERSE", UNIVERSE),
        top10_path=LATEST_TOP10_PATH,
    )
