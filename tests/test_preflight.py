"""Tests for market data preflight check."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.openalpha.preflight import (
    PreflightReport,
    _scan_data_availability,
    run_preflight,
    print_preflight,
)


# ═══════════════════════════════════════════════════════════════════════
# Market state detection
# ═══════════════════════════════════════════════════════════════════════

class TestMarketState:
    def test_market_open_detected(self):
        with patch("src.openalpha.preflight._et_now", return_value=__import__("datetime").datetime(2026, 7, 24, 10, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))):
            # Mock market_session_context to return regular session
            class FakeSession:
                is_market_holiday = False; is_premarket=False; is_regular_session=True
                is_after_hours=False; session_label="MARKET_OPEN"
                current_session_reason=""; current_session=__import__("datetime").date(2026,7,24)
                previous_completed_session=__import__("datetime").date(2026,7,23)
            with patch("src.openalpha.preflight.market_session_context", return_value=FakeSession()):
                report = run_preflight(symbols=["AAPL"], max_scan_symbols=1, dry_run=True)
                assert report.market_state == "MARKET_OPEN"
                assert report.run_mode in {"FULL", "DEGRADED"}

    def test_after_hours_detected(self):
        class FakeSession:
            is_market_holiday = False; is_premarket=False; is_regular_session=False
            is_after_hours=True; session_label="AFTER_HOURS"
            current_session_reason=""; current_session=__import__("datetime").date(2026,7,24)
            previous_completed_session=__import__("datetime").date(2026,7,24)
        with patch("src.openalpha.preflight.market_session_context", return_value=FakeSession()):
            report = run_preflight(symbols=["AAPL"], max_scan_symbols=1, dry_run=True)
            assert report.market_state == "AFTER_HOURS"
            assert report.run_mode == "AFTER_MARKET"
            assert report.data_mode == "EOD_ONLY"

    def test_closed_detected(self):
        class FakeSession:
            is_market_holiday = True; is_premarket=False; is_regular_session=False
            is_after_hours=False; session_label="CLOSED"
            current_session_reason="weekend"; current_session=None
            previous_completed_session=__import__("datetime").date(2026,7,23)
        with patch("src.openalpha.preflight.market_session_context", return_value=FakeSession()):
            report = run_preflight(symbols=["AAPL"], max_scan_symbols=1, dry_run=True)
            assert report.market_state == "CLOSED"
            assert report.run_mode == "AFTER_MARKET"


# ═══════════════════════════════════════════════════════════════════════
# Data availability scan
# ═══════════════════════════════════════════════════════════════════════

class TestDataAvailability:
    def test_scan_samples_symbols(self):
        availability = _scan_data_availability(["A", "B", "C", "D", "E", "F"], max_symbols=3)
        assert availability["checked"] == 3
        assert "quotes" in availability
        assert "ohlcv" in availability

    def test_empty_symbols_returns_zero(self):
        availability = _scan_data_availability([], max_symbols=10)
        assert availability["checked"] == 0

    def test_scan_with_real_symbol_triggers_no_error(self):
        availability = _scan_data_availability(["AAPL"], max_symbols=1)
        assert availability["checked"] == 1
        assert availability["quotes"] >= 0
        assert availability["ohlcv"] >= 0

    def test_run_preflight_uses_provided_symbols_for_coverage(self, monkeypatch):
        class FakeSession:
            is_market_holiday = False
            is_premarket = False
            is_regular_session = True
            is_after_hours = False
            session_label = "MARKET_OPEN"
            current_session_reason = ""
            current_session = __import__("datetime").date(2026, 7, 24)
            previous_completed_session = __import__("datetime").date(2026, 7, 23)

        captured = {}

        def fake_scan(symbols, max_symbols=20):
            captured["symbols"] = list(symbols)
            captured["max_symbols"] = max_symbols
            return {"quotes": 2, "ohlcv": 2, "checked": 4}

        monkeypatch.setattr("src.openalpha.preflight.market_session_context", lambda now_et: FakeSession())
        monkeypatch.setattr("src.openalpha.preflight._scan_data_availability", fake_scan)
        monkeypatch.setattr(
            "src.openalpha.preflight._et_now",
            lambda: __import__("datetime").datetime(2026, 7, 24, 10, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")),
        )

        report = run_preflight(
            symbols=["AAPL", "MSFT", "NVDA"],
            max_scan_symbols=2,
            dry_run=True,
        )

        assert captured["symbols"] == ["AAPL", "MSFT", "NVDA"]
        assert captured["max_symbols"] == 2
        assert report.symbols_checked == 4
        assert report.quote_coverage_pct == 50.0
        assert report.ohlcv_coverage_pct == 50.0
        assert report.run_mode == "FULL"

    def test_run_preflight_without_symbols_keeps_legacy_zero_coverage(self, monkeypatch):
        class FakeSession:
            is_market_holiday = False
            is_premarket = False
            is_regular_session = True
            is_after_hours = False
            session_label = "MARKET_OPEN"
            current_session_reason = ""
            current_session = __import__("datetime").date(2026, 7, 24)
            previous_completed_session = __import__("datetime").date(2026, 7, 23)

        called = {"count": 0}

        def fake_scan(symbols, max_symbols=20):
            called["count"] += 1
            return {"quotes": 1, "ohlcv": 1, "checked": 1}

        monkeypatch.setattr("src.openalpha.preflight.market_session_context", lambda now_et: FakeSession())
        monkeypatch.setattr("src.openalpha.preflight._scan_data_availability", fake_scan)
        monkeypatch.setattr(
            "src.openalpha.preflight._et_now",
            lambda: __import__("datetime").datetime(2026, 7, 24, 10, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")),
        )

        report = run_preflight(symbols=None, max_scan_symbols=2, dry_run=True)

        assert called["count"] == 0
        assert report.symbols_checked == 0
        assert report.quote_coverage_pct == 0.0
        assert report.ohlcv_coverage_pct == 0.0
        assert report.run_mode == "DEGRADED"


# ═══════════════════════════════════════════════════════════════════════
# Serialization
# ═══════════════════════════════════════════════════════════════════════

class TestSerialization:
    def test_to_dict_roundtrip(self):
        r = PreflightReport(
            market_state="AFTER_HOURS", run_mode="AFTER_MARKET",
            data_mode="EOD_ONLY", symbols_checked=10,
            quotes_available=3, ohlcv_available=9,
        )
        r.quote_coverage_pct = round(3/10*100, 1)
        r.ohlcv_coverage_pct = round(9/10*100, 1)
        d = r.to_dict()
        assert d["market_state"] == "AFTER_HOURS"
        assert d["run_mode"] == "AFTER_MARKET"
        assert d["quote_coverage_pct"] == 30.0

    def test_print_does_not_crash(self):
        r = PreflightReport(
            market_state="CLOSED", run_mode="EOD_ONLY",
            data_mode="EOD_ONLY",
        )
        print_preflight(r)  # No assertion — just must not raise


# ═══════════════════════════════════════════════════════════════════════
# Safety
# ═══════════════════════════════════════════════════════════════════════

class TestSafety:
    def test_no_broker_references(self):
        src = Path(__import__("src.openalpha.preflight").__file__).read_text()
        assert "LongBridgeBroker" not in src
        assert "PaperBroker" not in src
        assert "TradeContext" not in src
        assert "place_order" not in src
        assert "allow_live_order" not in src
        assert "reduce_only" not in src


def run_test_direct():
    TestMarketState().test_market_open_detected()
    TestMarketState().test_after_hours_detected()
    TestMarketState().test_closed_detected()
    TestDataAvailability().test_scan_samples_symbols()
    TestDataAvailability().test_empty_symbols_returns_zero()
    TestDataAvailability().test_scan_with_real_symbol_triggers_no_error()
    TestSerialization().test_to_dict_roundtrip()
    TestSerialization().test_print_does_not_crash()
    TestSafety().test_no_broker_references()
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()
