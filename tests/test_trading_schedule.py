from __future__ import annotations

import plistlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.utils.trading_schedule import auto_trade_decision


PROJECT_DIR = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def test_scheduled_start_runs_near_us_market_open_in_dst():
    decision = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 21, 9, 25, tzinfo=ET),
    )

    assert decision.should_run is True
    assert decision.reason == "market_open_start_window"
    assert decision.session_date == "2026-07-21"
    assert decision.required_selection_date == "2026-07-20"


def test_scheduled_start_runs_near_us_market_open_in_standard_time():
    decision = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 1, 6, 9, 25, tzinfo=ET),
    )

    assert decision.should_run is True
    assert decision.reason == "market_open_start_window"


def test_scheduled_stop_runs_after_us_market_close():
    decision = auto_trade_decision(
        "scheduled-stop",
        now_et=datetime(2026, 7, 21, 16, 5, tzinfo=ET),
    )

    assert decision.should_run is True
    assert decision.reason == "market_close_stop_window"
    assert decision.required_selection_date == "2026-07-21"


def test_scheduled_stop_uses_known_half_day_close():
    half_day = auto_trade_decision(
        "scheduled-stop",
        now_et=datetime(2026, 11, 27, 13, 5, tzinfo=ET),
    )
    late_full_day_time = auto_trade_decision(
        "scheduled-stop",
        now_et=datetime(2026, 11, 27, 16, 5, tzinfo=ET),
    )

    assert half_day.should_run is True
    assert half_day.reason == "market_close_stop_window"
    assert late_full_day_time.should_run is False
    assert late_full_day_time.reason == "outside_market_close_stop_window"


def test_scheduled_actions_skip_weekends_and_holidays():
    weekend = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 18, 9, 25, tzinfo=ET),
    )
    holiday = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 3, 9, 25, tzinfo=ET),
    )

    assert weekend.should_run is False
    assert weekend.reason == "not_us_market_trading_day"
    assert holiday.should_run is False
    assert holiday.reason == "not_us_market_trading_day"


def test_scheduled_actions_are_idempotent_per_session_marker():
    decision = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 21, 9, 25, tzinfo=ET),
        already_ran=True,
    )

    assert decision.should_run is False
    assert decision.reason == "already_ran_for_session"


def test_launchd_templates_use_market_calendar_scheduled_entrypoints():
    start_plist = plistlib.loads((PROJECT_DIR / "launchd/com.soxs.arbitrage.plist").read_bytes())
    stop_plist = plistlib.loads((PROJECT_DIR / "launchd/com.soxs.arbitrage.stop.plist").read_bytes())

    assert start_plist["ProgramArguments"][-1] == "scheduled-start"
    assert stop_plist["ProgramArguments"][-1] == "scheduled-stop"
    assert start_plist["StartInterval"] == 60
    assert stop_plist["StartInterval"] == 60
    assert "StartCalendarInterval" not in start_plist
    assert "StartCalendarInterval" not in stop_plist
