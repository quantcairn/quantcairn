"""Tests for US market calendar: trading day, holiday, and weekend detection."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.utils.market_calendar import (
    US_EASTERN,
    is_us_market_trading_day,
    is_us_market_holiday,
    us_market_holidays,
    market_session_context,
)

# ── Known 2026 US market holidays (from NYSE calendar) ──────────────────
# Derived from the algorithm in market_calendar.py; validated against
# published NYSE schedules.
_HOLIDAYS_2026 = sorted(us_market_holidays(2026))


class TestWeekendDetection:
    """Weekends (Sat/Sun) are never trading days."""

    def test_sunday_is_not_trading_day(self):
        assert not is_us_market_trading_day(date(2026, 7, 26))

    def test_saturday_is_not_trading_day(self):
        assert not is_us_market_trading_day(date(2026, 7, 25))

    def test_sunday_is_not_holiday(self):
        # Sunday is not a holiday — it's just a weekend
        assert not is_us_market_holiday(date(2026, 7, 26))

    def test_all_weekends_are_non_trading(self):
        """Regression: every Saturday and Sunday in 2026 returns False."""
        d = date(2026, 1, 3)  # first Saturday of 2026
        count = 0
        while d.year == 2026:
            if d.weekday() >= 5:
                assert not is_us_market_trading_day(d), f"{d.isoformat()} should not be a trading day"
                count += 1
            d = date.fromordinal(d.toordinal() + 1)
        assert count == 104  # 52 Saturdays + 52 Sundays


class TestMarketHolidayDetection:
    """Known US market holidays are correctly identified as non-trading days."""

    def test_new_years_day_is_holiday(self):
        """2026-01-01 (Thu) is New Year's Day — market closed."""
        assert not is_us_market_trading_day(date(2026, 1, 1))
        assert is_us_market_holiday(date(2026, 1, 1))

    def test_mlk_day_is_holiday(self):
        """2026-01-19 (Mon) is Martin Luther King Jr. Day."""
        assert not is_us_market_trading_day(date(2026, 1, 19))
        assert is_us_market_holiday(date(2026, 1, 19))

    def test_presidents_day_is_holiday(self):
        """2026-02-16 (Mon) is Presidents' Day."""
        assert not is_us_market_trading_day(date(2026, 2, 16))
        assert is_us_market_holiday(date(2026, 2, 16))

    def test_good_friday_is_holiday(self):
        """2026-04-03 (Fri) is Good Friday."""
        assert not is_us_market_trading_day(date(2026, 4, 3))
        assert is_us_market_holiday(date(2026, 4, 3))

    def test_memorial_day_is_holiday(self):
        """2026-05-25 (Mon) is Memorial Day."""
        assert not is_us_market_trading_day(date(2026, 5, 25))
        assert is_us_market_holiday(date(2026, 5, 25))

    def test_juneteenth_is_holiday(self):
        """2026-06-19 (Fri) is Juneteenth."""
        assert not is_us_market_trading_day(date(2026, 6, 19))
        assert is_us_market_holiday(date(2026, 6, 19))

    def test_independence_day_observed_is_holiday(self):
        """2026-07-04 (Sat) observed Fri 7/3 — market closed Fri."""
        assert not is_us_market_trading_day(date(2026, 7, 3))
        assert is_us_market_holiday(date(2026, 7, 3))

    def test_labor_day_is_holiday(self):
        """2026-09-07 (Mon) is Labor Day."""
        assert not is_us_market_trading_day(date(2026, 9, 7))
        assert is_us_market_holiday(date(2026, 9, 7))

    def test_thanksgiving_is_holiday(self):
        """2026-11-26 (Thu) is Thanksgiving."""
        assert not is_us_market_trading_day(date(2026, 11, 26))
        assert is_us_market_holiday(date(2026, 11, 26))

    def test_christmas_is_holiday(self):
        """2026-12-25 (Fri) is Christmas — market closed."""
        assert not is_us_market_trading_day(date(2026, 12, 25))
        assert is_us_market_holiday(date(2026, 12, 25))

    def test_holidays_count_is_ten(self):
        """2026 has exactly 10 US market holidays (NYSE calendar)."""
        assert len(_HOLIDAYS_2026) == 10

    def test_weekday_holiday_is_not_trading_day(self):
        """Every weekday holiday is not a trading day."""
        for h in _HOLIDAYS_2026:
            assert h.weekday() < 5, f"{h.isoformat()} holiday falls on weekend"
            assert not is_us_market_trading_day(h)


class TestNormalTradingDay:
    """Regular weekdays (non-holiday) are trading days."""

    def test_friday_is_trading_day(self):
        """2026-07-24 (Fri) is a normal trading day."""
        assert is_us_market_trading_day(date(2026, 7, 24))

    def test_monday_is_trading_day(self):
        """2026-07-20 (Mon) is a normal trading day."""
        assert is_us_market_trading_day(date(2026, 7, 20))

    def test_tuesday_is_trading_day(self):
        """2026-01-06 (Tue) is a normal trading day."""
        assert is_us_market_trading_day(date(2026, 1, 6))

    def test_wednesday_after_holiday_is_trading_day(self):
        """2026-01-21 (Wed) is trading day — holiday Mon is over."""
        assert not is_us_market_trading_day(date(2026, 1, 19))  # MLK Day
        assert is_us_market_trading_day(date(2026, 1, 20))       # Tue — trading
        assert is_us_market_trading_day(date(2026, 1, 21))       # Wed — trading


class TestMarketSessionContext:
    """market_session_context() reports correct session labels."""

    def test_weekend_sunday_session_closed(self):
        """Sunday afternoon — session is CLOSED, not a trading day."""
        # 2026-07-26 14:00 ET (Sunday afternoon)
        ctx = market_session_context(datetime(2026, 7, 26, 14, 0, tzinfo=US_EASTERN))
        assert ctx.session_label == "CLOSED"
        assert not ctx.market_open
        assert ctx.current_session_reason == "weekend_or_closed"

    def test_holiday_session_holiday_label(self):
        """New Year's Day 2026-01-01 at 10:00 ET — HOLIDAY."""
        ctx = market_session_context(datetime(2026, 1, 1, 10, 0, tzinfo=US_EASTERN))
        assert ctx.session_label == "HOLIDAY"
        assert ctx.is_market_holiday
        assert not ctx.market_open

    def test_regular_session_market_open(self):
        """2026-07-24 10:00 ET (Friday) — REGULAR session, market open."""
        ctx = market_session_context(datetime(2026, 7, 24, 10, 0, tzinfo=US_EASTERN))
        assert ctx.session_label == "REGULAR"
        assert ctx.market_open
        assert ctx.is_regular_session

    def test_premarket_session_label(self):
        """2026-07-24 08:00 ET (Friday) — PREMARKET."""
        ctx = market_session_context(datetime(2026, 7, 24, 8, 0, tzinfo=US_EASTERN))
        assert ctx.session_label == "PREMARKET"
        assert not ctx.market_open

    def test_after_hours_session_label(self):
        """2026-07-24 17:00 ET (Friday) — AFTER_HOURS."""
        ctx = market_session_context(datetime(2026, 7, 24, 17, 0, tzinfo=US_EASTERN))
        assert ctx.session_label == "AFTER_HOURS"
        assert not ctx.market_open


class TestAISelectorWrapperTradingDay:
    """The wrapper's is_trading_day() delegates to market_calendar."""

    def test_wrapper_is_trading_day_matches_calendar(self):
        from scripts.ai_selector_wrapper import is_trading_day as wrapper_is_trading_day
        from datetime import datetime as dt

        for d_str, expected in [
            ("2026-07-26", False),  # Sunday
            ("2026-07-25", False),  # Saturday
            ("2026-07-24", True),   # Friday
            ("2026-01-01", False),  # New Year's Day
            ("2026-01-19", False),  # MLK Day
            ("2026-12-25", False),  # Christmas
        ]:
            d = date.fromisoformat(d_str)
            # Create an ET-aware datetime at 09:00 ET
            now_et = dt(d.year, d.month, d.day, 9, 0, tzinfo=US_EASTERN)
            result = wrapper_is_trading_day(now_et)
            assert result == expected, f"{d_str}: expected={expected} got={result}"
