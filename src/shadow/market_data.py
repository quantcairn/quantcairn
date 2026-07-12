from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.backtest.data_feed import BacktestDataFeed, BacktestDataError
from src.backtest.models import Bar
from src.backtest.benchmarking import validate_benchmark_alignment
from src.data.longbridge_history import (
    LongbridgeHistoryDownloader,
    LongbridgeHistoryError,
    _canonical_symbol,
    _frequency_for_api,
    _frequency_label,
    _frequency_step,
    _session_bucket,
)


UTC = timezone.utc


class ShadowMarketDataError(RuntimeError):
    pass


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raw = str(value or "").strip()
    if not raw:
        raise ShadowMarketDataError("invalid_datetime")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception as exc:
        raise ShadowMarketDataError(f"invalid_datetime:{value!r}") from exc
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class ShadowMarketBundle:
    symbol: str
    benchmark_symbols: tuple[str, ...]
    frequency: str
    start_timestamp: datetime
    end_timestamp: datetime
    symbol_bars: list[Bar] = field(default_factory=list)
    benchmark_bars: dict[str, list[Bar]] = field(default_factory=dict)
    benchmark_validation: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = "longbridge_quote_api"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "benchmark_symbols": list(self.benchmark_symbols),
            "frequency": self.frequency,
            "start_timestamp": self.start_timestamp.isoformat(),
            "end_timestamp": self.end_timestamp.isoformat(),
            "symbol_bars": len(self.symbol_bars),
            "benchmark_bars": {key: len(value) for key, value in self.benchmark_bars.items()},
            "benchmark_validation": self.benchmark_validation,
            "source": self.source,
        }


class ShadowMarketDataSource:
    def __init__(
        self,
        *,
        page_size: int = 1000,
        max_retries: int = 3,
        request_interval_seconds: float = 0.25,
        regular_session_only: bool = True,
        adjustment: str = "auto",
    ) -> None:
        self.page_size = int(page_size)
        self.max_retries = int(max_retries)
        self.request_interval_seconds = float(request_interval_seconds)
        self.regular_session_only = bool(regular_session_only)
        self.adjustment = str(adjustment or "auto").strip().lower()
        self._downloader = LongbridgeHistoryDownloader(
            output_dir=Path(tempfile.mkdtemp(prefix="shadow_quote_history_")),
            page_size=self.page_size,
            max_retries=self.max_retries,
            request_interval_seconds=self.request_interval_seconds,
            regular_session_only=self.regular_session_only,
            adjustment=self.adjustment,
        )
        self._feed = BacktestDataFeed()

    @property
    def quote_only(self) -> bool:
        return True

    def _fetch_rows(
        self,
        *,
        symbol: str,
        frequency: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict[str, Any]]:
        ctx = self._downloader._build_context()
        _name, method = self._downloader._history_method(ctx)
        forward_mode = self._downloader._history_uses_forward_time(method)
        symbol = _canonical_symbol(symbol)
        frequency = _frequency_label(frequency)
        step = _frequency_step(frequency)
        rows: list[dict[str, Any]] = []
        anchor_time = end_dt
        offset = 0
        attempts = 0
        while True:
            attempts += 1
            page = self._downloader._page_to_rows(
                ctx=ctx,
                symbol=symbol,
                frequency=_frequency_for_api(frequency),
                offset=offset,
                end_time=anchor_time,
                forward=False if forward_mode else None,
            )
            if not page:
                break
            rows.extend(page)
            page_timestamps = [row["timestamp"] for row in page if isinstance(row.get("timestamp"), datetime)]
            if page_timestamps:
                earliest = min(page_timestamps)
                if earliest <= start_dt:
                    break
                if forward_mode:
                    anchor_time = earliest - step
                else:
                    offset += len(page)
            if len(page) < self.page_size:
                break
            if attempts > max(100, self.max_retries * 1000):
                raise ShadowMarketDataError("pagination_runaway")
        filtered: list[dict[str, Any]] = []
        for row in rows:
            ts = row.get("timestamp")
            if not isinstance(ts, datetime):
                continue
            ts_utc = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
            if ts_utc < start_dt or ts_utc > end_dt:
                continue
            if self.regular_session_only and frequency != "daily" and _session_bucket(ts_utc) != "regular":
                continue
            row = dict(row)
            row["timestamp"] = ts_utc
            filtered.append(row)

        deduped: dict[datetime, dict[str, Any]] = {}
        for row in filtered:
            ts = row["timestamp"]
            deduped[ts] = row
        ordered = [deduped[key] for key in sorted(deduped)]
        return ordered

    def fetch_bars(
        self,
        *,
        symbol: str,
        frequency: str,
        lookback_days: int,
        end_dt: datetime | None = None,
    ) -> list[Bar]:
        end_time = _coerce_datetime(end_dt or datetime.now(timezone.utc))
        start_time = end_time - timedelta(days=max(1, int(lookback_days or 1)))
        rows = self._fetch_rows(symbol=symbol, frequency=frequency, start_dt=start_time, end_dt=end_time)
        try:
            bars = self._feed.from_sequence(rows, symbol=_canonical_symbol(symbol))
        except BacktestDataError as exc:
            raise ShadowMarketDataError(str(exc)) from exc
        return bars

    def fetch_bundle(
        self,
        *,
        symbol: str,
        benchmark_symbols: tuple[str, ...],
        frequency: str,
        lookback_days: int,
        end_dt: datetime | None = None,
    ) -> ShadowMarketBundle:
        end_time = _coerce_datetime(end_dt or datetime.now(timezone.utc))
        symbol_bars = self.fetch_bars(symbol=symbol, frequency=frequency, lookback_days=lookback_days, end_dt=end_time)
        benchmark_bars: dict[str, list[Bar]] = {}
        benchmark_validation: dict[str, dict[str, Any]] = {}
        for benchmark_symbol in benchmark_symbols:
            bars = self.fetch_bars(
                symbol=benchmark_symbol,
                frequency=frequency,
                lookback_days=lookback_days,
                end_dt=end_time,
            )
            benchmark_bars[benchmark_symbol] = bars
            benchmark_validation[benchmark_symbol] = validate_benchmark_alignment(symbol, symbol_bars, bars).to_dict()
        return ShadowMarketBundle(
            symbol=_canonical_symbol(symbol),
            benchmark_symbols=tuple(_canonical_symbol(item) for item in benchmark_symbols),
            frequency=_frequency_label(frequency),
            start_timestamp=symbol_bars[0].timestamp if symbol_bars else start_time,
            end_timestamp=symbol_bars[-1].timestamp if symbol_bars else end_time,
            symbol_bars=symbol_bars,
            benchmark_bars=benchmark_bars,
            benchmark_validation=benchmark_validation,
            source="longbridge_quote_api",
        )

