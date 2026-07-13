from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


US_EASTERN = ZoneInfo("America/New_York")
PREMARKET_START_ET = time(4, 0)
PREMARKET_REFRESH_START_ET = time(8, 45)
CANDIDATE_FREEZE_TIME_ET = time(9, 15)
REGULAR_SESSION_START_ET = time(9, 30)
REGULAR_SESSION_END_ET = time(16, 0)
AFTER_HOURS_END_ET = time(20, 0)


@dataclass(frozen=True)
class MarketSessionContext:
    now_et: datetime
    current_session: date
    previous_completed_session: date
    next_session: date
    is_market_holiday: bool
    is_premarket: bool
    is_regular_session: bool
    is_after_hours: bool
    market_open: bool
    session_label: str
    current_session_status: str
    current_session_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "now_et": self.now_et.isoformat(),
            "current_session": self.current_session.isoformat(),
            "previous_completed_session": self.previous_completed_session.isoformat(),
            "next_session": self.next_session.isoformat(),
            "is_market_holiday": self.is_market_holiday,
            "is_premarket": self.is_premarket,
            "is_regular_session": self.is_regular_session,
            "is_after_hours": self.is_after_hours,
            "market_open": self.market_open,
            "session_label": self.session_label,
            "current_session_status": self.current_session_status,
            "current_session_reason": self.current_session_reason,
        }


def easter_sunday(year: int) -> date:
    """Gregorian Easter date used to derive Good Friday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    day = 1 + ((weekday - first.weekday()) % 7) + (n - 1) * 7
    return date(year, month, day)


def last_weekday(year: int, month: int, weekday: int) -> date:
    day = calendar.monthrange(year, month)[1]
    value = date(year, month, day)
    return value - timedelta(days=(value.weekday() - weekday) % 7)


def us_market_holidays(year: int) -> set[date]:
    holidays = {
        observed(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter_sunday(year) - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    }
    next_new_year = observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return holidays


def is_us_market_trading_day(trade_day: date) -> bool:
    return trade_day.weekday() < 5 and trade_day not in us_market_holidays(trade_day.year)


def is_us_market_holiday(trade_day: date) -> bool:
    return trade_day.weekday() < 5 and trade_day in us_market_holidays(trade_day.year)


def previous_us_market_trading_day(trade_day: date) -> date:
    candidate = trade_day - timedelta(days=1)
    while not is_us_market_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def next_us_market_trading_day(trade_day: date) -> date:
    candidate = trade_day + timedelta(days=1)
    while not is_us_market_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def market_session_context(now_et: datetime | None = None) -> MarketSessionContext:
    current = now_et or datetime.now(US_EASTERN)
    if current.tzinfo is None:
        current = current.replace(tzinfo=US_EASTERN)
    else:
        current = current.astimezone(US_EASTERN)

    trade_day = current.date()
    trading_day = is_us_market_trading_day(trade_day)
    clock = current.time()
    is_premarket = trading_day and PREMARKET_START_ET <= clock < REGULAR_SESSION_START_ET
    is_regular_session = trading_day and REGULAR_SESSION_START_ET <= clock < REGULAR_SESSION_END_ET
    is_after_hours = trading_day and REGULAR_SESSION_END_ET <= clock < AFTER_HOURS_END_ET
    is_market_holiday = is_us_market_holiday(trade_day)
    if trading_day:
        current_session = trade_day
        previous_completed_session = trade_day if clock >= REGULAR_SESSION_END_ET else previous_us_market_trading_day(trade_day)
    else:
        current_session = next_us_market_trading_day(trade_day)
        previous_completed_session = previous_us_market_trading_day(trade_day)
    next_session = next_us_market_trading_day(current_session)
    if is_premarket:
        session_label = "PREMARKET"
        current_session_status = "OPENING_SOON"
        current_session_reason = "premarket"
    elif is_regular_session:
        session_label = "REGULAR"
        current_session_status = "OPEN"
        current_session_reason = "regular_session"
    elif is_after_hours:
        session_label = "AFTER_HOURS"
        current_session_status = "CLOSED"
        current_session_reason = "after_hours"
    elif is_market_holiday:
        session_label = "HOLIDAY"
        current_session_status = "CLOSED"
        current_session_reason = "market_holiday"
    else:
        session_label = "CLOSED"
        current_session_status = "CLOSED"
        current_session_reason = "weekend_or_closed"
    return MarketSessionContext(
        now_et=current,
        current_session=current_session,
        previous_completed_session=previous_completed_session,
        next_session=next_session,
        is_market_holiday=is_market_holiday,
        is_premarket=is_premarket,
        is_regular_session=is_regular_session,
        is_after_hours=is_after_hours,
        market_open=is_regular_session,
        session_label=session_label,
        current_session_status=current_session_status,
        current_session_reason=current_session_reason,
    )
