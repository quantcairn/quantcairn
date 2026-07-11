from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.data_feed import BacktestDataError, BacktestDataFeed


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
