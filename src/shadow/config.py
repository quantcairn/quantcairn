from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import os
import subprocess
from pathlib import Path
from typing import Any

from src.config.runtime_values import has_longbridge_runtime_credentials

from .universe import (
    ShadowUniverseConfig,
    canonical_shadow_symbol,
    default_benchmarks_for,
    default_shadow_output_directory,
    is_safe_shadow_output_directory,
    shadow_title_for,
    strategy_family_for,
    symbol_class_for,
)


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


def _parse_optional_bool(name: str, default: bool) -> bool:
    raw = _raw_env(name)
    if not raw:
        return bool(default)
    lowered = raw.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    return bool(default)


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
    timeframe: str = "15m"
    strategy_version: str = "baseline"
    strategy_family: str = "range_etf"
    risk_profile: str = "strict"
    regular_session_only: bool = True
    shadow_enabled: bool = True
    trading_enabled: bool = False
    symbol_class: str = "inverse_etf"
    lookback_days: int = 120
    initial_cash: float = 10_000.0
    page_size: int = 1000
    max_retries: int = 3
    request_interval_seconds: float = 0.25
    poll_interval_seconds: float = 900.0
    run_once: bool = True

    @classmethod
    def from_env(cls) -> "ShadowRuntimeConfig":
        symbol = canonical_shadow_symbol(_raw_env("SOXS_SHADOW_SYMBOL") or "SOXS.US")
        timeframe = (_raw_env("SOXS_SHADOW_TIMEFRAME") or _raw_env("SOXS_SHADOW_FREQUENCY") or "15m").strip().lower() or "15m"
        explicit_benchmarks = _parse_csv("SOXS_SHADOW_BENCHMARKS", ())
        benchmarks = explicit_benchmarks if explicit_benchmarks else default_benchmarks_for(symbol)
        output_dir_value = _raw_env("SOXS_SHADOW_OUTPUT_DIR")
        output_dir = Path(output_dir_value) if output_dir_value else default_shadow_output_directory(symbol, timeframe)
        strategy_version = _raw_env("SOXS_SHADOW_STRATEGY_VERSION") or "baseline"
        strategy_family = _raw_env("SOXS_SHADOW_STRATEGY_FAMILY") or strategy_family_for(symbol)
        risk_profile = _raw_env("SOXS_SHADOW_RISK_PROFILE") or ""
        symbol_class = _raw_env("SOXS_SHADOW_SYMBOL_CLASS") or symbol_class_for(symbol)
        return cls(
            output_dir=output_dir,
            symbol=symbol,
            benchmark_symbols=benchmarks,
            frequency=timeframe,
            timeframe=timeframe,
            strategy_version=strategy_version,
            strategy_family=strategy_family,
            risk_profile=risk_profile or "strict",
            regular_session_only=_parse_optional_bool("SOXS_SHADOW_REGULAR_SESSION_ONLY", True),
            shadow_enabled=_parse_optional_bool("SOXS_SHADOW_ENABLED", True),
            trading_enabled=_parse_optional_bool("SOXS_TRADING_ENABLED", False),
            symbol_class=symbol_class,
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

    @property
    def shadow_title(self) -> str:
        return shadow_title_for(self.symbol, self.timeframe)

    def resolve_universe(self) -> ShadowUniverseConfig:
        benchmarks = self.benchmark_symbols
        catalog_defaults = default_benchmarks_for(self.symbol)
        if not benchmarks:
            benchmarks = catalog_defaults
        elif self.symbol != "SOXS.US" and benchmarks == ("SOXX.US", "SMH.US") and catalog_defaults:
            benchmarks = catalog_defaults
        resolved_output_dir = self.output_dir
        if self.symbol != "SOXS.US" and Path(self.output_dir) == Path("artifacts/shadow/soxs_15m"):
            resolved_output_dir = default_shadow_output_directory(self.symbol, self.timeframe)
        return ShadowUniverseConfig.for_symbol(
            self.symbol,
            benchmarks=benchmarks,
            timeframe=self.timeframe,
            strategy_version=self.strategy_version,
            strategy_family=self.strategy_family or None,
            risk_profile=self.risk_profile or None,
            regular_session_only=self.regular_session_only,
            output_directory=resolved_output_dir,
            shadow_enabled=self.shadow_enabled,
            trading_enabled=self.trading_enabled,
            symbol_class=self.symbol_class or None,
        )
