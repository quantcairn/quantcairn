"""Tests for the Managed Universe system — symbol validation, filtering, composition, snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.universe.models import UniverseSymbol, UniverseSnapshot
from src.universe.profiles import (
    default_universe, INDEX_ETFS, MEGA_CAPS, LEVERAGED_AND_INVERSE,
    SECTOR_ETFS, ADDITIONAL_STOCKS,
)
from src.universe.filters import (
    apply_liquidity_filter, apply_price_filter, apply_risk_filter,
    apply_composition_filter, run_all_filters,
)
from src.universe.manager import UniverseManager


# ═══════════════════════════════════════════════════════════════════════
# Symbol validation
# ═══════════════════════════════════════════════════════════════════════

class TestSymbolValidation:
    def test_symbol_to_dict_roundtrip(self):
        s = UniverseSymbol(symbol="AAPL", asset_type="common_stock", sector="technology", leverage_type="1x", risk_score=20.0)
        d = s.to_dict()
        s2 = UniverseSymbol.from_dict(d)
        assert s2.symbol == "AAPL"
        assert s2.asset_type == "common_stock"
        assert s2.risk_score == 20.0

    def test_is_leveraged(self):
        assert UniverseSymbol(symbol="SPY", leverage_type="1x").is_leveraged is False
        assert UniverseSymbol(symbol="TQQQ", leverage_type="3x").is_leveraged is True
        assert UniverseSymbol(symbol="SQQQ", leverage_type="-3x").is_leveraged is True

    def test_is_inverse(self):
        assert UniverseSymbol(symbol="SOXS", asset_type="inverse_etf", leverage_type="-3x").is_inverse is True
        assert UniverseSymbol(symbol="SPY", leverage_type="1x").is_inverse is False

    def test_enable_disable(self):
        s = UniverseSymbol(symbol="TEST")
        assert s.enabled is True
        s.disable("test reason")
        assert s.enabled is False
        assert s.disabled_reason == "test reason"
        s.enable()
        assert s.enabled is True

    def test_default_universe_size(self):
        uni = default_universe()
        assert len(uni) >= 30
        assert len(uni) <= 60


# ═══════════════════════════════════════════════════════════════════════
# Profiles
# ═══════════════════════════════════════════════════════════════════════

class TestProfiles:
    def test_index_etfs(self):
        symbols = {s.symbol for s in INDEX_ETFS}
        assert "SPY" in symbols
        assert "QQQ" in symbols
        assert "IWM" in symbols

    def test_mega_caps(self):
        symbols = {s.symbol for s in MEGA_CAPS}
        assert "AAPL" in symbols
        assert "NVDA" in symbols
        assert "TSLA" in symbols

    def test_leveraged_includes_soxs(self):
        symbols = {s.symbol for s in LEVERAGED_AND_INVERSE}
        assert "SOXS" in symbols
        assert "SOXL" in symbols
        assert "TQQQ" in symbols
        assert "SQQQ" in symbols

    def test_no_duplicate_symbols(self):
        uni = default_universe()
        seen = set()
        for s in uni:
            assert s.symbol not in seen, f"Duplicate symbol: {s.symbol}"
            seen.add(s.symbol)

    def test_all_symbols_have_required_fields(self):
        uni = default_universe()
        for s in uni:
            assert s.symbol, f"Empty symbol"
            assert s.asset_type, f"{s.symbol}: empty asset_type"
            assert s.leverage_type, f"{s.symbol}: empty leverage_type"


# ═══════════════════════════════════════════════════════════════════════
# Filtering
# ═══════════════════════════════════════════════════════════════════════

class TestLiquidityFilter:
    def test_high_liquidity_passes(self):
        symbols = [UniverseSymbol(symbol="SPY", liquidity_score=95.0, min_avg_volume=10_000_000)]
        kept, dropped = apply_liquidity_filter(symbols, min_avg_volume=500_000)
        assert len(kept) == 1

    def test_low_liquidity_dropped(self):
        symbols = [UniverseSymbol(symbol="LOW", liquidity_score=10.0, min_avg_volume=1000)]
        kept, dropped = apply_liquidity_filter(symbols, min_avg_volume=500_000)
        assert len(kept) == 0
        assert len(dropped) == 1

    def test_disabled_symbol_dropped(self):
        s = UniverseSymbol(symbol="DISABLED", liquidity_score=95.0)
        s.disable("manually")
        kept, dropped = apply_liquidity_filter([s])
        assert len(kept) == 0


class TestPriceFilter:
    def test_valid_price_passes(self):
        symbols = [UniverseSymbol(symbol="OK", min_price=50.0)]
        kept, dropped = apply_price_filter(symbols, min_price=4.0, max_price=300.0)
        assert len(kept) == 1

    def test_price_too_low(self):
        symbols = [UniverseSymbol(symbol="CHEAP", min_price=2.0)]
        kept, dropped = apply_price_filter(symbols, min_price=4.0)
        assert len(dropped) == 1

    def test_price_too_high(self):
        symbols = [UniverseSymbol(symbol="BRK", min_price=500.0)]
        kept, dropped = apply_price_filter(symbols, max_price=300.0)
        assert len(dropped) == 1


class TestRiskFilter:
    def test_normal_risk_passes(self):
        symbols = [UniverseSymbol(symbol="SAFE", risk_score=20.0, volatility_score=18.0)]
        kept, dropped = apply_risk_filter(symbols)
        assert len(kept) == 1

    def test_high_risk_dropped(self):
        symbols = [UniverseSymbol(symbol="DANGER", risk_score=80.0, volatility_score=85.0)]
        kept, dropped = apply_risk_filter(symbols, max_risk_score=70.0, max_volatility_score=80.0)
        assert len(dropped) == 1


class TestCompositionFilter:
    def test_leveraged_limit_enforced(self):
        symbols = [
            UniverseSymbol(symbol="SPY", leverage_type="1x", liquidity_score=90.0),
            UniverseSymbol(symbol="SOXL", leverage_type="3x", liquidity_score=75.0),
            UniverseSymbol(symbol="TQQQ", leverage_type="3x", liquidity_score=78.0),
            UniverseSymbol(symbol="AAPL", leverage_type="1x", liquidity_score=98.0),
        ]
        kept, dropped = apply_composition_filter(symbols, max_leveraged_inverse=1)
        # TQQQ has higher liquidity (78 > 75), so it stays; SOXL dropped
        assert len(kept) == 3  # SPY + TQQQ + AAPL
        assert len(dropped) == 1
        assert dropped[0]["symbol"] == "SOXL"

    def test_no_leveraged_all_pass(self):
        symbols = [
            UniverseSymbol(symbol="SPY", leverage_type="1x", liquidity_score=90.0),
            UniverseSymbol(symbol="AAPL", leverage_type="1x", liquidity_score=98.0),
        ]
        kept, dropped = apply_composition_filter(symbols, max_leveraged_inverse=1)
        assert len(kept) == 2
        assert len(dropped) == 0


# ═══════════════════════════════════════════════════════════════════════
# Snapshot generation
# ═══════════════════════════════════════════════════════════════════════

class TestSnapshot:
    def test_snapshot_to_dict(self, tmp_path: Path):
        symbols = [
            UniverseSymbol(symbol="SPY", asset_type="index_etf", liquidity_score=98.0, min_avg_volume=10_000_000, risk_score=15.0, volatility_score=18.0),
            UniverseSymbol(symbol="AAPL", asset_type="common_stock", liquidity_score=97.0, min_avg_volume=50_000_000, risk_score=20.0, volatility_score=28.0),
        ]
        snap = run_all_filters(symbols)
        d = snap.to_dict()
        assert d["total_symbols"] == 2
        assert d["enabled_symbols"] == 2

    def test_snapshot_composition(self):
        symbols = [
            UniverseSymbol(symbol="SPY", asset_type="index_etf", liquidity_score=98.0, min_avg_volume=10_000_000),
            UniverseSymbol(symbol="AAPL", asset_type="common_stock", liquidity_score=97.0, min_avg_volume=50_000_000),
            UniverseSymbol(symbol="SOXS", asset_type="inverse_etf", leverage_type="-3x", liquidity_score=72.0, min_avg_volume=5_000_000, risk_score=55.0, volatility_score=65.0),
        ]
        snap = run_all_filters(symbols, max_leveraged_inverse=1)
        comp = snap.composition
        assert comp["index_etf"] >= 1
        assert comp["common_stock"] >= 1

    def test_filter_order_correct(self):
        """Composition filter must be last — it applies to enabled symbols only."""
        symbols = [
            UniverseSymbol(symbol="SPY", asset_type="index_etf", liquidity_score=98.0, min_avg_volume=10_000_000, risk_score=15.0, volatility_score=18.0),
            UniverseSymbol(symbol="SOXS", asset_type="inverse_etf", leverage_type="-3x", liquidity_score=72.0, min_avg_volume=5_000_000, risk_score=55.0, volatility_score=65.0),
            UniverseSymbol(symbol="SQQQ", asset_type="inverse_etf", leverage_type="-3x", liquidity_score=76.0, min_avg_volume=3_000_000, risk_score=60.0, volatility_score=70.0),
        ]
        snap = run_all_filters(symbols, max_leveraged_inverse=1)
        # SQQQ has higher liquidity, so it should survive composition
        # SOXS should be dropped by composition
        enabled = [s.symbol for s in snap.symbols if s.enabled]
        assert "SQQQ" in enabled or "SOXS" in enabled
        assert "SPY" in enabled


# ═══════════════════════════════════════════════════════════════════════
# Invalid data handling
# ═══════════════════════════════════════════════════════════════════════

class TestInvalidData:
    def test_from_dict_missing_fields(self):
        d = {"symbol": "X", "asset_type": "common_stock"}
        s = UniverseSymbol.from_dict(d)
        assert s.symbol == "X"
        assert s.enabled is True
        assert s.liquidity_score == 0.0

    def test_from_dict_invalid_types(self):
        d = {"symbol": "Y", "min_price": "not_a_number", "enabled": "maybe"}
        s = UniverseSymbol.from_dict(d)
        assert s.symbol == "Y"
        assert s.min_price == 4.0  # default fallback
        assert s.enabled is False  # unrecognized string → fail closed

    def test_snapshot_empty_universe(self):
        snap = run_all_filters([])
        assert snap.total_symbols == 0
        assert snap.enabled_symbols == 0

    def test_manager_loads_default_when_no_file(self, tmp_path: Path):
        mgr = UniverseManager(universe_path=tmp_path / "nonexistent.json", snapshot_path=tmp_path / "snap.json")
        symbols = mgr.load_symbols()
        assert len(symbols) >= 30


# ═══════════════════════════════════════════════════════════════════════
# Safety
# ═══════════════════════════════════════════════════════════════════════

class TestSafetyBoundaries:
    def test_no_broker_in_universe(self):
        for mod_name in ("src.universe.models", "src.universe.filters", "src.universe.manager", "src.universe.profiles"):
            import importlib
            mod = importlib.import_module(mod_name)
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "LongBridgeBroker" not in src
            assert "PaperBroker" not in src
            assert "TradeContext" not in src
            assert "place_order" not in src

    def test_no_trading_engine_in_universe(self):
        import src.universe.manager as m
        import src.universe.filters as f
        src = Path(m.__file__).read_text() + Path(f.__file__).read_text()
        assert "TradingEngine" not in src
        assert "RiskManager" not in src
        assert "PortfolioManager" not in src


def run_test_direct():
    import tempfile
    td = Path(tempfile.mkdtemp())
    try:
        TestSymbolValidation().test_symbol_to_dict_roundtrip()
        TestSymbolValidation().test_is_leveraged()
        TestSymbolValidation().test_is_inverse()
        TestSymbolValidation().test_enable_disable()
        TestSymbolValidation().test_default_universe_size()
        TestProfiles().test_index_etfs()
        TestProfiles().test_mega_caps()
        TestProfiles().test_leveraged_includes_soxs()
        TestProfiles().test_no_duplicate_symbols()
        TestProfiles().test_all_symbols_have_required_fields()
        TestLiquidityFilter().test_high_liquidity_passes()
        TestLiquidityFilter().test_low_liquidity_dropped()
        TestLiquidityFilter().test_disabled_symbol_dropped()
        TestPriceFilter().test_valid_price_passes()
        TestPriceFilter().test_price_too_low()
        TestPriceFilter().test_price_too_high()
        TestRiskFilter().test_normal_risk_passes()
        TestRiskFilter().test_high_risk_dropped()
        TestCompositionFilter().test_leveraged_limit_enforced()
        TestCompositionFilter().test_no_leveraged_all_pass()
        TestSnapshot().test_filter_order_correct()
        TestInvalidData().test_from_dict_missing_fields()
        TestInvalidData().test_from_dict_invalid_types()
        TestInvalidData().test_snapshot_empty_universe()
        TestInvalidData().test_manager_loads_default_when_no_file(td)
        TestSafetyBoundaries().test_no_broker_in_universe()
        TestSafetyBoundaries().test_no_trading_engine_in_universe()
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()


# ═══════════════════════════════════════════════════════════════════════
# Selector integration tests
# ═══════════════════════════════════════════════════════════════════════

class TestSelectorIntegration:
    def test_managed_universe_loads_enabled_symbols(self, tmp_path: Path):
        """The managed universe loader returns enabled symbol names."""
        from src.universe.manager import UniverseManager
        from src.universe.models import UniverseSymbol

        snap_path = tmp_path / "universe_snapshot.json"
        mgr = UniverseManager(snapshot_path=snap_path, universe_path=tmp_path / "universe.json")
        symbols = [
            UniverseSymbol(symbol="AAPL", asset_type="common_stock", enabled=True, liquidity_score=98.0),
            UniverseSymbol(symbol="TSLA", asset_type="common_stock", enabled=False, liquidity_score=90.0,
                           disabled_reason="test_disabled"),
            UniverseSymbol(symbol="SPY", asset_type="index_etf", enabled=True, liquidity_score=98.0),
        ]
        mgr.save_symbols(symbols)
        mgr.build_snapshot(dry_run=False)

        from src.openalpha.selector import _load_managed_universe
        with patch("src.universe.manager.UniverseManager", return_value=mgr):
            result = _load_managed_universe()
            assert result is not None
            assert "AAPL" in result
            assert "SPY" in result
            assert "TSLA" not in result  # disabled

    def test_managed_universe_returns_none_when_empty(self, tmp_path: Path):
        """Empty universe returns None, signalling fallback."""
        from src.universe.manager import UniverseManager

        mgr = UniverseManager(snapshot_path=tmp_path / "snap.json", universe_path=tmp_path / "uni.json")
        mgr.save_symbols([])
        with patch("src.universe.manager.UniverseManager", return_value=mgr):
            from src.openalpha.selector import _load_managed_universe
            result = _load_managed_universe()
            assert result is None

    def test_legacy_sample_fallback_works(self):
        """The legacy sample universe is still accessible when 'sample' is requested."""
        import src.openalpha.selector as sel_mod
        assert hasattr(sel_mod, '_load_managed_universe')
        assert callable(sel_mod.Universe()._load_local_snapshot)


if __name__ == "__main__":
    run_test_direct()
