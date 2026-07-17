from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from zoneinfo import ZoneInfo

from src.ai_selector import market_context
from src.utils.market_calendar import market_session_context


class _FakePriceFetcher:
    def __init__(self, symbol: str, poll_interval: int = 0):
        self.symbol = symbol
        self._last_quote_fetch_status = "COMPLETE"
        self._last_quote_error_code = None
        self._last_quote_error_message = None
        self._last_history_fetch_status = "COMPLETE"
        self._last_history_error_code = None
        self._last_history_error_message = None
        self._cache_status = "COMPLETE"
        self._cache_error_message = None

    def get_quote(self):
        now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))
        return SimpleNamespace(
            price=101.25,
            last_done=101.25,
            bid=101.0,
            ask=101.5,
            volume=250_000,
            timestamp=now_et.astimezone(timezone.utc),
        )

    def get_ohlcv(self, period: str = "1mo", interval: str = "1d"):
        return [
            SimpleNamespace(timestamp=datetime(2026, 7, 9, 16, 0, tzinfo=ZoneInfo("America/New_York")), close=99.0, volume=110_000),
            SimpleNamespace(timestamp=datetime(2026, 7, 10, 16, 0, tzinfo=ZoneInfo("America/New_York")), close=100.0, volume=120_000),
        ]


def test_market_session_context_handles_monday_premarket_previous_completed_session():
    now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))
    context = market_session_context(now_et)

    assert context.current_session.isoformat() == "2026-07-13"
    assert context.previous_completed_session.isoformat() == "2026-07-10"
    assert context.next_session.isoformat() == "2026-07-14"
    assert context.is_premarket is True
    assert context.is_regular_session is False
    assert context.is_after_hours is False
    assert context.is_market_holiday is False


def test_market_session_context_handles_july_fourth_holiday_roll_forward():
    now_et = datetime(2026, 7, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    context = market_session_context(now_et)

    assert context.is_market_holiday is True
    assert context.current_session.isoformat() == "2026-07-06"
    assert context.previous_completed_session.isoformat() == "2026-07-02"
    assert context.next_session.isoformat() == "2026-07-07"
    assert context.is_regular_session is False
    assert context.is_premarket is False


def test_build_candidate_market_snapshot_keeps_friday_daily_data_latest_completed_session(monkeypatch):
    monkeypatch.setenv("SOXS_ENABLE_LIVE_MARKET_SNAPSHOT_IN_TESTS", "1")
    monkeypatch.setattr(market_context, "PriceFetcher", _FakePriceFetcher)
    now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))

    snapshot = market_context.build_candidate_market_snapshot("SOXS.US", now_et=now_et)

    assert snapshot["current_session"] == "2026-07-13"
    assert snapshot["previous_completed_session"] == "2026-07-10"
    assert snapshot["daily_data_as_of"] == "2026-07-10"
    assert snapshot["daily_data_status"] == "LATEST_COMPLETED_SESSION"
    assert snapshot["selection_stage"] == "PREMARKET_REFRESHED"
    assert snapshot["freshness_status"] == "SAFE"
    assert snapshot["stale_reason"] == ""
    assert snapshot["premarket_snapshot_available"] is True
    assert snapshot["benchmark_status"] == "VALID"
    assert snapshot["benchmark_alignment_status"] == "VALID"
    assert snapshot["trading_eligible"] is False
    assert snapshot["shadow_enabled"] is False
    assert snapshot["paper_enabled"] is False
    assert snapshot["live_enabled"] is False


def test_build_candidate_market_snapshot_uses_generic_benchmark_fallback_for_unknown_common_stock(monkeypatch):
    monkeypatch.setenv("SOXS_ENABLE_LIVE_MARKET_SNAPSHOT_IN_TESTS", "1")
    monkeypatch.setattr(market_context, "PriceFetcher", _FakePriceFetcher)
    now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))

    snapshot = market_context.build_candidate_market_snapshot("SOFI.US", now_et=now_et)

    assert snapshot["benchmark_symbols"] == ["QQQ.US", "SPY.US"]
    assert snapshot["benchmark_status"] == "VALID"
    assert snapshot["benchmark_alignment_status"] == "VALID"
    assert snapshot["selection_stage"] == "PREMARKET_REFRESHED"
    assert snapshot["freshness_status"] == "SAFE"
    assert snapshot["stale_reason"] == ""


def test_build_candidate_market_snapshot_records_fetch_diagnostics_and_normalizes_benchmarks(monkeypatch):
    monkeypatch.setenv("SOXS_ENABLE_LIVE_MARKET_SNAPSHOT_IN_TESTS", "1")
    seen_symbols: list[str] = []

    class _TrackingPriceFetcher(_FakePriceFetcher):
        def __init__(self, symbol: str, poll_interval: int = 0):
            seen_symbols.append(symbol)
            super().__init__(symbol, poll_interval=poll_interval)

    monkeypatch.setattr(market_context, "PriceFetcher", _TrackingPriceFetcher)
    monkeypatch.setattr(market_context, "default_benchmarks_for", lambda symbol: ["SOXX.US", "SMH.US"])
    now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))

    snapshot = market_context.build_candidate_market_snapshot("SOXS.US", now_et=now_et)

    assert seen_symbols[0] == "SOXS"
    assert seen_symbols[1:] == ["SOXX.US", "SMH.US"]
    assert snapshot["quote_fetch_status"] == "COMPLETE"
    assert snapshot["ohlcv_fetch_status"] == "COMPLETE"
    assert snapshot["benchmark_quote_fetch_status"] == {"SOXX.US": "COMPLETE", "SMH.US": "COMPLETE"}
    assert snapshot["benchmark_ohlcv_fetch_status"] == {"SOXX.US": "COMPLETE", "SMH.US": "COMPLETE"}
    assert snapshot["benchmark_status"] == "VALID"
