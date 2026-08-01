from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.openalpha.earnings_calendar_provider import (
    EarningsRiskLevel,
    FmpEarningsCalendarProvider,
    calculate_trading_days_to_earnings,
    candidate_earnings_info,
    get_default_earnings_provider,
    normalize_earnings_info,
)


class DummyResponse:
    def __init__(self, payload, status_code: int = 200, exc: Exception | None = None):
        self.payload = payload
        self.status_code = status_code
        self.exc = exc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")

    def json(self):
        if self.exc is not None:
            raise self.exc
        return self.payload


class DummySession:
    def __init__(self, response: DummyResponse | None = None, *, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict | None, int | None]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response


def test_calculate_trading_days_to_earnings_uses_trading_days_not_calendar_days():
    as_of = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    assert calculate_trading_days_to_earnings("2026-08-03", as_of=as_of) == 0
    assert calculate_trading_days_to_earnings("2026-08-04", as_of=as_of) == 1
    assert calculate_trading_days_to_earnings("2026-08-05", as_of=as_of) == 2
    assert calculate_trading_days_to_earnings("2026-08-10", as_of=as_of) >= 5


def test_normalize_earnings_info_maps_risk_level_from_trading_days():
    info = normalize_earnings_info(
        {
            "symbol": "NVDA",
            "earnings_date": "2026-08-04",
            "trading_days_to_earnings": 1,
            "market_timezone": "America/New_York",
            "source": "unit_test",
        },
        as_of=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    )
    assert info is not None
    assert info.earnings_risk_level == EarningsRiskLevel.VERY_HIGH

    medium = normalize_earnings_info(
        {
            "symbol": "NVDA",
            "earnings_date": "2026-08-06",
            "trading_days_to_earnings": 3,
            "market_timezone": "America/New_York",
            "source": "unit_test",
        },
        as_of=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    )
    assert medium is not None
    assert medium.earnings_risk_level == EarningsRiskLevel.MEDIUM


def test_fmp_provider_parses_success_response():
    response = DummyResponse(
        [
            {
                "symbol": "NVDA",
                "date": "2026-08-04",
                "time": "amc",
                "updatedAt": "2026-08-01T12:00:00Z",
                "confidence": 0.91,
            }
        ]
    )
    session = DummySession(response)
    provider = FmpEarningsCalendarProvider(
        api_key="demo",
        base_url="https://example.com/stable/earnings-calendar",
        session=session,
        timeout_seconds=7,
    )

    info = provider.get_earnings_info({"ticker": "NVDA"}, as_of=datetime(2026, 8, 3, 12, tzinfo=timezone.utc))

    assert info is not None
    assert info.symbol == "NVDA"
    assert info.earnings_date == "2026-08-04"
    assert info.earnings_time == "AMC"
    assert info.earnings_risk_level == EarningsRiskLevel.VERY_HIGH
    assert info.source == "fmp_earnings_calendar"
    assert info.confidence == pytest.approx(0.91)
    assert len(session.calls) == 1
    _, params, timeout = session.calls[0]
    assert params["symbol"] == "NVDA"
    assert params["from"] == "2026-07-27"
    assert params["to"] == "2026-09-02"
    assert params["apikey"] == "demo"
    assert params["includeReportTimes"] == "true"
    assert timeout == 7


def test_fmp_provider_missing_api_key_falls_back_without_call():
    session = DummySession(DummyResponse([]))
    provider = FmpEarningsCalendarProvider(
        api_key="",
        base_url="https://example.com/stable/earnings-calendar",
        session=session,
    )

    assert provider.get_earnings_info({"ticker": "NVDA"}) is None
    assert session.calls == []


def test_fmp_provider_request_exception_falls_back():
    session = DummySession(exc=TimeoutError("timeout"))
    provider = FmpEarningsCalendarProvider(
        api_key="demo",
        base_url="https://example.com/stable/earnings-calendar",
        session=session,
    )

    assert provider.get_earnings_info({"ticker": "NVDA"}) is None
    assert len(session.calls) == 1


def test_fmp_provider_invalid_response_falls_back():
    session = DummySession(DummyResponse({"unexpected": "shape"}))
    provider = FmpEarningsCalendarProvider(
        api_key="demo",
        base_url="https://example.com/stable/earnings-calendar",
        session=session,
    )

    assert provider.get_earnings_info({"ticker": "NVDA"}) is None


def test_fmp_provider_empty_array_falls_back():
    session = DummySession(DummyResponse([]))
    provider = FmpEarningsCalendarProvider(
        api_key="demo",
        base_url="https://example.com/stable/earnings-calendar",
        session=session,
    )

    assert provider.get_earnings_info({"ticker": "NVDA"}) is None
    assert len(session.calls) == 1


def test_default_provider_returns_safe_provider_when_disabled():
    provider = get_default_earnings_provider(
        type(
            "Cfg",
            (),
            {
                "fmp_enabled": False,
                "fmp_api_key": "",
            },
        )()
    )
    assert candidate_earnings_info({"ticker": "NVDA"}, provider=provider) is None
