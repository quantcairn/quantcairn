from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import os
import subprocess
from pathlib import Path
from typing import Any

from src.config.runtime_values import has_longbridge_runtime_credentials


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


class ShadowConfigError(RuntimeError):
    pass


def _raw_env(name: str) -> str:
    value = os.getenv(name)
    if value is not None and str(value).strip():
        return str(value).strip()
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


def _parse_required_true(name: str) -> tuple[bool | None, str | None]:
    raw = _raw_env(name)
    if not raw:
        return None, f"{name} missing"
    if raw.lower() not in TRUE_VALUES:
        return False, f"{name} must be true"
    return True, None


def _parse_required_false(name: str) -> tuple[bool | None, str | None]:
    raw = _raw_env(name)
    if not raw:
        return None, f"{name} missing"
    if raw.lower() not in FALSE_VALUES:
        return True, f"{name} must be false"
    return False, None


def _parse_int(name: str, default: int) -> int:
    raw = _raw_env(name)
    if not raw:
        return int(default)
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return int(default)


def _parse_float(name: str, default: float) -> float:
    raw = _raw_env(name)
    if not raw:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _parse_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _raw_env(name)
    if not raw:
        return default
    values = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return tuple(values) if values else default


@dataclass(frozen=True)
class ShadowSafetyConfig:
    shadow_mode: bool | None
    trading_enabled: bool | None
    order_submission_enabled: bool | None
    broker_write_enabled: bool | None
    longbridge_credentials_present: bool

    @classmethod
    def from_env(cls) -> "ShadowSafetyConfig":
        shadow_mode, _ = _parse_required_true("SHADOW_MODE")
        trading_enabled, _ = _parse_required_false("TRADING_ENABLED")
        order_submission_enabled, _ = _parse_required_false("ORDER_SUBMISSION_ENABLED")
        broker_write_enabled, _ = _parse_required_false("BROKER_WRITE_ENABLED")
        return cls(
            shadow_mode=shadow_mode,
            trading_enabled=trading_enabled,
            order_submission_enabled=order_submission_enabled,
            broker_write_enabled=broker_write_enabled,
            longbridge_credentials_present=has_longbridge_runtime_credentials(),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.shadow_mode is not True:
            errors.append("SHADOW_MODE must be true")
        for name, value in (
            ("TRADING_ENABLED", self.trading_enabled),
            ("ORDER_SUBMISSION_ENABLED", self.order_submission_enabled),
            ("BROKER_WRITE_ENABLED", self.broker_write_enabled),
        ):
            if value is not False:
                errors.append(f"{name} must be false")
        if not self.longbridge_credentials_present:
            errors.append("longbridge_credentials_missing")
        return errors

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "shadow_mode": self.shadow_mode,
            "trading_enabled": self.trading_enabled,
            "order_submission_enabled": self.order_submission_enabled,
            "broker_write_enabled": self.broker_write_enabled,
            "longbridge_credentials_present": self.longbridge_credentials_present,
        }


@dataclass(frozen=True)
class ShadowRuntimeConfig:
    output_dir: Path = Path("artifacts/shadow/soxs_15m")
    symbol: str = "SOXS.US"
    benchmark_symbols: tuple[str, ...] = ("SOXX.US", "SMH.US")
    frequency: str = "15m"
    lookback_days: int = 120
    initial_cash: float = 10_000.0
    page_size: int = 1000
    max_retries: int = 3
    request_interval_seconds: float = 0.25
    poll_interval_seconds: float = 900.0
    run_once: bool = True

    @classmethod
    def from_env(cls) -> "ShadowRuntimeConfig":
        return cls(
            output_dir=Path(_raw_env("SOXS_SHADOW_OUTPUT_DIR") or "artifacts/shadow/soxs_15m"),
            symbol=_raw_env("SOXS_SHADOW_SYMBOL") or "SOXS.US",
            benchmark_symbols=_parse_csv("SOXS_SHADOW_BENCHMARKS", ("SOXX.US", "SMH.US")),
            frequency=_raw_env("SOXS_SHADOW_FREQUENCY") or "15m",
            lookback_days=_parse_int("SOXS_SHADOW_LOOKBACK_DAYS", 120),
            initial_cash=_parse_float("SOXS_SHADOW_INITIAL_CASH", 10_000.0),
            page_size=_parse_int("SOXS_SHADOW_PAGE_SIZE", 1000),
            max_retries=_parse_int("SOXS_SHADOW_MAX_RETRIES", 3),
            request_interval_seconds=_parse_float("SOXS_SHADOW_REQUEST_INTERVAL_SECONDS", 0.25),
            poll_interval_seconds=_parse_float("SOXS_SHADOW_POLL_INTERVAL_SECONDS", 900.0),
            run_once=True,
        )

    def start_date(self) -> date:
        return date.today() - timedelta(days=max(1, self.lookback_days))
