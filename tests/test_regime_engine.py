"""Tests for the Market Regime Engine — advisory only, never touches trading."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.regime.models import MarketSnapshot, RegimeState, RegimeHistory
from src.regime.detector import _compute_rsi, _compute_sma, _compute_atr_pct
from src.regime.classifier import classify


def _snap(**kw) -> dict:
    defaults = {"price": 100.0, "ma20": 98.0, "ma50": 95.0, "ma200": 90.0,
                "rsi": 60.0, "atr_pct": 1.2, "return_5d_pct": 2.0,
                "return_20d_pct": 5.0, "volume_sma20_ratio": 1.1,
                "price_vs_ma20": 100.0 / 98.0,
                "price_vs_ma50": 100.0 / 95.0,
                "price_vs_ma200": 100.0 / 90.0}
    defaults.update(kw)
    # Recompute price ratios
    if "price" in kw or "ma20" in kw:
        defaults["price_vs_ma20"] = round(defaults["price"] / max(defaults["ma20"], 0.01), 4)
    if "price" in kw or "ma50" in kw:
        defaults["price_vs_ma50"] = round(defaults["price"] / max(defaults["ma50"], 0.01), 4)
    if "price" in kw or "ma200" in kw:
        defaults["price_vs_ma200"] = round(defaults["price"] / max(defaults["ma200"], 0.01), 4)
    return defaults


def _state(**kw) -> RegimeState:
    defaults: dict = {"vix": 15.0, "vix_change_pct": -2.0,
        "market_return_5d": 2.5, "market_return_20d": 6.0,
        "indices": {"SPY": _snap(), "QQQ": _snap(price=105, ma20=102), "IWM": _snap(price=98, ma20=97)}}
    defaults.update(kw)
    s = RegimeState(**{k: v for k, v in defaults.items() if hasattr(RegimeState, k)})
    s.vix = defaults["vix"]
    s.vix_change_pct = defaults["vix_change_pct"]
    s.market_return_5d = defaults["market_return_5d"]
    s.market_return_20d = defaults["market_return_20d"]
    s.indices = defaults["indices"]
    return s


# ═══════════════════════════════════════════════════════════════════════
# Indicator computations
# ═══════════════════════════════════════════════════════════════════════

class TestIndicators:
    def test_rsi_uptrend(self):
        prices = list(range(100, 120))  # steady uptrend
        rsi = _compute_rsi([float(p) for p in prices])
        assert rsi > 50

    def test_rsi_downtrend(self):
        prices = list(range(120, 100, -1))
        rsi = _compute_rsi([float(p) for p in prices])
        assert rsi < 50

    def test_rsi_insufficient_data(self):
        assert _compute_rsi([100.0]) == 50.0

    def test_sma(self):
        assert _compute_sma([10.0, 20.0, 30.0], 3) == 20.0

    def test_atr_pct(self):
        closes = [100.0 + i * 0.1 for i in range(20)]
        highs = [c * 1.02 for c in closes]
        lows = [c * 0.98 for c in closes]
        atr = _compute_atr_pct(highs, lows, closes)
        assert atr > 0


# ═══════════════════════════════════════════════════════════════════════
# Regime classification
# ═══════════════════════════════════════════════════════════════════════

class TestBullDetection:
    def test_strong_bull(self):
        state = _state()
        classify(state)
        assert state.regime == "BULL"
        assert state.bull_score > state.bear_score
        assert state.confidence > 0.4

    def test_bull_above_all_mas(self):
        state = _state(indices={
            "SPY": _snap(price=110, ma20=100, ma50=95, ma200=88,
                         return_5d_pct=4.0, return_20d_pct=8.0, rsi=65),
            "QQQ": _snap(price=112, ma20=100, ma50=95, ma200=88,
                         return_5d_pct=5.0, return_20d_pct=9.0, rsi=68)},
                       market_return_5d=4.5, market_return_20d=8.5, vix=14)
        classify(state)
        assert state.bull_score >= 40

    def test_bull_signals_present(self):
        state = _state()
        classify(state)
        assert len(state.signals) > 0


class TestSidewaysDetection:
    def test_sideways(self):
        state = _state(
            indices={"SPY": _snap(price=100, ma20=99, rsi=50, return_5d_pct=0.5, return_20d_pct=1.0)},
            market_return_5d=0.5, market_return_20d=1.0, vix=18)
        classify(state)
        # Sideways should be competitive — scores near each other
        assert state.sideways_score > 0

    def test_near_ma20_boosts_sideways(self):
        state = _state(indices={"SPY": _snap(price=100, ma20=99)}, vix=18)
        s_far = _state(indices={"SPY": _snap(price=100, ma20=80)}, vix=18)
        classify(state)
        classify(s_far)
        assert state.sideways_score >= s_far.sideways_score


class TestBearDetection:
    def test_strong_bear(self):
        state = _state(
            indices={"SPY": _snap(price=90, ma20=95, ma50=98, ma200=100,
                                    return_5d_pct=-3.0, return_20d_pct=-6.0, rsi=35)},
            market_return_5d=-3.0, market_return_20d=-6.0, vix=28)
        classify(state)
        assert state.bear_score > state.bull_score

    def test_bear_below_ma200_signals(self):
        state = _state(
            indices={"SPY": _snap(price=88, ma20=92, ma50=96, ma200=100,
                                    rsi=35, return_5d_pct=-2.0, return_20d_pct=-4.0)},
            market_return_5d=-2.0, market_return_20d=-4.0, vix=26)
        classify(state)
        assert state.bear_score > 25


class TestRiskOffDetection:
    def test_risk_off_vix_spike(self):
        state = _state(
            indices={"SPY": _snap(price=85, ma20=95, ma50=100, ma200=105,
                                    atr_pct=3.5, return_5d_pct=-5.0, return_20d_pct=-8.0,
                                    rsi=25)},
            market_return_5d=-5.0, market_return_20d=-8.0,
            vix=35, vix_change_pct=15.0)
        classify(state)
        assert state.risk_off_score > 25

    def test_risk_off_below_ma200_boosts(self):
        state = _state(
            indices={"SPY": _snap(price=82, ma20=95, ma50=100, ma200=105,
                                    atr_pct=4.0, return_5d_pct=-6.0, return_20d_pct=-10.0),
                     "QQQ": _snap(price=75, ma20=90, ma50=95, ma200=100,
                                    atr_pct=4.5, return_5d_pct=-7.0, return_20d_pct=-12.0)},
            market_return_5d=-6.0, market_return_20d=-10.0,
            vix=38, vix_change_pct=20.0)
        classify(state)
        assert state.risk_off_score > state.bull_score


class TestMissingData:
    def test_no_indices(self):
        state = RegimeState()
        state.indices = {}
        classify(state)
        # No index data: all scores should be relatively flat
        assert state.bull_score == 0.0
        assert state.bear_score == 0.0

    def test_no_indices_valid_regime(self):
        state = RegimeState()
        state.indices = {}
        classify(state)
        assert state.regime in {"BULL", "SIDEWAYS", "BEAR", "RISK_OFF"}

    def test_single_index(self):
        state = _state(indices={"SPY": _snap()})
        classify(state)
        assert state.regime in {"BULL", "SIDEWAYS", "BEAR", "RISK_OFF"}


class TestConfidence:
    def test_confidence_with_good_data(self):
        state = _state()
        classify(state)
        assert 0.0 <= state.confidence <= 1.0

    def test_confidence_lower_with_high_vix(self):
        state_lo = _state(vix=15)
        state_hi = _state(vix=35)
        classify(state_lo)
        classify(state_hi)
        assert state_lo.confidence > state_hi.confidence

    def test_all_scores_non_negative(self):
        state = _state()
        classify(state)
        for s in (state.bull_score, state.sideways_score, state.bear_score, state.risk_off_score):
            assert s >= 0


class TestInvalidRegimeProtection:
    def test_regime_is_valid_type(self):
        state = _state()
        classify(state)
        assert state.regime in {"BULL", "SIDEWAYS", "BEAR", "RISK_OFF"}

    def test_garbage_indices_no_crash(self):
        state = RegimeState()
        state.indices = {"JUNK": {"price": None, "rsi": "nope"}}
        classify(state)
        assert state.regime in {"BULL", "SIDEWAYS", "BEAR", "RISK_OFF"}


# ═══════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════

class TestModels:
    def test_regime_state_to_dict(self):
        state = RegimeState(regime="BULL", confidence=0.85, bull_score=75.0,
                            vix=16.5, signals=["bull_above_ma20:3/3", "low_vix_bullish"])
        d = state.to_dict()
        assert d["regime"] == "BULL"
        assert d["vix"] == 16.5
        assert len(d["signals"]) == 2

    def test_market_snapshot_ratios(self):
        snap = MarketSnapshot(symbol="SPY", price=105, ma20=100, ma50=95, ma200=90)
        assert snap.price_vs_ma20 == 1.05
        assert snap.price_vs_ma50 > 1.0
        assert snap.price_vs_ma200 > 1.0

    def test_regime_history_trimming(self):
        h = RegimeHistory()
        for i in range(400):
            h.add(RegimeState(regime="SIDEWAYS"))
        assert len(h.records) == 365

    def test_regime_history_counts(self):
        h = RegimeHistory()
        h.add(RegimeState(regime="BULL"))
        h.add(RegimeState(regime="BULL"))
        h.add(RegimeState(regime="BEAR"))
        counts = h.regime_counts()
        assert counts["BULL"] == 2
        assert counts["BEAR"] == 1


# ═══════════════════════════════════════════════════════════════════════
# Safety
# ═══════════════════════════════════════════════════════════════════════

class TestSafetyBoundaries:
    def test_no_broker_in_regime_module(self):
        for mod_name in ("src.regime.detector", "src.regime.classifier", "src.regime.models"):
            import importlib
            mod = importlib.import_module(mod_name)
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "LongBridgeBroker" not in src
            assert "PaperBroker" not in src
            assert "TradeContext" not in src
            assert "place_order" not in src

    def test_no_trading_engine_in_regime(self):
        import src.regime.detector as d
        import src.regime.classifier as c
        src = Path(d.__file__).read_text() + Path(c.__file__).read_text()
        assert "TradingEngine" not in src
        assert "RiskManager" not in src
        assert "PortfolioManager" not in src


def run_test_direct():
    # Indicators
    TestIndicators().test_rsi_uptrend()
    TestIndicators().test_rsi_downtrend()
    TestIndicators().test_rsi_insufficient_data()
    TestIndicators().test_sma()
    TestIndicators().test_atr_pct()
    # Bull
    TestBullDetection().test_strong_bull()
    TestBullDetection().test_bull_above_all_mas()
    TestBullDetection().test_bull_signals_present()
    # Sideways
    TestSidewaysDetection().test_sideways()
    TestSidewaysDetection().test_near_ma20_boosts_sideways()
    # Bear
    TestBearDetection().test_strong_bear()
    TestBearDetection().test_bear_below_ma200_signals()
    # Risk Off
    TestRiskOffDetection().test_risk_off_vix_spike()
    TestRiskOffDetection().test_risk_off_below_ma200_boosts()
    # Missing data
    TestMissingData().test_no_indices()
    TestMissingData().test_no_indices_valid_regime()
    TestMissingData().test_single_index()
    # Confidence
    TestConfidence().test_confidence_with_good_data()
    TestConfidence().test_confidence_lower_with_high_vix()
    TestConfidence().test_all_scores_non_negative()
    # Invalid regime
    TestInvalidRegimeProtection().test_regime_is_valid_type()
    TestInvalidRegimeProtection().test_garbage_indices_no_crash()
    # Models
    TestModels().test_regime_state_to_dict()
    TestModels().test_market_snapshot_ratios()
    TestModels().test_regime_history_trimming()
    TestModels().test_regime_history_counts()
    # Safety
    TestSafetyBoundaries().test_no_broker_in_regime_module()
    TestSafetyBoundaries().test_no_trading_engine_in_regime()
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()
