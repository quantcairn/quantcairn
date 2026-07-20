from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        end = datetime(2026, 7, 10, 16, 0, tzinfo=ZoneInfo("America/New_York"))
        rows = []
        for index in range(220):
            close = 80.0 + index * 0.1
            rows.append(
                SimpleNamespace(
                    timestamp=end - timedelta(days=219 - index),
                    open=close - 0.2,
                    high=close + 0.8,
                    low=close - 0.8,
                    close=close,
                    volume=120_000 + index,
                )
            )
        return rows


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
    assert snapshot["quote_status"] == "COMPLETE"
    assert snapshot["ohlcv_status"] == "COMPLETE"
    assert snapshot["history_status"] == "COMPLETE"
    assert snapshot["history_available_bars"] == 220
    assert snapshot["history_required_bars"] == 200
    assert snapshot["history_missing_windows"] == []
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
    seen_periods: list[tuple[str, str]] = []

    class _TrackingPriceFetcher(_FakePriceFetcher):
        def __init__(self, symbol: str, poll_interval: int = 0):
            seen_symbols.append(symbol)
            super().__init__(symbol, poll_interval=poll_interval)

        def get_ohlcv(self, period: str = "1mo", interval: str = "1d"):
            seen_periods.append((period, interval))
            return super().get_ohlcv(period=period, interval=interval)

    monkeypatch.setattr(market_context, "PriceFetcher", _TrackingPriceFetcher)
    monkeypatch.setattr(market_context, "default_benchmarks_for", lambda symbol: ["SOXX.US", "SMH.US"])
    now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))

    snapshot = market_context.build_candidate_market_snapshot("SOXS.US", now_et=now_et)

    assert seen_symbols[0] == "SOXS"
    assert seen_symbols[1:] == ["SOXX.US", "SMH.US"]
    assert seen_periods == [("1y", "1d"), ("1y", "1d"), ("1y", "1d")]
    assert snapshot["quote_fetch_status"] == "COMPLETE"
    assert snapshot["ohlcv_fetch_status"] == "COMPLETE"
    assert snapshot["benchmark_quote_fetch_status"] == {"SOXX.US": "COMPLETE", "SMH.US": "COMPLETE"}
    assert snapshot["benchmark_ohlcv_fetch_status"] == {"SOXX.US": "COMPLETE", "SMH.US": "COMPLETE"}
    assert snapshot["benchmark_status"] == "VALID"


def test_build_candidate_market_snapshot_marks_short_history_explicitly(monkeypatch):
    monkeypatch.setenv("SOXS_ENABLE_LIVE_MARKET_SNAPSHOT_IN_TESTS", "1")

    class _ShortHistoryFetcher(_FakePriceFetcher):
        def get_ohlcv(self, period: str = "1mo", interval: str = "1d"):
            return super().get_ohlcv(period=period, interval=interval)[-30:]

    monkeypatch.setattr(market_context, "PriceFetcher", _ShortHistoryFetcher)
    now_et = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))

    snapshot = market_context.build_candidate_market_snapshot("SOFI.US", now_et=now_et)

    assert snapshot["ohlcv_status"] == "COMPLETE"
    assert snapshot["history_status"] == "MISSING"
    assert snapshot["history_available_bars"] == 30
    assert snapshot["history_required_bars"] == 200
    assert snapshot["history_missing_windows"] == ["ma50", "ma200"]
