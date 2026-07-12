from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

import pandas as pd

from .models import Bar


class BacktestDataError(ValueError):
    pass


def _coerce_timestamp(value: Any, assume_timezone: str = "UTC") -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    elif is_dataclass(value):
        payload = asdict(value)
        ts = _coerce_timestamp(payload.get("timestamp") or payload.get("time"), assume_timezone=assume_timezone)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                ts = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except Exception:
                return None
    if ts.tzinfo is None:
        if assume_timezone.upper() == "UTC":
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            try:
                from zoneinfo import ZoneInfo

                ts = ts.replace(tzinfo=ZoneInfo(assume_timezone))
            except Exception:
                ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BacktestDataError(f"Invalid {field_name}: {value!r}") from exc
    if result <= 0:
        raise BacktestDataError(f"Invalid {field_name}: {value!r}")
    return result


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError) as exc:
        raise BacktestDataError(f"Invalid {field_name}: {value!r}") from exc
    if result < 0:
        raise BacktestDataError(f"Invalid {field_name}: {value!r}")
    return result


class BacktestDataFeed:
    def __init__(self, assume_timezone: str = "UTC") -> None:
        self.assume_timezone = assume_timezone

    def load(self, source: Any, symbol: str | None = None) -> list[Bar]:
        if source is None:
            return []
        if isinstance(source, (str, Path)):
            return self.load_csv(source, symbol=symbol)
        if isinstance(source, pd.DataFrame):
            return self.from_dataframe(source, symbol=symbol)
        if isinstance(source, dict):
            return self.from_sequence([source], symbol=symbol)
        if isinstance(source, Sequence) or isinstance(source, Iterable):
            return self.from_sequence(source, symbol=symbol)
        raise BacktestDataError(f"Unsupported data source type: {type(source).__name__}")

    def load_csv(self, path: str | Path, symbol: str | None = None) -> list[Bar]:
        frame = pd.read_csv(path)
        if frame is not None and not frame.empty:
            frame.columns = [str(col).strip().lower() for col in frame.columns]
        return self.from_dataframe(frame, symbol=symbol)

    def from_dataframe(self, frame: pd.DataFrame, symbol: str | None = None) -> list[Bar]:
        if frame is None or frame.empty:
            return []
        frequency_columns = [column for column in ("frequency", "bar_frequency", "interval") if column in frame.columns]
        if frequency_columns:
            column = frequency_columns[0]
            values = {
                str(value).strip().lower()
                for value in frame[column].dropna().astype(str).tolist()
                if str(value).strip()
            }
            if len(values) > 1:
                raise BacktestDataError("Daily and intraday bars cannot be mixed")
        records = frame.to_dict(orient="records")
        return self.from_sequence(records, symbol=symbol)

    def from_sequence(self, rows: Sequence[Any] | Iterable[Any], symbol: str | None = None) -> list[Bar]:
        bars: list[Bar] = []
        seen_timestamps: set[datetime] = set()
        previous_timestamp: datetime | None = None
        for index, raw in enumerate(list(rows or [])):
            bar = self._coerce_bar(raw, symbol=symbol)
            if bar.timestamp in seen_timestamps:
                raise BacktestDataError(f"Duplicate timestamp: {bar.timestamp.isoformat()}")
            if previous_timestamp is not None and bar.timestamp <= previous_timestamp:
                raise BacktestDataError("Timestamps must be strictly increasing")
            seen_timestamps.add(bar.timestamp)
            previous_timestamp = bar.timestamp
            bars.append(bar)
        self._validate_frequency_consistency(bars)
        return bars

    def _coerce_bar(self, raw: Any, symbol: str | None = None) -> Bar:
        if is_dataclass(raw):
            payload = asdict(raw)
        elif isinstance(raw, dict):
            payload = dict(raw)
        else:
            payload = {}
            for name in ("symbol", "timestamp", "time", "open", "high", "low", "close", "volume", "bid", "ask", "source"):
                if hasattr(raw, name):
                    payload[name] = getattr(raw, name)

        ts = _coerce_timestamp(
            payload.get("timestamp")
            or payload.get("time")
            or payload.get("datetime")
            or payload.get("date"),
            assume_timezone=self.assume_timezone,
        )
        if ts is None:
            raise BacktestDataError("Missing timestamp")

        sym = str(payload.get("symbol") or symbol or "").strip().upper()
        if not sym:
            raise BacktestDataError("Missing symbol")

        open_ = _coerce_float(payload.get("open"), "open")
        high = _coerce_float(payload.get("high"), "high")
        low = _coerce_float(payload.get("low"), "low")
        close = _coerce_float(payload.get("close"), "close")
        volume = _coerce_int(payload.get("volume"), "volume")

        if high < low:
            raise BacktestDataError("high must be >= low")
        if high < max(open_, close):
            raise BacktestDataError("high must be >= open/close")
        if low > min(open_, close):
            raise BacktestDataError("low must be <= open/close")

        bid = payload.get("bid")
        ask = payload.get("ask")
        bid_value = float(bid) if bid not in (None, "") else None
        ask_value = float(ask) if ask not in (None, "") else None

        return Bar(
            symbol=sym,
            timestamp=ts,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            bid=bid_value,
            ask=ask_value,
            source=str(payload.get("source") or "unknown"),
        )

    def _validate_frequency_consistency(self, bars: list[Bar]) -> None:
        if len(bars) < 2:
            return
        counts_by_date: dict[datetime.date, int] = {}
        for bar in bars:
            counts_by_date[bar.timestamp.date()] = counts_by_date.get(bar.timestamp.date(), 0) + 1
        if not counts_by_date:
            return
        has_single_bar_day = any(count == 1 for count in counts_by_date.values())
        has_multi_bar_day = any(count > 1 for count in counts_by_date.values())
        if has_single_bar_day and has_multi_bar_day:
            raise BacktestDataError("Daily and intraday bars cannot be mixed")

    def infer_frequency(self, bars: Sequence[Bar] | Iterable[Bar] | None) -> str:
        return infer_bar_frequency(bars)


def infer_bar_frequency(bars: Sequence[Bar] | Iterable[Bar] | None) -> str:
    bar_list = list(bars or [])
    if len(bar_list) < 2:
        return "unknown"
    deltas = []
    for previous, current in zip(bar_list, bar_list[1:]):
        previous_ts = getattr(previous, "timestamp", None)
        if previous_ts is None and isinstance(previous, dict):
            previous_ts = previous.get("timestamp") or previous.get("time") or previous.get("datetime") or previous.get("date")
        current_ts = getattr(current, "timestamp", None)
        if current_ts is None and isinstance(current, dict):
            current_ts = current.get("timestamp") or current.get("time") or current.get("datetime") or current.get("date")
        if previous_ts is None or current_ts is None:
            continue
        delta = current_ts - previous_ts
        seconds = delta.total_seconds()
        if seconds > 0:
            deltas.append(seconds)
    if not deltas:
        return "unknown"
    seconds = float(median(deltas))
    if seconds >= 23 * 3600:
        return "daily"
    mapping = [
        (300.0, "5m"),
        (900.0, "15m"),
        (1800.0, "30m"),
        (3600.0, "1h"),
    ]
    for target, label in mapping:
        if abs(seconds - target) <= max(30.0, target * 0.2):
            return label
    if seconds < 86400.0:
        minutes = max(1, int(round(seconds / 60.0)))
        return f"{minutes}m"
    return "daily"
