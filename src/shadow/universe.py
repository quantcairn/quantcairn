from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any


VALID_SYMBOL_CLASSES = {"common_stock", "leveraged_etf", "inverse_etf", "index_etf"}
VALID_RISK_PROFILES = {"balanced", "strict", "very_strict"}
PROJECT_DIR = Path(__file__).resolve().parents[2]
SHADOW_ROOT_DIR = PROJECT_DIR / "artifacts" / "shadow"
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9._-]*\.US$")
_TIMEFRAME_PATTERN = re.compile(r"^(?:\d+[mh]|daily)$")
_SAFE_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


SHADOW_SYMBOL_CATALOG: dict[str, dict[str, Any]] = {
    "SOXS.US": {
        "symbol_class": "inverse_etf",
        "strategy_family": "range_etf",
        "default_benchmarks": ("SOXX.US", "SMH.US"),
        "compatible_strategy_families": ("range_etf", "inverse_range"),
        "risk_profile": "strict",
    },
    "SOXX.US": {
        "symbol_class": "index_etf",
        "strategy_family": "benchmark",
        "default_benchmarks": ("SOXS.US",),
        "compatible_strategy_families": ("benchmark", "index_follow"),
        "risk_profile": "balanced",
    },
    "SMH.US": {
        "symbol_class": "index_etf",
        "strategy_family": "benchmark",
        "default_benchmarks": ("SOXS.US",),
        "compatible_strategy_families": ("benchmark", "index_follow"),
        "risk_profile": "balanced",
    },
    "AAPL.US": {
        "symbol_class": "common_stock",
        "strategy_family": "equity_mean_reversion",
        "default_benchmarks": ("QQQ.US", "SPY.US"),
        "compatible_strategy_families": ("equity_mean_reversion", "mean_reversion"),
        "risk_profile": "balanced",
    },
    "QQQ.US": {
        "symbol_class": "index_etf",
        "strategy_family": "benchmark",
        "default_benchmarks": ("AAPL.US",),
        "compatible_strategy_families": ("benchmark", "index_follow"),
        "risk_profile": "balanced",
    },
    "SPY.US": {
        "symbol_class": "index_etf",
        "strategy_family": "benchmark",
        "default_benchmarks": ("AAPL.US",),
        "compatible_strategy_families": ("benchmark", "index_follow"),
        "risk_profile": "balanced",
    },
    "SOXL.US": {
        "symbol_class": "leveraged_etf",
        "strategy_family": "leveraged_range",
        "default_benchmarks": ("SOXX.US", "SMH.US"),
        "compatible_strategy_families": ("leveraged_range", "range_etf"),
        "risk_profile": "strict",
    },
    "LABD.US": {
        "symbol_class": "inverse_etf",
        "strategy_family": "inverse_range",
        "default_benchmarks": ("XLV.US", "IBB.US"),
        "compatible_strategy_families": ("inverse_range", "range_etf"),
        "risk_profile": "strict",
    },
    "DRIP.US": {
        "symbol_class": "inverse_etf",
        "strategy_family": "inverse_range",
        "default_benchmarks": ("XLE.US", "USO.US"),
        "compatible_strategy_families": ("inverse_range", "range_etf"),
        "risk_profile": "strict",
    },
    "YINN.US": {
        "symbol_class": "leveraged_etf",
        "strategy_family": "leveraged_range",
        "default_benchmarks": ("FXI.US", "MCHI.US"),
        "compatible_strategy_families": ("leveraged_range", "range_etf"),
        "risk_profile": "strict",
    },
}

_FALLBACK_BENCHMARKS_BY_CLASS: dict[str, tuple[str, ...]] = {
    "common_stock": ("QQQ.US", "SPY.US"),
    "index_etf": ("SPY.US", "QQQ.US"),
    "leveraged_etf": ("QQQ.US", "SPY.US"),
    "inverse_etf": ("QQQ.US", "SPY.US"),
}


def _safe_slug(value: str, *, fallback: str = "shadow") -> str:
    text = str(value or "").strip().lower().replace(".", "_")
    text = _SAFE_SLUG_PATTERN.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _is_safe_shadow_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_PATTERN.fullmatch(canonical_shadow_symbol(symbol)))


def _is_safe_timeframe(timeframe: str) -> bool:
    return bool(_TIMEFRAME_PATTERN.fullmatch(str(timeframe or "").strip().lower()))


def is_safe_shadow_output_directory(path: Path | str) -> bool:
    candidate = Path(path)
    resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (PROJECT_DIR / candidate).resolve(strict=False)
    try:
        resolved.relative_to(SHADOW_ROOT_DIR.resolve())
    except ValueError:
        return False
    return True


def canonical_shadow_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        return ""
    if "." not in value:
        value = f"{value}.US"
    return value


def _catalog_entry(symbol: str) -> dict[str, Any]:
    return dict(SHADOW_SYMBOL_CATALOG.get(canonical_shadow_symbol(symbol), {}))


def symbol_class_for(symbol: str) -> str:
    entry = _catalog_entry(symbol)
    return str(entry.get("symbol_class") or "common_stock")


def strategy_family_for(symbol: str) -> str:
    entry = _catalog_entry(symbol)
    return str(entry.get("strategy_family") or "")


def default_benchmarks_for(symbol: str) -> tuple[str, ...]:
    entry = _catalog_entry(symbol)
    values = entry.get("default_benchmarks")
    if isinstance(values, tuple):
        return tuple(canonical_shadow_symbol(item) for item in values)
    if isinstance(values, list):
        return tuple(canonical_shadow_symbol(item) for item in values)
    fallback = _FALLBACK_BENCHMARKS_BY_CLASS.get(symbol_class_for(symbol))
    if fallback:
        return tuple(canonical_shadow_symbol(item) for item in fallback)
    return ()


def default_shadow_output_directory(symbol: str, timeframe: str) -> Path:
    normalized_symbol = canonical_shadow_symbol(symbol)
    normalized_timeframe = str(timeframe or "15m").strip().lower() or "15m"
    if normalized_symbol == "SOXS.US" and normalized_timeframe == "15m":
        return Path("artifacts/shadow/soxs_15m")
    safe_symbol = _safe_slug(normalized_symbol.split(".")[0], fallback="shadow")
    safe_timeframe = _safe_slug(normalized_timeframe, fallback="15m")
    return Path(f"artifacts/shadow/{safe_symbol}_{safe_timeframe}")


def shadow_title_for(symbol: str, timeframe: str | None = None) -> str:
    base_symbol = canonical_shadow_symbol(symbol).split(".")[0] or "SHADOW"
    return f"{base_symbol} Shadow Observer"


@dataclass(frozen=True, slots=True)
class ShadowUniverseConfig:
    symbol: str
    benchmarks: tuple[str, ...]
    timeframe: str = "15m"
    strategy_version: str = "baseline"
    strategy_family: str = "range_etf"
    risk_profile: str = "strict"
    regular_session_only: bool = True
    output_directory: Path = Path("artifacts/shadow/soxs_15m")
    shadow_enabled: bool = True
    trading_enabled: bool = False
    symbol_class: str = "inverse_etf"

    @classmethod
    def for_symbol(
        cls,
        symbol: str,
        *,
        benchmarks: tuple[str, ...] | None = None,
        timeframe: str = "15m",
        strategy_version: str = "baseline",
        strategy_family: str | None = None,
        risk_profile: str | None = None,
        regular_session_only: bool = True,
        output_directory: Path | None = None,
        shadow_enabled: bool = True,
        trading_enabled: bool = False,
        symbol_class: str | None = None,
    ) -> "ShadowUniverseConfig":
        canonical_symbol = canonical_shadow_symbol(symbol)
        entry = _catalog_entry(canonical_symbol)
        resolved_benchmarks = tuple(
            canonical_shadow_symbol(item)
            for item in (
                benchmarks
                if benchmarks is not None
                else entry.get("default_benchmarks")
                or default_benchmarks_for(canonical_symbol)
                or ()
            )
        )
        resolved_family = str(strategy_family or entry.get("strategy_family") or "").strip()
        resolved_risk_profile = str(risk_profile or entry.get("risk_profile") or "").strip().lower()
        resolved_class = str(symbol_class or entry.get("symbol_class") or "common_stock").strip().lower()
        resolved_timeframe = str(timeframe or "15m").strip().lower() or "15m"
        resolved_output_directory = Path(output_directory) if output_directory else default_shadow_output_directory(canonical_symbol, resolved_timeframe)
        return cls(
            symbol=canonical_symbol,
            benchmarks=resolved_benchmarks,
            timeframe=resolved_timeframe,
            strategy_version=str(strategy_version or "baseline").strip(),
            strategy_family=resolved_family,
            risk_profile=resolved_risk_profile or "balanced",
            regular_session_only=bool(regular_session_only),
            output_directory=resolved_output_directory,
            shadow_enabled=bool(shadow_enabled),
            trading_enabled=bool(trading_enabled),
            symbol_class=resolved_class or "common_stock",
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.shadow_enabled:
            errors.append("shadow_enabled must be true")
        if self.trading_enabled:
            errors.append("trading_enabled must be false")
        if not self.symbol:
            errors.append("symbol missing")
        if not _is_safe_shadow_symbol(self.symbol):
            errors.append("invalid_symbol")
        if not self.timeframe:
            errors.append("timeframe missing")
        if not _is_safe_timeframe(self.timeframe):
            errors.append("invalid_timeframe")
        if not self.benchmarks:
            errors.append("benchmarks missing")
        if not self.strategy_family:
            errors.append("strategy_family missing")
        if self.symbol_class not in VALID_SYMBOL_CLASSES:
            errors.append("invalid_symbol_class")
        if self.symbol_class in {"leveraged_etf", "inverse_etf"} and self.risk_profile not in VALID_RISK_PROFILES:
            errors.append("missing_risk_profile")
        if self.symbol_class in {"leveraged_etf", "inverse_etf"} and self.risk_profile not in {"strict", "very_strict"}:
            errors.append("insufficient_risk_profile")
        if not self.regular_session_only:
            errors.append("regular_session_only must be true")
        if not is_safe_shadow_output_directory(self.output_directory):
            errors.append("unsafe_output_directory")

        compatible = tuple(
            str(item).strip().lower()
            for item in (_catalog_entry(self.symbol).get("compatible_strategy_families") or ())
            if str(item).strip()
        )
        if compatible and self.strategy_family.lower() not in compatible:
            errors.append(f"incompatible_strategy_family:{self.strategy_family}")
        return errors

    @property
    def display_name(self) -> str:
        return shadow_title_for(self.symbol, self.timeframe)

    @property
    def output_dir(self) -> Path:
        return self.output_directory

    @property
    def benchmark_symbols(self) -> tuple[str, ...]:
        return self.benchmarks

    @property
    def frequency(self) -> str:
        return self.timeframe

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "benchmarks": list(self.benchmarks),
            "benchmark_symbols": list(self.benchmarks),
            "timeframe": self.timeframe,
            "frequency": self.timeframe,
            "strategy_version": self.strategy_version,
            "strategy_family": self.strategy_family,
            "risk_profile": self.risk_profile,
            "regular_session_only": self.regular_session_only,
            "output_directory": str(self.output_directory),
            "shadow_enabled": self.shadow_enabled,
            "trading_enabled": self.trading_enabled,
            "symbol_class": self.symbol_class,
            "title": self.display_name,
        }
