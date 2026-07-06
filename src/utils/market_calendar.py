from __future__ import annotations

import calendar
from datetime import date, timedelta


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
