from __future__ import annotations

import math

from src.market_regime.detector import detect_market_regime
from src.strategy.range_strategy import RangeStrategy
from src.strategy.trend_strategy import TrendStrategy
from src.strategy_router.router import NoTradeStrategy, select_strategy


class FakeSeries(list):
    def tolist(self):
        return list(self)


class FakeFrame:
    def __init__(self, rows):
        self.rows = list(rows)
        self.columns = list(self.rows[0].keys()) if self.rows else []
        self.attrs = {}
        self._data = {column: FakeSeries([row[column] for row in self.rows]) for column in self.columns}

    def __getitem__(self, key):
        return self._data[key]


def _frame(rows):
    return FakeFrame(rows)


def _range_setup_df():
    rows = []
    for idx in range(60):
        close = 120.0 - idx * 0.5
        rows.append(
            {
                "open": close + 0.3,
                "high": close + 10.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_200_000,
            }
        )
    return _frame(rows)


def _trend_setup_df():
    rows = []
    for idx in range(60):
        close = 50.0 + idx * 1.4 + (idx % 4) * 0.3
        rows.append(
            {
                "open": close - 0.2,
                "high": close + 1.5,
                "low": close - 1.5,
                "close": close,
                "volume": 1_000_000 + idx * 50_000,
            }
        )
    return _frame(rows)


def _event_setup_df():
    df = _range_setup_df()
    df.attrs["news_score"] = 95
    return df


def test_strategy_router_maps_range_to_range_strategy():
    df = _range_setup_df()
    routed = select_strategy(df)
    assert routed["regime"] == "RANGE"
    assert isinstance(routed["strategy"], RangeStrategy)


def test_strategy_router_maps_trend_to_trend_strategy():
    df = _trend_setup_df()
    routed = select_strategy(df)
    assert routed["regime"] == "TREND"
    assert isinstance(routed["strategy"], TrendStrategy)


def test_strategy_router_maps_event_to_no_trade_strategy():
    df = _event_setup_df()
    routed = select_strategy(df)
    assert routed["regime"] == "EVENT"
    assert isinstance(routed["strategy"], NoTradeStrategy)


def test_range_strategy_support_resistance_and_entry():
    df = _range_setup_df()
    strategy = RangeStrategy()
    levels = strategy.calculate_levels(df)
    assert math.isclose(levels["support"], 89.5, rel_tol=1e-9)
    assert math.isclose(levels["resistance"], 110.0, rel_tol=1e-9)

    signal = strategy.generate_signal(df)
    assert signal["action"] == "buy"
    assert signal["side"] == "long"
    assert math.isclose(signal["target_price"], (89.5 + 110.0) / 2.0, rel_tol=1e-9)
    assert math.isclose(signal["stop_loss"], 1.2 * signal["atr"], rel_tol=1e-9)


def test_trend_strategy_breakout_and_trailing_stop():
    df = _trend_setup_df()
    strategy = TrendStrategy()
    signal = strategy.generate_signal(df)
    assert signal["action"] == "buy"
    assert signal["side"] == "long"
    assert signal["entry_type"] in {"breakout", "pullback"}
    assert signal["atr_trailing_stop"] < signal["price"]
