from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from src.utils.market_calendar import (
    US_EASTERN,
    is_us_market_trading_day,
    next_us_market_trading_day,
)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_symbol(value: Any) -> str:
    return _normalize_text(value).upper().split(".")[0]


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _normalize_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
        return dt.date()


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    else:
        text = _normalize_text(value)
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in {float("inf"), float("-inf")}:
        return default
    return result


class EarningsRiskLevel(str, Enum):
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EarningsInfo:
    symbol: str
    earnings_date: str
    earnings_time: str
    market_timezone: str
    trading_days_to_earnings: int
    earnings_risk_level: EarningsRiskLevel
    source: str
    updated_at: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "earnings_date": self.earnings_date,
            "earnings_time": self.earnings_time,
            "market_timezone": self.market_timezone,
            "trading_days_to_earnings": int(self.trading_days_to_earnings),
            "earnings_risk_level": self.earnings_risk_level.value,
            "source": self.source,
            "updated_at": self.updated_at,
            "confidence": float(self.confidence),
        }


@runtime_checkable
class EarningsProvider(Protocol):
    def get_earnings_info(
        self,
        candidate: Mapping[str, Any],
        *,
        as_of: datetime | date | None = None,
    ) -> EarningsInfo | None:
        ...


def _risk_level_from_trading_days(trading_days: int | None) -> EarningsRiskLevel:
    if trading_days is None or trading_days < 0:
        return EarningsRiskLevel.UNKNOWN
    if trading_days <= 1:
        return EarningsRiskLevel.VERY_HIGH
    if trading_days == 2:
        return EarningsRiskLevel.HIGH
    if 3 <= trading_days <= 7:
        return EarningsRiskLevel.MEDIUM
    return EarningsRiskLevel.LOW


def calculate_trading_days_to_earnings(
    earnings_date: Any,
    *,
    as_of: datetime | date | None = None,
    market_timezone: str | ZoneInfo | None = None,
) -> int | None:
    target_date = _as_date(earnings_date)
    if target_date is None:
        return None
    current = _as_datetime(as_of) or datetime.now(timezone.utc)
    if market_timezone is None:
        tz = US_EASTERN
    elif isinstance(market_timezone, ZoneInfo):
        tz = market_timezone
    else:
        try:
            tz = ZoneInfo(str(market_timezone))
        except Exception:
            tz = US_EASTERN
    current_et = current.astimezone(tz)
    as_of_date = current_et.date()
    target_trading_date = target_date if is_us_market_trading_day(target_date) else next_us_market_trading_day(target_date)
    if target_trading_date < as_of_date:
        return None
    if target_trading_date == as_of_date:
        return 0
    trading_days = 0
    session = as_of_date
    while session < target_trading_date:
        session = next_us_market_trading_day(session)
        if session <= target_trading_date:
            trading_days += 1
    return trading_days


def _extract_candidate_earnings_payload(candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not isinstance(candidate, Mapping):
        return payload

    nested_sources = []
    for key in ("earnings_info", "market_data", "trade_market_data", "metrics"):
        value = candidate.get(key)
        if isinstance(value, Mapping):
            nested_sources.append(value)

    for source in nested_sources:
        payload.update({str(k): v for k, v in source.items() if v is not None})

    for key in (
        "symbol",
        "ticker",
        "earnings_date",
        "earnings_time",
        "market_timezone",
        "trading_days_to_earnings",
        "days_to_earnings",
        "earnings_risk_level",
        "source",
        "updated_at",
        "confidence",
    ):
        if candidate.get(key) is not None:
            payload[key] = candidate.get(key)
    return payload


def normalize_earnings_info(
    payload: Mapping[str, Any] | None,
    *,
    symbol: str | None = None,
    as_of: datetime | date | None = None,
) -> EarningsInfo | None:
    if not isinstance(payload, Mapping):
        return None

    raw = {str(k): v for k, v in payload.items() if v is not None}
    resolved_symbol = _normalize_symbol(symbol or raw.get("symbol") or raw.get("ticker"))
    if not resolved_symbol:
        return None

    earnings_date = _as_date(raw.get("earnings_date") or raw.get("next_earnings_date"))
    if earnings_date is None:
        return None

    market_timezone = _normalize_text(raw.get("market_timezone")) or US_EASTERN.key
    trading_days = raw.get("trading_days_to_earnings")
    if trading_days is None:
        trading_days = raw.get("days_to_earnings")
    trading_days_int = None
    try:
        if trading_days is not None and trading_days != "":
            trading_days_int = int(float(trading_days))
    except (TypeError, ValueError):
        trading_days_int = None
    if trading_days_int is None:
        trading_days_int = calculate_trading_days_to_earnings(
            earnings_date,
            as_of=as_of,
            market_timezone=market_timezone,
        )
    if trading_days_int is None:
        return None

    risk_level_raw = _normalize_text(raw.get("earnings_risk_level")).upper()
    risk_level = _risk_level_from_trading_days(trading_days_int)
    if risk_level_raw and risk_level_raw in EarningsRiskLevel.__members__:
        # Preserve a valid explicit level only if it is consistent or more conservative.
        explicit_level = EarningsRiskLevel[risk_level_raw]
        order = {
            EarningsRiskLevel.UNKNOWN: -1,
            EarningsRiskLevel.LOW: 0,
            EarningsRiskLevel.MEDIUM: 1,
            EarningsRiskLevel.HIGH: 2,
            EarningsRiskLevel.VERY_HIGH: 3,
        }
        if order[explicit_level] >= order[risk_level]:
            risk_level = explicit_level

    source = _normalize_text(raw.get("source")) or "earnings_provider"
    updated_at = _normalize_text(raw.get("updated_at"))
    if not updated_at:
        reference = _as_datetime(as_of) or datetime.now(timezone.utc)
        updated_at = reference.astimezone(timezone.utc).isoformat()

    confidence = _as_float(raw.get("confidence"), default=0.5)
    confidence = max(0.0, min(1.0, confidence if confidence is not None else 0.5))

    return EarningsInfo(
        symbol=resolved_symbol,
        earnings_date=earnings_date.isoformat(),
        earnings_time=_normalize_text(raw.get("earnings_time")),
        market_timezone=market_timezone,
        trading_days_to_earnings=trading_days_int,
        earnings_risk_level=risk_level,
        source=source,
        updated_at=updated_at,
        confidence=confidence,
    )


class CandidateEarningsProvider:
    """Default read-only earnings provider for candidate enrichment."""

    def get_earnings_info(
        self,
        candidate: Mapping[str, Any],
        *,
        as_of: datetime | date | None = None,
    ) -> EarningsInfo | None:
        try:
            payload = _extract_candidate_earnings_payload(candidate)
            if not payload:
                return None
            return normalize_earnings_info(
                payload,
                symbol=str(candidate.get("ticker") or candidate.get("symbol") or ""),
                as_of=as_of,
            )
        except Exception:
            return None


def get_default_earnings_provider() -> EarningsProvider:
    return CandidateEarningsProvider()


def candidate_earnings_info(
    candidate: Mapping[str, Any] | None,
    *,
    provider: EarningsProvider | None = None,
    as_of: datetime | date | None = None,
) -> EarningsInfo | None:
    provider = provider or get_default_earnings_provider()
    if not isinstance(candidate, Mapping):
        return None
    try:
        return provider.get_earnings_info(candidate, as_of=as_of)
    except Exception:
        return None
