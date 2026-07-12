from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.data.longbridge_history import LongbridgeHistoryDownloader, LongbridgeHistoryError, _parse_timestamp


def _bar(symbol: str, ts: datetime, *, base: float, index: int) -> dict[str, object]:
    price = base + index * 0.1
    return {
        "symbol": symbol,
        "timestamp": ts.isoformat(),
        "open": round(price, 4),
        "high": round(price + 0.2, 4),
        "low": round(price - 0.2, 4),
        "close": round(price + 0.05, 4),
        "volume": 1000 + index,
        "turnover": round((1000 + index) * (price + 0.05), 4),
    }


class FakeQuoteContext:
    def __init__(self, config, pages):
        self.config = config
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def history_candlesticks_by_offset(self, **kwargs):
        self.calls.append(dict(kwargs))
        symbol = str(kwargs.get("symbol") or "").upper()
        period = str(kwargs.get("period") or "").lower()
        offset = int(kwargs.get("offset") or 0)
        return {"candlesticks": self.pages.get((symbol, period, offset), [])}

    def close(self):
        return None


class _TimestampMismatchCandle:
    def __init__(self, attr_timestamp: datetime, repr_timestamp: str) -> None:
        self.timestamp = attr_timestamp
        self._repr_timestamp = repr_timestamp

    def __repr__(self) -> str:
        return f'Candlestick {{ timestamp: "{self._repr_timestamp}", trade_session: Intraday }}'


def _install_fake_longbridge(monkeypatch, pages):
    fake_module = types.SimpleNamespace()
    fake_module.AdjustType = types.SimpleNamespace(ForwardAdjust="ForwardAdjust", NoAdjust="NoAdjust")
    fake_module.TradeSessions = types.SimpleNamespace(All="All", Intraday="Intraday")
    fake_module.TradeSession = types.SimpleNamespace(Normal="Normal")

    class _Config:
        @classmethod
        def from_apikey(cls, *args, **kwargs):
            return {"args": args, "kwargs": kwargs}

    fake_module.Config = _Config
    fake_module.QuoteContext = lambda config: FakeQuoteContext(config, pages)
    monkeypatch.setitem(sys.modules, "longbridge", types.SimpleNamespace(openapi=fake_module))
    monkeypatch.setitem(sys.modules, "longbridge.openapi", fake_module)
    return fake_module


def _prepare_pages() -> dict[tuple[str, str, int], list[dict[str, object]]]:
    pages: dict[tuple[str, str, int], list[dict[str, object]]] = {}
    symbols = ["SOXS.US", "SOXX.US", "SMH.US", "AAPL.US", "QQQ.US", "SPY.US"]
    for symbol in symbols:
        for period, base, step in (("15m", 10.0 if symbol != "QQQ.US" else 400.0, timedelta(minutes=15)), ("1d", 20.0 if symbol != "QQQ.US" else 300.0, timedelta(days=1))):
            start = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc) if period == "15m" else datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
            page0 = [_bar(symbol, start + step * idx, base=base, index=idx) for idx in range(4)]
            page1 = [_bar(symbol, start + step * (idx + 4), base=base, index=idx + 4) for idx in range(4)]
            pages[(symbol, period, 0)] = list(reversed(page0))
            pages[(symbol, period, 4)] = list(reversed(page1))
    return pages


def test_download_longbridge_history_writes_csv_manifest_and_validation(monkeypatch, tmp_path):
    pages = _prepare_pages()
    fake_module = _install_fake_longbridge(monkeypatch, pages)
    monkeypatch.setattr("src.data.longbridge_history.has_longbridge_runtime_credentials", lambda: True)
    monkeypatch.setattr("src.data.longbridge_history.get_runtime_env", lambda key, default=None: default)
    monkeypatch.setattr(
        "src.data.longbridge_history._latest_complete_trade_day",
        lambda now=None: date(2026, 7, 2),
    )
    downloader = LongbridgeHistoryDownloader(output_dir=tmp_path, page_size=4, request_interval_seconds=0.0)
    result = downloader.download_many(
        symbols=["SOXS.US", "SOXX.US", "SMH.US", "AAPL.US", "QQQ.US", "SPY.US"],
        frequencies=["15m", "daily"],
        intraday_start="2023-12-04",
        daily_start="2020-01-01",
    )

    assert result["files"]
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    validation = json.loads((tmp_path / "validation_report.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "longbridge_quote_api"
    assert validation["source"] == "longbridge_quote_api"
    assert len(manifest["files"]) == 12
    assert any(item["symbol"] == "SOXS.US" and item["frequency"] == "15m" for item in manifest["files"])
    assert any(item["symbol"] == "AAPL.US" and item["frequency"] == "daily" for item in manifest["files"])
    assert len(validation["validations"]) == 8
    assert all(item["eligible_for_backtest"] for item in validation["validations"])
    assert all(item["benchmark_status"] == "VALID" for item in validation["validations"])
    assert all(item["frequency_match"] for item in validation["validations"])
    assert all(item["overlap_ratio"] >= 1.0 for item in validation["validations"])
    assert all(item["duplicate_count"] == 0 for item in validation["validations"])
    assert fake_module.QuoteContext({"x": 1}).calls == []

    sample = tmp_path / "SOXS_US_15m_20231204_latest.csv"
    assert sample.exists()
    lines = sample.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("symbol,timestamp,open,high,low,close,volume,turnover,frequency,timezone,trade_session,source")
    assert len(lines) > 1


def test_download_longbridge_history_does_not_overwrite_existing_file_on_failure(monkeypatch, tmp_path):
    bad_pages = {
        ("SOXS.US", "15m", 0): [
            {
                "symbol": "SOXS.US",
                "timestamp": datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).isoformat(),
                "open": 10.0,
                "high": 9.0,
                "low": 9.5,
                "close": 9.8,
                "volume": 1000,
                "turnover": 9800,
            }
        ]
    }
    _install_fake_longbridge(monkeypatch, bad_pages)
    monkeypatch.setattr("src.data.longbridge_history.has_longbridge_runtime_credentials", lambda: True)
    monkeypatch.setattr("src.data.longbridge_history.get_runtime_env", lambda key, default=None: default)
    monkeypatch.setattr(
        "src.data.longbridge_history._latest_complete_trade_day",
        lambda now=None: date(2026, 7, 2),
    )
    existing = tmp_path / "SOXS_US_15m_20231204_latest.csv"
    existing.write_text("keep-me\n", encoding="utf-8")
    downloader = LongbridgeHistoryDownloader(output_dir=tmp_path, page_size=4, request_interval_seconds=0.0)

    with pytest.raises(LongbridgeHistoryError):
        downloader.download_symbol_frequency(
            "SOXS.US",
            "15m",
            start_date=date(2023, 12, 4),
            end_date=date(2026, 7, 2),
        )

    assert existing.read_text(encoding="utf-8") == "keep-me\n"


def test_regular_session_filter_keeps_only_et_regular_bars(monkeypatch, tmp_path):
    regular_open = datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    premarket = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
    after_hours = datetime(2026, 7, 7, 21, 0, tzinfo=timezone.utc)  # 17:00 ET
    pages = {
        ("SOXS.US", "15m", 0): [
            _bar("SOXS.US", after_hours, base=10.0, index=0),
            _bar("SOXS.US", regular_open, base=10.0, index=1),
            _bar("SOXS.US", premarket, base=10.0, index=2),
        ]
    }
    _install_fake_longbridge(monkeypatch, pages)
    monkeypatch.setattr("src.data.longbridge_history.has_longbridge_runtime_credentials", lambda: True)
    monkeypatch.setattr("src.data.longbridge_history.get_runtime_env", lambda key, default=None: default)
    monkeypatch.setattr("src.data.longbridge_history._latest_complete_trade_day", lambda now=None: date(2026, 7, 7))
    downloader = LongbridgeHistoryDownloader(output_dir=tmp_path, page_size=4, request_interval_seconds=0.0)
    artifact = downloader.download_symbol_frequency(
        "SOXS.US",
        "15m",
        start_date=date(2026, 7, 7),
        end_date=date(2026, 7, 7),
    )
    frame = pd.read_csv(artifact.path)
    assert len(frame) == 1
    assert pd.to_datetime(frame["timestamp"], utc=True).iloc[0] == regular_open
    assert frame["trade_session"].iloc[0] == "regular"


def test_timestamp_parser_prefers_repr_utc_and_rejects_ambiguous_naive_values():
    naive = datetime(2026, 6, 17, 22, 0)
    candle = _TimestampMismatchCandle(naive, "2026-06-17T14:00:00Z")
    parsed = _parse_timestamp(getattr(candle, "timestamp"), raw=candle)
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-06-17T14:00:00+00:00"

    with pytest.raises(LongbridgeHistoryError, match="ambiguous_naive_timestamp"):
        _parse_timestamp(datetime(2026, 6, 17, 22, 0))


def test_timestamp_parser_rejects_inconsistent_sources():
    candle = _TimestampMismatchCandle(
        datetime(2026, 6, 17, 22, 0, tzinfo=timezone.utc),
        "2026-06-17T14:00:00Z",
    )
    with pytest.raises(LongbridgeHistoryError, match="inconsistent_timestamp_sources"):
        _parse_timestamp(getattr(candle, "timestamp"), raw=candle)


def test_timestamp_parser_supports_epoch_and_iso_z():
    expected_utc = datetime(2024, 6, 17, 0, 0, tzinfo=timezone.utc)
    epoch = _parse_timestamp(int(expected_utc.timestamp()))
    assert epoch is not None and epoch.tzinfo == timezone.utc
    assert epoch.isoformat() == "2024-06-17T00:00:00+00:00"

    iso = _parse_timestamp("2026-06-17T14:00:00Z")
    assert iso is not None and iso.tzinfo == timezone.utc
    assert iso.isoformat() == "2026-06-17T14:00:00+00:00"


def test_cli_module_rejects_when_sdk_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "longbridge", None)
    monkeypatch.setitem(sys.modules, "longbridge.openapi", None)
    from scripts.download_longbridge_history import main

    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "k")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "s")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "t")
    monkeypatch.setattr(
        "src.data.longbridge_history._latest_complete_trade_day",
        lambda now=None: date(2026, 7, 2),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_longbridge_history.py",
            "--symbols",
            "SOXS.US",
            "--frequencies",
            "15m",
            "--intraday-start",
            "2023-12-04",
            "--daily-start",
            "2020-01-01",
            "--output-dir",
            str(tmp_path),
        ],
    )
    assert main() == 1
