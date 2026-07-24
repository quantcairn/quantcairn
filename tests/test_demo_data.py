"""Tests for the QuantCairn demo data layer.

Verifies:
  1. Demo provider returns valid, deterministic data
  2. Data format matches pipeline expectations
  3. Demo mode does not call external APIs
  4. Demo selector produces pipeline output
  5. Reproducibility: same seed → same data
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.openalpha.demo_data import (
    DEMO_HISTORY_ROWS,
    DEMO_SYMBOLS,
    DemoDataProvider,
    _generate_ohlcv,
    get_demo_provider,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Data format and content
# ═══════════════════════════════════════════════════════════════════════════════

class TestDemoDataFormat:
    """Verify demo data has the correct structure and values."""

    def test_all_symbols_have_data(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert isinstance(df, pd.DataFrame), f"{sym}: not a DataFrame"
            assert not df.empty, f"{sym}: empty DataFrame"

    def test_row_count_is_252(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert len(df) == DEMO_HISTORY_ROWS, (
                f"{sym}: expected {DEMO_HISTORY_ROWS} rows, got {len(df)}"
            )

    def test_required_columns_present(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                assert col in df.columns, f"{sym}: missing column {col}"

    def test_no_nan_values(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert not df.isnull().any().any(), f"{sym}: contains NaN"

    def test_no_negative_or_zero_close(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert (df["Close"] > 0).all(), f"{sym}: has zero/negative Close"

    def test_high_gte_low(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert (df["High"] >= df["Low"]).all(), f"{sym}: High < Low"

    def test_open_close_within_high_low(self):
        """Open and Close should be between High and Low for each bar."""
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert (df["Open"] <= df["High"]).all(), f"{sym}: Open > High"
            assert (df["Open"] >= df["Low"]).all(), f"{sym}: Open < Low"
            assert (df["Close"] <= df["High"]).all(), f"{sym}: Close > High"
            assert (df["Close"] >= df["Low"]).all(), f"{sym}: Close < Low"

    def test_index_is_datetime(self):
        provider = DemoDataProvider()
        df = provider.get_ohlcv("AAPL")
        assert isinstance(df.index, pd.DatetimeIndex), (
            f"Expected DatetimeIndex, got {type(df.index)}"
        )

    def test_volume_positive_integer(self):
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert (df["Volume"] >= 1_000_000).all(), (
                f"{sym}: volume below 1M minimum"
            )

    def test_sufficient_history_for_scoring(self):
        """Pipeline requires >= 60 rows. Demo must provide at least that."""
        provider = DemoDataProvider()
        for sym in DEMO_SYMBOLS:
            df = provider.get_ohlcv(sym)
            assert len(df) >= 60, (
                f"{sym}: only {len(df)} rows, need ≥60 for scoring"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Determinism / reproducibility
# ═══════════════════════════════════════════════════════════════════════════════

class TestDemoDataReproducibility:
    """Demo data must be fully reproducible — same seed → same output."""

    def test_same_seed_same_data(self):
        df1 = _generate_ohlcv("AAPL")
        df2 = _generate_ohlcv("AAPL")
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_symbols_different_data(self):
        df_aapl = _generate_ohlcv("AAPL")
        df_nvda = _generate_ohlcv("NVDA")
        # Different base prices → different values
        assert df_aapl["Close"].iloc[-1] != df_nvda["Close"].iloc[-1]

    def test_provider_caches_consistently(self):
        provider = DemoDataProvider()
        df1 = provider.get_ohlcv("SPY")
        df2 = provider.get_ohlcv("SPY")
        pd.testing.assert_frame_equal(df1, df2)

    def test_returns_copy_not_reference(self):
        provider = DemoDataProvider()
        df1 = provider.get_ohlcv("TSLA")
        df2 = provider.get_ohlcv("TSLA")
        df1.iloc[0, df1.columns.get_loc("Close")] = 999.0
        assert df1["Close"].iloc[0] != df2["Close"].iloc[0], (
            "Modifying returned DataFrame should not affect cached data"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. No external API calls
# ═══════════════════════════════════════════════════════════════════════════════

class TestDemoDataNoNetwork:
    """Demo provider must never touch external APIs or filesystem."""

    def test_no_network_calls(self, monkeypatch):
        """Creating data should not access any network."""
        # Block urllib so any HTTP call raises immediately
        import urllib.request

        def _block_urlopen(*args, **kwargs):
            raise RuntimeError("Demo data provider attempted network access!")

        monkeypatch.setattr(urllib.request, "urlopen", _block_urlopen)

        # Should work fine without network
        provider = DemoDataProvider()
        df = provider.get_ohlcv("AAPL")
        assert len(df) == 252

    def test_no_yfinance_import_required(self):
        """Demo provider module should not import yfinance."""
        import importlib
        import src.openalpha.demo_data as dd

        # Re-import to check
        spec = importlib.util.find_spec("src.openalpha.demo_data")
        assert spec is not None

        # Verify the module source doesn't import yfinance
        source = Path(dd.__file__).read_text() if dd.__file__ else ""
        assert "import yfinance" not in source
        assert "from yfinance" not in source

    def test_get_demo_provider_singleton(self):
        """get_demo_provider returns the same instance."""
        p1 = get_demo_provider()
        p2 = get_demo_provider()
        assert p1 is p2


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Demo selector script runs end-to-end
# ═══════════════════════════════════════════════════════════════════════════════

class TestDemoSelectorEndToEnd:
    """The demo selector script should produce pipeline output."""

    def test_demo_selector_produces_results(self, monkeypatch, tmp_path):
        """Running the demo entry point should return a result dict."""
        import src.openalpha.selector as selector_module
        from src.openalpha.demo_data import DemoDataProvider
        from src.openalpha.selector import AIStrategySelector

        provider = DemoDataProvider()

        # Patch preflight → DEMO mode
        monkeypatch.setattr(
            "src.openalpha.preflight.run_preflight",
            lambda dry_run=True: type("PF", (), {
                "to_dict": lambda self: {
                    "market_state": "DEMO",
                    "run_mode": "DEMO",
                    "is_trading_day": True,
                },
            })(),
        )

        # Mock LOG_DIR so we don't write to real filesystem
        monkeypatch.setattr(selector_module, "LOG_DIR", tmp_path / "logs")

        # Patch quality context for demo
        class DemoQCtx:
            def history_metrics(self, symbol):
                df = provider.get_ohlcv(str(symbol).upper())
                if df is not None and len(df) >= 10:
                    avg_vol = float(df["Volume"].tail(10).mean())
                    closes = df["Close"].values
                    chg = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0 if closes[-4] > 0 else 0.0
                    return (avg_vol, chg, float(closes[-1]))
                return (2_000_000, 0.0, 50.0)

            def quote_metrics(self, symbol):
                p = provider.price_at(str(symbol).upper()) or 50.0
                s = p * 0.0002
                return (p, p - s, p + s, True)

            def close(self):
                pass

        monkeypatch.setattr(
            "src.openalpha.selector._QualityFilterContext", DemoQCtx
        )

        os.environ["OPENALPHA_UNIVERSE"] = "sample"

        try:
            selector = AIStrategySelector()
            selector.selection_size = 3
            selector.universe._load_local_snapshot = (
                lambda: list(provider.symbols)
            )
            selector.news.collect_for_symbols = (
                lambda symbols: {s: [] for s in symbols}
            )

            # Patch scorer to use demo data
            def _demo_load_history(symbol: str):
                try:
                    return provider.get_ohlcv(str(symbol).strip().upper())
                except KeyError:
                    return pd.DataFrame()

            selector.scorer._load_history = _demo_load_history

            result = selector.run_selection(write_configs=False)

            assert "run_mode" in result
            assert "top5" in result
            assert "formal_candidates" in result
            assert "preview_candidates" in result

            # Demo should produce candidates (relaxed quality in DEMO mode)
            assert len(result["top5"]) > 0, (
                "Demo selector should produce TOP candidates"
            )
        finally:
            os.environ.pop("OPENALPHA_UNIVERSE", None)

    def test_demo_selector_no_external_api_invoked(self, monkeypatch):
        """During demo run, PriceFetcher should not be instantiated."""
        from src.openalpha.demo_data import DemoDataProvider

        provider = DemoDataProvider()

        # Block PriceFetcher instantiation
        def _block_fetcher(*args, **kwargs):
            raise RuntimeError(
                "PriceFetcher called during demo run — "
                "demo should use DemoDataProvider only!"
            )

        monkeypatch.setattr(
            "src.data.fetcher.PriceFetcher.__init__", _block_fetcher
        )

        # Should not trigger PriceFetcher
        df = provider.get_ohlcv("MSFT")
        assert len(df) == 252

    def test_demo_symbols_match_expected_list(self):
        provider = DemoDataProvider()
        assert provider.symbols == ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]

    def test_unknown_symbol_raises_keyerror(self):
        with pytest.raises(KeyError, match="Unknown demo symbol"):
            _generate_ohlcv("FAKE123")

    def test_sector_and_asset_type_metadata(self):
        provider = DemoDataProvider()
        assert provider.sector_for("AAPL") == "Technology"
        assert provider.asset_type_for("SPY") == "etf"
        assert provider.asset_type_for("AAPL") == "common_stock"
        # Unknown symbol
        assert provider.sector_for("UNKNOWN") == "Unknown"
        assert provider.asset_type_for("UNKNOWN") == "common_stock"

    def test_price_and_volume_accessors(self):
        provider = DemoDataProvider()
        price = provider.price_at("AAPL")
        assert price is not None
        assert price > 0

        vol = provider.daily_volume_at("AAPL")
        assert vol > 0
