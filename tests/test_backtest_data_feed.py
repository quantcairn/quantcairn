from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.data_feed import BacktestDataError, BacktestDataFeed, infer_bar_frequency
from src.backtest.comparison import compare_versions
from src.backtest.models import Bar


def _bar(ts, open_, high, low, close, volume, symbol="SOXS.US"):
    return {
        "symbol": symbol,
        "timestamp": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_load_from_sequence_and_dataframe_and_csv(tmp_path):
    feed = BacktestDataFeed()
    rows = [
        _bar(datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), 10.0, 10.2, 9.8, 10.1, 1000),
        _bar(datetime(2026, 7, 1, 9, 31, tzinfo=timezone.utc), 10.1, 10.3, 9.9, 10.2, 1100),
    ]

    bars = feed.from_sequence(rows)
    assert len(bars) == 2
    assert bars[0].symbol == "SOXS.US"
    assert bars[0].timestamp.tzinfo is not None

    frame = pd.DataFrame(rows)
    assert len(feed.from_dataframe(frame)) == 2

    csv_path = tmp_path / "bars.csv"
    frame.to_csv(csv_path, index=False)
    assert len(feed.load_csv(csv_path)) == 2


def test_rejects_duplicate_and_unsorted_timestamps():
    feed = BacktestDataFeed()
    ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    rows = [_bar(ts, 10, 11, 9, 10.5, 1000), _bar(ts, 10.5, 11, 10, 10.8, 900)]
    with pytest.raises(BacktestDataError):
        feed.from_sequence(rows)

    rows = [
        _bar(datetime(2026, 7, 1, 9, 31, tzinfo=timezone.utc), 10, 11, 9, 10.5, 1000),
        _bar(datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), 10.5, 11, 10, 10.8, 900),
    ]
    with pytest.raises(BacktestDataError):
        feed.from_sequence(rows)


def test_rejects_invalid_ohlc():
    feed = BacktestDataFeed()
    ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bad_rows = [
        _bar(ts, 0, 11, 9, 10.5, 1000),
        _bar(ts, 10, 8, 9, 10.5, 1000),
        _bar(ts, 10, 11, 12, 10.5, 1000),
    ]
    for row in bad_rows:
        with pytest.raises(BacktestDataError):
            feed.from_sequence([row])


def test_missing_timestamp_symbol_and_negative_price_fail_closed():
    feed = BacktestDataFeed()
    with pytest.raises(BacktestDataError):
        feed.from_sequence([{"symbol": "SOXS.US", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000}])
    with pytest.raises(BacktestDataError):
        feed.from_sequence([{"timestamp": datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), "symbol": "", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000}])
    with pytest.raises(BacktestDataError):
        feed.from_sequence([{"timestamp": datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), "symbol": "SOXS.US", "open": -1, "high": 11, "low": 9, "close": 10, "volume": 1000}])


def test_loads_date_and_datetime_columns_and_normalizes_csv_headers(tmp_path):
    feed = BacktestDataFeed()
    frame = pd.DataFrame(
        [
            {"Date": "2026-07-01", "OPEN": 10.0, "HIGH": 10.5, "LOW": 9.8, "CLOSE": 10.1, "VOLUME": 1000, "SYMBOL": "SOXS.US"},
            {"Date": "2026-07-02", "OPEN": 10.1, "HIGH": 10.6, "LOW": 9.9, "CLOSE": 10.2, "VOLUME": 1100, "SYMBOL": "SOXS.US"},
        ]
    )
    csv_path = tmp_path / "bars.csv"
    frame.to_csv(csv_path, index=False)
    bars = feed.load_csv(csv_path)
    assert len(bars) == 2
    assert bars[0].timestamp.isoformat().startswith("2026-07-01T")
    assert bars[0].open == 10.0
    assert bars[0].close == 10.1

    frame = pd.DataFrame(
        [
            {"datetime": "2026-07-03T09:30:00+00:00", "open": 10.3, "high": 10.6, "low": 10.1, "close": 10.4, "volume": 1200, "symbol": "SOXS.US"},
            {"datetime": "2026-07-03T09:31:00+00:00", "open": 10.4, "high": 10.7, "low": 10.2, "close": 10.5, "volume": 1300, "symbol": "SOXS.US"},
        ]
    )
    csv_path = tmp_path / "bars_datetime.csv"
    frame.to_csv(csv_path, index=False)
    bars = feed.load_csv(csv_path)
    assert len(bars) == 2
    assert bars[0].timestamp.isoformat().startswith("2026-07-03T09:30:00")


def test_rejects_mixed_daily_and_intraday_bars():
    feed = BacktestDataFeed()
    rows = [
        {"timestamp": datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), "symbol": "SOXS.US", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
        {"timestamp": datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc), "symbol": "SOXS.US", "open": 10.5, "high": 11.0, "low": 10.0, "close": 10.8, "volume": 1200},
        {"timestamp": datetime(2026, 7, 2, 9, 31, tzinfo=timezone.utc), "symbol": "SOXS.US", "open": 10.8, "high": 11.1, "low": 10.7, "close": 10.9, "volume": 900},
    ]
    with pytest.raises(BacktestDataError):
        feed.from_sequence(rows)


def test_benchmark_alignment_ignores_future_benchmark_data():
    bars = [
        Bar(symbol="SOXS.US", timestamp=datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), open=10.0, high=10.2, low=9.8, close=10.1, volume=1000),
        Bar(symbol="SOXS.US", timestamp=datetime(2026, 7, 1, 9, 31, tzinfo=timezone.utc), open=10.1, high=10.3, low=9.9, close=10.2, volume=1000),
        Bar(symbol="SOXS.US", timestamp=datetime(2026, 7, 1, 9, 32, tzinfo=timezone.utc), open=10.2, high=10.4, low=10.0, close=10.25, volume=1000),
    ]
    benchmark = [
        Bar(symbol="SMH.US", timestamp=datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), open=100, high=101, low=99, close=100.5, volume=1000),
        Bar(symbol="SMH.US", timestamp=datetime(2026, 7, 1, 9, 31, tzinfo=timezone.utc), open=100.5, high=101.5, low=100, close=100.8, volume=1000),
        Bar(symbol="SMH.US", timestamp=datetime(2026, 7, 1, 9, 32, tzinfo=timezone.utc), open=100.8, high=101.8, low=100.2, close=101.0, volume=1000),
        Bar(symbol="SMH.US", timestamp=datetime(2026, 7, 1, 9, 33, tzinfo=timezone.utc), open=200, high=201, low=199, close=200.0, volume=1000),
    ]
    left = compare_versions(bars, symbol="SOXS.US", benchmark_bars=benchmark[:-1], versions=["baseline", "a", "b", "c"], initial_cash=10_000.0)
    right = compare_versions(bars, symbol="SOXS.US", benchmark_bars=benchmark, versions=["baseline", "a", "b", "c"], initial_cash=10_000.0)
    assert left.to_dict() == right.to_dict()


def test_infer_frequency_supports_minute_and_hourly_series():
    feed = BacktestDataFeed()
    series = []
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    for step_minutes, expected in ((5, "5m"), (15, "15m"), (30, "30m"), (60, "1h")):
        series = [
            _bar(start + timedelta(minutes=step_minutes * idx), 10.0 + idx * 0.01, 10.1 + idx * 0.01, 9.9 + idx * 0.01, 10.05 + idx * 0.01, 1000)
            for idx in range(4)
        ]
        assert feed.from_sequence(series)
        assert infer_bar_frequency(series) == expected
