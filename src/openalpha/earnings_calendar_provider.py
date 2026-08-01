from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

import requests

from .config import AISelectorRuntimeConfig, load_runtime_config
from .earnings_provider import (
    EarningsInfo,
    EarningsRiskLevel,
    calculate_trading_days_to_earnings,
    normalize_earnings_info,
)
from src.utils.market_calendar import US_EASTERN


logger = logging.getLogger(__name__)

FMP_EARNINGS_CALENDAR_URL = "https://financialmodelingprep.com/stable/earnings-calendar"


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
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except Exception:
            return None


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


def _coerce_rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("earningsCalendar", "calendar", "data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
        if any(
            key in payload
            for key in (
                "symbol",
                "ticker",
                "date",
                "earnings_date",
                "earningsDate",
                "reportDate",
                "announcementDate",
                "announcement_date",
                "time",
                "reportTime",
                "timeOfDay",
                "period",
            )
        ):
            return [dict(payload)]
        return []
    return []


def _earnings_date_from_row(row: Mapping[str, Any]) -> date | None:
    for key in (
        "date",
        "earnings_date",
        "earningsDate",
        "reportDate",
        "announcementDate",
        "announcement_date",
        "next_earnings_date",
    ):
        value = row.get(key)
        parsed = _as_date(value)
        if parsed is not None:
            return parsed
    return None


def _earnings_time_from_row(row: Mapping[str, Any]) -> str:
    for key in ("time", "reportTime", "report_time", "timeOfDay", "period", "market_time"):
        value = _normalize_text(row.get(key))
        if value:
            return value.upper()
    return ""


def _updated_at_from_row(row: Mapping[str, Any], *, as_of: datetime | date | None = None) -> str:
    for key in ("updatedAt", "updated_at", "lastUpdated", "last_updated", "timestamp", "asOf"):
        value = _normalize_text(row.get(key))
        if value:
            return value
    reference = _as_datetime(as_of) or datetime.now(timezone.utc)
    return reference.astimezone(timezone.utc).isoformat()


@runtime_checkable
class EarningsProvider(Protocol):
    def get_earnings_info(
        self,
        candidate: Mapping[str, Any],
        *,
        as_of: datetime | date | None = None,
    ) -> EarningsInfo | None:
        ...


class FmpEarningsCalendarProvider:
    """Read-only FMP earnings calendar provider.

    The provider is intentionally defensive:
    - if configuration is missing, it returns ``None``
    - HTTP / parsing failures are swallowed and converted to ``None``
    - it never raises to the selection pipeline
    """

    def __init__(
        self,
        config: AISelectorRuntimeConfig | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.config = config or load_runtime_config()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(self.config, "fmp_api_key", "")
            or os.environ.get("FMP_API_KEY", "")
            or ""
        ).strip()
        if api_key is not None:
            self.enabled = bool(self.api_key)
        else:
            self.enabled = bool(getattr(self.config, "fmp_enabled", bool(self.api_key))) and bool(self.api_key)
        self.base_url = str(base_url or os.environ.get("FMP_EARNINGS_CALENDAR_URL") or FMP_EARNINGS_CALENDAR_URL).strip().rstrip("/")
        self.session = session or requests.Session()
        self.timeout_seconds = max(3, min(int(timeout_seconds or os.environ.get("SOXS_FMP_TIMEOUT_SECONDS", "10") or 10), 60))
        self._cache: dict[tuple[str, str], EarningsInfo | None] = {}

    def get_earnings_info(
        self,
        candidate: Mapping[str, Any],
        *,
        as_of: datetime | date | None = None,
    ) -> EarningsInfo | None:
        try:
            symbol = _normalize_symbol(candidate.get("ticker") or candidate.get("symbol"))
            if not symbol or not self.enabled or not self.api_key:
                return None
            as_of_dt = _as_datetime(as_of) or datetime.now(timezone.utc)
            cache_key = (symbol, as_of_dt.astimezone(US_EASTERN).date().isoformat())
            if cache_key in self._cache:
                return self._cache[cache_key]
            info = self._fetch_and_normalize(symbol, as_of=as_of_dt)
            self._cache[cache_key] = info
            return info
        except Exception:
            return None

    def _fetch_and_normalize(self, symbol: str, *, as_of: datetime) -> EarningsInfo | None:
        rows = self._fetch_rows(symbol, as_of=as_of)
        if not rows:
            return None
        chosen = self._choose_row(rows, symbol=symbol, as_of=as_of)
        if chosen is None:
            return None
        payload = self._normalize_row(chosen, symbol=symbol, as_of=as_of)
        return normalize_earnings_info(payload, symbol=symbol, as_of=as_of)

    def _fetch_rows(self, symbol: str, *, as_of: datetime) -> list[dict[str, Any]]:
        start = (as_of.astimezone(US_EASTERN).date() - timedelta(days=7)).isoformat()
        # FMP's earnings calendar endpoint is reliable for short forward windows.
        # A 365-day lookahead often returns an empty array even when shorter windows
        # (for the same symbol and same API key) do contain rows, so we keep the
        # query tightly bounded to avoid false "no data" results.
        end = (as_of.astimezone(US_EASTERN).date() + timedelta(days=30)).isoformat()
        params = {
            "symbol": symbol,
            "from": start,
            "to": end,
            "apikey": self.api_key,
            "includeReportTimes": "true",
        }
        try:
            response = self.session.get(self.base_url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug("FMP earnings calendar request failed for %s: %s", symbol, exc)
            return []
        return _coerce_rows(payload)

    def _choose_row(self, rows: list[dict[str, Any]], *, symbol: str, as_of: datetime) -> dict[str, Any] | None:
        normalized_symbol = _normalize_symbol(symbol)
        candidates: list[tuple[date, dict[str, Any]]] = []
        fallback: list[tuple[date | None, dict[str, Any]]] = []
        current_date = as_of.astimezone(US_EASTERN).date()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_symbol = _normalize_symbol(row.get("symbol") or row.get("ticker") or row.get("company_symbol"))
            if row_symbol and row_symbol != normalized_symbol:
                continue
            earnings_date = _earnings_date_from_row(row)
            if earnings_date is None:
                fallback.append((None, dict(row)))
                continue
            if earnings_date >= current_date:
                candidates.append((earnings_date, dict(row)))
            else:
                fallback.append((earnings_date, dict(row)))
        if candidates:
            candidates.sort(key=lambda item: (item[0], _normalize_text(item[1].get("time") or item[1].get("reportTime") or "")))
            return candidates[0][1]
        if fallback:
            fallback.sort(key=lambda item: (item[0] is None, item[0] or date.max))
            return fallback[0][1]
        return None

    def _normalize_row(self, row: Mapping[str, Any], *, symbol: str, as_of: datetime) -> dict[str, Any]:
        earnings_date = _earnings_date_from_row(row)
        if earnings_date is None:
            earnings_date = as_of.astimezone(US_EASTERN).date()
        trading_days = calculate_trading_days_to_earnings(
            earnings_date,
            as_of=as_of,
            market_timezone=US_EASTERN,
        )
        payload = {
            "symbol": symbol,
            "earnings_date": earnings_date.isoformat(),
            "earnings_time": _earnings_time_from_row(row),
            "market_timezone": US_EASTERN.key,
            "trading_days_to_earnings": trading_days,
            "earnings_risk_level": row.get("earnings_risk_level") or row.get("risk_level") or row.get("earningsRiskLevel"),
            "source": _normalize_text(row.get("source")) or "fmp_earnings_calendar",
            "updated_at": _updated_at_from_row(row, as_of=as_of),
            "confidence": row.get("confidence") or row.get("confidence_score") or 0.75,
        }
        return payload


class NullEarningsCalendarProvider:
    def get_earnings_info(
        self,
        candidate: Mapping[str, Any],
        *,
        as_of: datetime | date | None = None,
    ) -> EarningsInfo | None:
        return None


def get_default_earnings_provider(
    config: AISelectorRuntimeConfig | None = None,
) -> EarningsProvider:
    config = config or load_runtime_config()
    if not bool(getattr(config, "fmp_enabled", False)):
        return NullEarningsCalendarProvider()
    return FmpEarningsCalendarProvider(config=config)


def candidate_earnings_info(
    candidate: Mapping[str, Any] | None,
    *,
    provider: EarningsProvider | None = None,
    as_of: datetime | date | None = None,
) -> EarningsInfo | None:
    if not isinstance(candidate, Mapping):
        return None
    provider = provider or get_default_earnings_provider()
    try:
        return provider.get_earnings_info(candidate, as_of=as_of)
    except Exception:
        return None
