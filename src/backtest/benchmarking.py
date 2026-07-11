from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .data_feed import infer_bar_frequency
from .models import Bar


def _canonical_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw.split(".")[0]


def _extract_symbol(record: Any) -> str:
    if hasattr(record, "symbol"):
        return _canonical_symbol(getattr(record, "symbol", ""))
    if isinstance(record, dict):
        return _canonical_symbol(record.get("symbol"))
    return _canonical_symbol(record)


DEFAULT_BENCHMARK_MAP: dict[str, tuple[str, ...]] = {
    "SOXS": ("SOXX", "SMH"),
    "SOXL": ("SOXX", "SMH"),
    "SOXX": ("SMH",),
    "SMH": ("SOXX",),
    "AAPL": ("QQQ", "SPY"),
    "MSFT": ("QQQ", "SPY"),
    "NVDA": ("QQQ", "SOXX"),
    "PLTR": ("QQQ", "SPY"),
}


@dataclass(slots=True)
class BenchmarkValidation:
    symbol: str
    benchmark_symbol: str | None
    status: str
    reason: str
    allowed_benchmarks: tuple[str, ...] = ()
    symbol_frequency: str = "unknown"
    benchmark_frequency: str = "unknown"
    shared_bars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "benchmark_symbol": self.benchmark_symbol,
            "status": self.status,
            "reason": self.reason,
            "allowed_benchmarks": list(self.allowed_benchmarks),
            "symbol_frequency": self.symbol_frequency,
            "benchmark_frequency": self.benchmark_frequency,
            "shared_bars": self.shared_bars,
        }


def benchmark_symbols_for(symbol: str) -> tuple[str, ...]:
    return DEFAULT_BENCHMARK_MAP.get(_canonical_symbol(symbol), ())


def validate_benchmark_alignment(
    symbol: str,
    symbol_bars: Sequence[Bar] | Iterable[Bar] | None,
    benchmark_bars: Sequence[Bar] | Iterable[Bar] | None,
) -> BenchmarkValidation:
    symbol_key = _canonical_symbol(symbol)
    allowed = benchmark_symbols_for(symbol_key)
    symbol_frequency = infer_bar_frequency(symbol_bars)
    benchmark_list = list(benchmark_bars or [])
    benchmark_symbol = _extract_symbol(benchmark_list[0]) if benchmark_list else ""
    benchmark_frequency = infer_bar_frequency(benchmark_list)
    if not benchmark_list:
        return BenchmarkValidation(
            symbol=symbol_key,
            benchmark_symbol=None,
            status="MISSING_BENCHMARK",
            reason="benchmark_missing",
            allowed_benchmarks=allowed,
            symbol_frequency=symbol_frequency,
            benchmark_frequency="unknown",
            shared_bars=0,
        )
    if allowed and benchmark_symbol not in allowed:
        return BenchmarkValidation(
            symbol=symbol_key,
            benchmark_symbol=benchmark_symbol,
            status="INVALID_BENCHMARK",
            reason="benchmark_symbol_not_permitted",
            allowed_benchmarks=allowed,
            symbol_frequency=symbol_frequency,
            benchmark_frequency=benchmark_frequency,
            shared_bars=0,
        )
    if symbol_frequency != "unknown" and benchmark_frequency != "unknown" and symbol_frequency != benchmark_frequency:
        return BenchmarkValidation(
            symbol=symbol_key,
            benchmark_symbol=benchmark_symbol,
            status="INVALID_BENCHMARK",
            reason="benchmark_frequency_mismatch",
            allowed_benchmarks=allowed,
            symbol_frequency=symbol_frequency,
            benchmark_frequency=benchmark_frequency,
            shared_bars=0,
        )
    symbol_timestamps = {
        getattr(bar, "timestamp", None) if not isinstance(bar, dict) else bar.get("timestamp") or bar.get("time") or bar.get("datetime") or bar.get("date")
        for bar in (symbol_bars or [])
    }
    benchmark_timestamps = {
        getattr(bar, "timestamp", None) if not isinstance(bar, dict) else bar.get("timestamp") or bar.get("time") or bar.get("datetime") or bar.get("date")
        for bar in benchmark_list
    }
    symbol_timestamps = {ts for ts in symbol_timestamps if ts is not None}
    benchmark_timestamps = {ts for ts in benchmark_timestamps if ts is not None}
    shared = len(symbol_timestamps & benchmark_timestamps)
    if shared <= 0:
        return BenchmarkValidation(
            symbol=symbol_key,
            benchmark_symbol=benchmark_symbol,
            status="INVALID_BENCHMARK",
            reason="benchmark_no_time_overlap",
            allowed_benchmarks=allowed,
            symbol_frequency=symbol_frequency,
            benchmark_frequency=benchmark_frequency,
            shared_bars=0,
        )
    return BenchmarkValidation(
        symbol=symbol_key,
        benchmark_symbol=benchmark_symbol,
        status="VALID",
        reason="benchmark_valid",
        allowed_benchmarks=allowed,
        symbol_frequency=symbol_frequency,
        benchmark_frequency=benchmark_frequency,
        shared_bars=shared,
    )
