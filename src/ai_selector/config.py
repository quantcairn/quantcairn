from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


AI_SELECTOR_ENABLED = False
OPENBB_ENABLED = False
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
FMP_ENABLED = bool(FMP_API_KEY)
TOP_N = 3
ANALYSIS_UNIVERSE_LIMIT = 4
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
    tradingagents_path: str
    tradingagents_python: str
    tradingagents_analysis_date: str | None
    finrobot_path: str
    finrobot_python: str
    finrobot_config_file: str
    finrobot_output_dir: str
    analysis_universe_limit: int = ANALYSIS_UNIVERSE_LIMIT
    openbb_enabled: bool = OPENBB_ENABLED
    fmp_enabled: bool = FMP_ENABLED
    fmp_api_key: str = FMP_API_KEY


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
        analysis_universe_limit=_int_env(
            "SOXS_AI_SELECTOR_ANALYSIS_UNIVERSE_LIMIT",
            ANALYSIS_UNIVERSE_LIMIT,
        ),
        universe=_list_env("SOXS_AI_SELECTOR_UNIVERSE", UNIVERSE),
        top10_path=LATEST_TOP10_PATH,
        tradingagents_path=os.environ.get("SOXS_TRADINGAGENTS_PATH", "").strip(),
        tradingagents_python=os.environ.get("SOXS_TRADINGAGENTS_PYTHON", "").strip() or "python3",
        tradingagents_analysis_date=os.environ.get("SOXS_TRADINGAGENTS_ANALYSIS_DATE", "").strip() or None,
        finrobot_path=os.environ.get("SOXS_FINROBOT_PATH", "").strip(),
        finrobot_python=os.environ.get("SOXS_FINROBOT_PYTHON", "").strip() or "python3",
        finrobot_config_file=os.environ.get("SOXS_FINROBOT_CONFIG", "").strip(),
        finrobot_output_dir=os.environ.get("SOXS_FINROBOT_OUTPUT_DIR", "").strip(),
        openbb_enabled=_bool_env("SOXS_OPENBB_ENABLED", OPENBB_ENABLED),
        fmp_enabled=_bool_env("SOXS_FMP_ENABLED", FMP_ENABLED) and bool(
            os.environ.get("FMP_API_KEY", FMP_API_KEY).strip()
        ),
        fmp_api_key=os.environ.get("FMP_API_KEY", FMP_API_KEY).strip(),
    )
