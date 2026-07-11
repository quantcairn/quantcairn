from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .benchmarking import BenchmarkValidation, benchmark_symbols_for, validate_benchmark_alignment
from .data_feed import BacktestDataError, BacktestDataFeed, infer_bar_frequency
from .models import Bar


def _canonical_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw.split(".")[0]


def _timezone_label(timestamp: datetime | None) -> str:
    if timestamp is None or timestamp.tzinfo is None:
        return "naive"
    tz = timestamp.tzinfo
    name = tz.tzname(timestamp)
    return name or str(tz)


def _expected_timedelta(frequency: str) -> timedelta | None:
    freq = str(frequency or "").strip().lower()
    mapping = {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "1d": timedelta(days=1),
    }
    return mapping.get(freq)


def _load_raw_frame(path: str | Path | None) -> pd.DataFrame | None:
    if path in (None, ""):
        return None
    frame = pd.read_csv(path)
    if frame is not None and not frame.empty:
        frame.columns = [str(col).strip().lower() for col in frame.columns]
    return frame


def _frame_symbol(frame: pd.DataFrame | None, override: str | None = None) -> str | None:
    if override:
        return _canonical_symbol(override)
    if frame is None or frame.empty:
        return None
    for column in ("symbol", "ticker"):
        if column in frame.columns and frame[column].dropna().astype(str).str.strip().any():
            return _canonical_symbol(frame[column].dropna().astype(str).iloc[0])
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _find_timestamp_column(frame: pd.DataFrame) -> str | None:
    for column in ("timestamp", "time", "datetime", "date"):
        if column in frame.columns:
            return column
    return None


def _parse_frame_to_bars(frame: pd.DataFrame | None, symbol: str | None = None) -> list[Bar]:
    if frame is None or frame.empty:
        return []
    feed = BacktestDataFeed()
    return feed.from_dataframe(frame, symbol=symbol)


def _calculate_missing_bar_ratio(bars: Sequence[Bar], expected_frequency: str) -> float:
    if len(bars) < 2:
        return 0.0
    step = _expected_timedelta(expected_frequency)
    if step is None:
        return 0.0
    expected_slots = 0
    observed_slots = len(bars)
    if step >= timedelta(days=1):
        start_date = bars[0].timestamp.date()
        end_date = bars[-1].timestamp.date()
        expected_slots = max(1, len(pd.bdate_range(start_date, end_date)))
    else:
        span = bars[-1].timestamp - bars[0].timestamp
        if span.total_seconds() < 0:
            return 1.0
        expected_slots = int(span.total_seconds() // step.total_seconds()) + 1
    if expected_slots <= 0:
        return 0.0
    missing = max(0, expected_slots - observed_slots)
    return round(missing / expected_slots, 6)


def _future_data_risk(symbol_bars: Sequence[Bar], benchmark_bars: Sequence[Bar]) -> bool:
    if not symbol_bars or not benchmark_bars:
        return False
    symbol_end = symbol_bars[-1].timestamp
    for bar in benchmark_bars:
        if bar.timestamp > symbol_end:
            return True
    return False


@dataclass(slots=True)
class DatasetValidationReport:
    symbol: str
    benchmark_symbol: str | None
    expected_frequency: str
    symbol_frequency: str
    benchmark_frequency: str
    symbol_timezone: str
    benchmark_timezone: str
    benchmark_allowed: tuple[str, ...]
    benchmark_mapping_status: str
    benchmark_mapping_reason: str
    symbol_bar_count: int
    benchmark_bar_count: int
    shared_bar_count: int
    symbol_overlap_ratio: float
    benchmark_overlap_ratio: float
    overlap_ratio: float
    missing_bar_ratio: float
    duplicate_timestamp_count: int
    invalid_ohlc_count: int
    future_data_risk: bool
    data_start: str | None
    data_end: str | None
    benchmark_start: str | None
    benchmark_end: str | None
    formal_backtest_eligible: bool
    fail_closed_reason: str | None
    messages: list[str] = field(default_factory=list)
    benchmark_validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "benchmark_symbol": self.benchmark_symbol,
            "expected_frequency": self.expected_frequency,
            "symbol_frequency": self.symbol_frequency,
            "benchmark_frequency": self.benchmark_frequency,
            "symbol_timezone": self.symbol_timezone,
            "benchmark_timezone": self.benchmark_timezone,
            "benchmark_allowed": list(self.benchmark_allowed),
            "benchmark_mapping_status": self.benchmark_mapping_status,
            "benchmark_mapping_reason": self.benchmark_mapping_reason,
            "symbol_bar_count": self.symbol_bar_count,
            "benchmark_bar_count": self.benchmark_bar_count,
            "shared_bar_count": self.shared_bar_count,
            "symbol_overlap_ratio": self.symbol_overlap_ratio,
            "benchmark_overlap_ratio": self.benchmark_overlap_ratio,
            "overlap_ratio": self.overlap_ratio,
            "missing_bar_ratio": self.missing_bar_ratio,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "invalid_ohlc_count": self.invalid_ohlc_count,
            "future_data_risk": self.future_data_risk,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "benchmark_start": self.benchmark_start,
            "benchmark_end": self.benchmark_end,
            "formal_backtest_eligible": self.formal_backtest_eligible,
            "fail_closed_reason": self.fail_closed_reason,
            "messages": list(self.messages),
            "benchmark_validation": dict(self.benchmark_validation),
        }


def validate_backtest_dataset(
    *,
    symbol_csv: str | Path,
    benchmark_csv: str | Path | None,
    symbol: str,
    benchmark: str | None,
    expected_frequency: str,
) -> DatasetValidationReport:
    symbol_raw = _load_raw_frame(symbol_csv)
    benchmark_raw = _load_raw_frame(benchmark_csv)
    symbol_key = _canonical_symbol(symbol)
    benchmark_key = _canonical_symbol(benchmark) if benchmark else _frame_symbol(benchmark_raw)
    benchmark_allowed = benchmark_symbols_for(symbol_key)

    messages: list[str] = []
    fail_closed_reason: str | None = None
    formal_backtest_eligible = False

    def _failure(reason: str, detail: str | None = None) -> DatasetValidationReport:
        return DatasetValidationReport(
            symbol=symbol_key,
            benchmark_symbol=benchmark_key,
            expected_frequency=expected_frequency,
            symbol_frequency="unknown",
            benchmark_frequency="unknown",
            symbol_timezone="unknown",
            benchmark_timezone="unknown",
            benchmark_allowed=benchmark_allowed,
            benchmark_mapping_status="INVALID" if reason != "benchmark_missing" else "MISSING",
            benchmark_mapping_reason=reason,
            symbol_bar_count=0,
            benchmark_bar_count=0,
            shared_bar_count=0,
            symbol_overlap_ratio=0.0,
            benchmark_overlap_ratio=0.0,
            overlap_ratio=0.0,
            missing_bar_ratio=1.0,
            duplicate_timestamp_count=0,
            invalid_ohlc_count=0,
            future_data_risk=False,
            data_start=None,
            data_end=None,
            benchmark_start=None,
            benchmark_end=None,
            formal_backtest_eligible=False,
            fail_closed_reason=detail or reason,
            messages=[detail or reason],
            benchmark_validation={
                "symbol": symbol_key,
                "benchmark_symbol": benchmark_key,
                "status": "INVALID_BENCHMARK" if reason != "benchmark_missing" else "MISSING_BENCHMARK",
                "reason": reason,
                "allowed_benchmarks": list(benchmark_allowed),
            },
        )

    if symbol_raw is None or symbol_raw.empty:
        return _failure("symbol_missing", "symbol_csv_empty")

    if benchmark_csv is not None and benchmark_raw is None:
        return _failure("benchmark_missing", "benchmark_csv_empty")

    try:
        symbol_bars = _parse_frame_to_bars(symbol_raw, symbol=symbol_key)
    except BacktestDataError as exc:
        return _failure("symbol_invalid", f"symbol_data_error:{exc}")

    benchmark_bars: list[Bar] = []
    if benchmark_raw is not None and not benchmark_raw.empty:
        try:
            benchmark_bars = _parse_frame_to_bars(benchmark_raw, symbol=benchmark_key)
        except BacktestDataError as exc:
            return _failure("benchmark_invalid", f"benchmark_data_error:{exc}")

    if not symbol_bars:
        return _failure("symbol_empty", "symbol_bars_empty")

    symbol_frequency = infer_bar_frequency(symbol_bars)
    benchmark_frequency = infer_bar_frequency(benchmark_bars)
    benchmark_validation: BenchmarkValidation = validate_benchmark_alignment(symbol_key, symbol_bars, benchmark_bars)

    symbol_timezone = _timezone_label(symbol_bars[0].timestamp if symbol_bars else None)
    benchmark_timezone = _timezone_label(benchmark_bars[0].timestamp if benchmark_bars else None)
    duplicate_timestamp_count = 0
    invalid_ohlc_count = 0

    if symbol_raw is not None and not symbol_raw.empty:
        timestamp_column = _find_timestamp_column(symbol_raw)
        if timestamp_column:
            timestamps = symbol_raw[timestamp_column].map(_parse_timestamp)
            duplicate_timestamp_count = int(timestamps.duplicated().sum())
        for _, row in symbol_raw.iterrows():
            try:
                open_ = float(row.get("open"))
                high = float(row.get("high"))
                low = float(row.get("low"))
                close = float(row.get("close"))
            except Exception:
                invalid_ohlc_count += 1
                continue
            if any(value <= 0 for value in (open_, high, low, close)) or high < low or high < max(open_, close) or low > min(open_, close):
                invalid_ohlc_count += 1

    if benchmark_raw is not None and not benchmark_raw.empty:
        benchmark_timestamp_column = _find_timestamp_column(benchmark_raw)
        if benchmark_timestamp_column:
            benchmark_timestamps = benchmark_raw[benchmark_timestamp_column].map(_parse_timestamp)
            duplicate_timestamp_count += int(benchmark_timestamps.duplicated().sum())

    overlap_count = benchmark_validation.shared_bars
    symbol_overlap_ratio = round(overlap_count / len(symbol_bars), 6) if symbol_bars else 0.0
    benchmark_overlap_ratio = round(overlap_count / len(benchmark_bars), 6) if benchmark_bars else 0.0
    overlap_ratio = round(overlap_count / max(len(symbol_bars), len(benchmark_bars), 1), 6)
    missing_bar_ratio = _calculate_missing_bar_ratio(symbol_bars, expected_frequency)
    future_data_risk = _future_data_risk(symbol_bars, benchmark_bars)

    if benchmark_csv is None or not benchmark_bars:
        messages.append("benchmark_missing")
    if benchmark_allowed and benchmark_validation.benchmark_symbol not in benchmark_allowed:
        messages.append("benchmark_symbol_not_permitted")
    if benchmark_frequency != "unknown" and expected_frequency and benchmark_frequency.lower() != str(expected_frequency).lower():
        messages.append("benchmark_frequency_mismatch")
    if symbol_frequency != "unknown" and expected_frequency and symbol_frequency.lower() != str(expected_frequency).lower():
        messages.append("symbol_frequency_mismatch")
    if overlap_ratio <= 0:
        messages.append("no_time_overlap")
    if future_data_risk:
        messages.append("future_data_risk")

    if duplicate_timestamp_count > 0:
        messages.append("duplicate_timestamp")
    if invalid_ohlc_count > 0:
        messages.append("invalid_ohlc")
    if missing_bar_ratio > 0.2:
        messages.append("missing_bar_ratio_high")

    formal_backtest_eligible = bool(
        benchmark_validation.status == "VALID"
        and not future_data_risk
        and overlap_ratio >= 0.5
        and symbol_frequency == benchmark_frequency
        and expected_frequency.lower() == symbol_frequency.lower()
        and duplicate_timestamp_count == 0
        and invalid_ohlc_count == 0
    )

    fail_closed_reason = None
    if benchmark_validation.status != "VALID":
        fail_closed_reason = benchmark_validation.reason
    elif expected_frequency.lower() != symbol_frequency.lower():
        fail_closed_reason = "symbol_frequency_mismatch"
    elif symbol_frequency != benchmark_frequency:
        fail_closed_reason = "benchmark_frequency_mismatch"
    elif overlap_ratio < 0.5:
        fail_closed_reason = "insufficient_overlap"
    elif future_data_risk:
        fail_closed_reason = "future_data_risk"
    elif duplicate_timestamp_count > 0:
        fail_closed_reason = "duplicate_timestamp"
    elif invalid_ohlc_count > 0:
        fail_closed_reason = "invalid_ohlc"

    return DatasetValidationReport(
        symbol=symbol_key,
        benchmark_symbol=benchmark_validation.benchmark_symbol or benchmark_key,
        expected_frequency=expected_frequency,
        symbol_frequency=symbol_frequency,
        benchmark_frequency=benchmark_frequency,
        symbol_timezone=symbol_timezone,
        benchmark_timezone=benchmark_timezone,
        benchmark_allowed=benchmark_allowed,
        benchmark_mapping_status=benchmark_validation.status,
        benchmark_mapping_reason=benchmark_validation.reason,
        symbol_bar_count=len(symbol_bars),
        benchmark_bar_count=len(benchmark_bars),
        shared_bar_count=overlap_count,
        symbol_overlap_ratio=symbol_overlap_ratio,
        benchmark_overlap_ratio=benchmark_overlap_ratio,
        overlap_ratio=overlap_ratio,
        missing_bar_ratio=round(missing_bar_ratio, 6),
        duplicate_timestamp_count=duplicate_timestamp_count,
        invalid_ohlc_count=invalid_ohlc_count,
        future_data_risk=future_data_risk,
        data_start=symbol_bars[0].timestamp.isoformat() if symbol_bars else None,
        data_end=symbol_bars[-1].timestamp.isoformat() if symbol_bars else None,
        benchmark_start=benchmark_bars[0].timestamp.isoformat() if benchmark_bars else None,
        benchmark_end=benchmark_bars[-1].timestamp.isoformat() if benchmark_bars else None,
        formal_backtest_eligible=formal_backtest_eligible,
        fail_closed_reason=fail_closed_reason,
        messages=messages,
        benchmark_validation=benchmark_validation.to_dict(),
    )
