from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.utils.market_calendar import is_us_market_trading_day, nth_weekday, required_selection_date


US_EASTERN = ZoneInfo("America/New_York")
AUTO_START_TARGET_ET = time(9, 25)
AUTO_STOP_TARGET_ET = time(16, 5)
AUTO_START_WINDOW_MINUTES = 15
AUTO_STOP_WINDOW_MINUTES = 20
EARLY_CLOSE_ET = time(13, 0)


@dataclass(frozen=True)
class AutoTradeDecision:
    should_run: bool
    action: str
    reason: str
    now_et: datetime
    session_date: str
    required_selection_date: str


def _normalize_now(now_et: datetime | None = None) -> datetime:
    current = now_et or datetime.now(US_EASTERN)
    if current.tzinfo is None:
        return current.replace(tzinfo=US_EASTERN)
    return current.astimezone(US_EASTERN)


def _minutes_since_midnight(value: time) -> int:
    return value.hour * 60 + value.minute


def _in_forward_window(now_et: datetime, target: time, window_minutes: int) -> bool:
    current_minute = _minutes_since_midnight(now_et.time())
    target_minute = _minutes_since_midnight(target)
    return target_minute <= current_minute <= target_minute + int(window_minutes)


def _thanksgiving_day(year: int):
    return nth_weekday(year, 11, 3, 4)


def _is_known_us_market_early_close(session_day) -> bool:
    """Common NYSE half-days used by the local scheduler."""
    if not is_us_market_trading_day(session_day):
        return False
    if session_day.month == 12 and session_day.day == 24:
        return True
    if session_day == _thanksgiving_day(session_day.year) + timedelta(days=1):
        return True
    return False


def session_close_time_et(session_day) -> time:
    return EARLY_CLOSE_ET if _is_known_us_market_early_close(session_day) else time(16, 0)


def scheduled_stop_time_et(session_day) -> time:
    close_time = session_close_time_et(session_day)
    return (
        datetime.combine(session_day, close_time, tzinfo=US_EASTERN)
        + timedelta(minutes=5)
    ).time()


def auto_trade_decision(
    action: str,
    *,
    now_et: datetime | None = None,
    already_ran: bool = False,
) -> AutoTradeDecision:
    current = _normalize_now(now_et)
    action_name = str(action or "").strip().lower().replace("_", "-")
    session_date = current.date().isoformat()
    required_date = required_selection_date(current)

    if action_name not in {"scheduled-start", "scheduled-stop"}:
        return AutoTradeDecision(
            False,
            action_name,
            "unsupported_action",
            current,
            session_date,
            required_date,
        )

    if not is_us_market_trading_day(current.date()):
        return AutoTradeDecision(
            False,
            action_name,
            "not_us_market_trading_day",
            current,
            session_date,
            required_date,
        )

    if already_ran:
        return AutoTradeDecision(
            False,
            action_name,
            "already_ran_for_session",
            current,
            session_date,
            required_date,
        )

    if action_name == "scheduled-start":
        in_window = _in_forward_window(current, AUTO_START_TARGET_ET, AUTO_START_WINDOW_MINUTES)
        reason = "market_open_start_window" if in_window else "outside_market_open_start_window"
    else:
        stop_target = scheduled_stop_time_et(current.date())
        in_window = _in_forward_window(current, stop_target, AUTO_STOP_WINDOW_MINUTES)
        reason = "market_close_stop_window" if in_window else "outside_market_close_stop_window"

    return AutoTradeDecision(
        in_window,
        action_name,
        reason,
        current,
        session_date,
        required_date,
    )


def parse_env_now(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _normalize_now(parsed)


def marker_timestamp(now_et: datetime | None = None) -> str:
    return _normalize_now(now_et).isoformat(timespec="seconds")
