"""Smoke test for the QuantCairn public API.

Verifies that a user running ``pip install -e .`` can:
  1. Import quantcairn and access __version__
  2. Import every name in quantcairn.__all__
  3. Instantiate DemoDataProvider and get deterministic data
  4. Run a minimal demo path without network / API keys / broker

All tests are deterministic and require zero external dependencies.
"""

from __future__ import annotations

import pytest


class TestPackageImport:
    """Basic package import and metadata."""

    def test_import_quantcairn(self):
        import quantcairn
        assert quantcairn is not None

    def test_version_is_string(self):
        import quantcairn
        assert isinstance(quantcairn.__version__, str)
        assert quantcairn.__version__.count(".") >= 2

    def test_all_names_are_importable(self):
        import quantcairn
        for name in quantcairn.__all__:
            obj = getattr(quantcairn, name, None)
            assert obj is not None, f"__all__ entry '{name}' is None"


class TestDemoProviderSmoke:
    """DemoDataProvider works without network or API keys."""

    def test_provider_instantiation(self):
        from quantcairn import DemoDataProvider
        provider = DemoDataProvider()
        assert provider is not None

    def test_provider_symbols(self):
        from quantcairn import DemoDataProvider
        provider = DemoDataProvider()
        assert provider.symbols == ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]

    def test_get_ohlcv_returns_data(self):
        from quantcairn import DemoDataProvider
        provider = DemoDataProvider()
        df = provider.get_ohlcv("AAPL")
        assert len(df) == 252
        for col in ("Open", "High", "Low", "Close", "Volume"):
            assert col in df.columns

    def test_deterministic_output(self):
        from quantcairn import DemoDataProvider
        p1 = DemoDataProvider()
        p2 = DemoDataProvider()
        import pandas as pd
        pd.testing.assert_frame_equal(p1.get_ohlcv("NVDA"), p2.get_ohlcv("NVDA"))

    def test_price_at_returns_positive_float(self):
        from quantcairn import DemoDataProvider
        provider = DemoDataProvider()
        price = provider.price_at("SPY")
        assert isinstance(price, float)
        assert price > 0


class TestSubModuleImports:
    """Every public sub-module must be importable."""

    @pytest.mark.parametrize("module_name", [
        "quantcairn.selector",
        "quantcairn.funnel_tracker",
        "quantcairn.demo_data",
        "quantcairn.preflight",
        "quantcairn.data_diagnostics",
        "quantcairn.candidate_ranking",
        "quantcairn.universe_filter",
        "quantcairn.settings",
    ])
    def test_sub_module_import(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        assert mod is not None


class TestNoNetworkCalls:
    """Public API imports must not trigger network access."""

    def test_demo_provider_no_urllib(self, monkeypatch):
        import urllib.request

        def _block(*args, **kwargs):
            raise RuntimeError("Network access attempted!")

        monkeypatch.setattr(urllib.request, "urlopen", _block)
        from quantcairn import DemoDataProvider
        provider = DemoDataProvider()
        df = provider.get_ohlcv("MSFT")
        assert len(df) == 252

    def test_import_does_not_import_broker(self, monkeypatch):
        """quantcairn import must not pull in broker modules."""
        import builtins
        _orig = builtins.__import__

        def _guard(name, *args, **kwargs):
            if "longbridge" in str(name).lower() or "broker" in str(name).lower():
                raise ImportError(f"Broker import detected: {name}")
            return _orig(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        import quantcairn  # noqa: F401 — should not trigger broker import
        assert quantcairn.__version__ is not None
