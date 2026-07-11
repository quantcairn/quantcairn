from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.strategy.time_stop import TimeStop


def test_time_stop_triggers_after_bar_threshold():
    checker = TimeStop()
    entry = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    current = entry + timedelta(minutes=35)
    result = checker.evaluate(
        symbol="SOXS.US",
        entry_time=entry,
        current_time=current,
        holding_bars=21,
        holding_minutes=35,
        configured_max_bars=20,
        configured_max_minutes=240,
    )

    assert result["triggered"] is True
    assert result["reason"] == "time_stop_bars"
    assert result["exit_priority"] > 0


def test_time_stop_leveraged_is_stricter():
    checker = TimeStop()
    entry = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    current = entry + timedelta(minutes=40)
    result = checker.evaluate(
        symbol="SOXS.US",
        entry_time=entry,
        current_time=current,
        holding_bars=16,
        holding_minutes=121,
        leveraged_etf=True,
        configured_max_bars=30,
        configured_max_minutes=240,
    )

    assert result["triggered"] is True
    assert result["configured_max_bars"] == 15
    assert result["configured_max_minutes"] == 120


def test_time_stop_is_fail_closed_without_timestamps():
    checker = TimeStop()
    result = checker.evaluate(
        symbol="SOXS.US",
        entry_time=None,
        current_time=None,
        holding_bars=100,
        holding_minutes=1000,
    )

    assert result["triggered"] is False
    assert result["reason"] == ""
