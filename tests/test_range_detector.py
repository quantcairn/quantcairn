import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.strategy.range_detector import RangeDetector, SignalType


@dataclass
class DummyCandle:
    high: float
    low: float
    close: float
    volume: int = 1_000


def test_manual_range_can_buy_near_support_without_volume_confidence_gate():
    detector = RangeDetector(
        ticker="TOP1",
        mode="manual",
        support_price=100.0,
        resistance_price=110.0,
        tolerance_pct=1.0,
        trend_enabled=False,
    )

    signal = detector.evaluate(100.4, has_position=False)

    assert signal.type == SignalType.BUY
    assert "near support" in signal.reason


def test_manual_range_can_buy_in_lower_half_of_range():
    detector = RangeDetector(
        ticker="TOP1",
        mode="manual",
        support_price=100.0,
        resistance_price=110.0,
        tolerance_pct=0.3,
        trend_enabled=False,
    )

    signal = detector.evaluate(104.0, has_position=False)

    assert signal.type == SignalType.BUY
    assert "in lower range" in signal.reason


def test_manual_range_does_not_buy_below_support():
    detector = RangeDetector(
        ticker="TOP1",
        mode="manual",
        support_price=100.0,
        resistance_price=110.0,
        tolerance_pct=1.0,
        trend_enabled=False,
    )

    signal = detector.evaluate(99.2, has_position=False)

    assert signal.type == SignalType.BUY
    assert "near support" in signal.reason or "in lower range" in signal.reason


def test_auto_range_seed_succeeds_with_valid_ohlcv():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        auto_lookback=10,
        trend_enabled=False,
        min_profit_per_trade=0.5,
        min_range_width_pct=0.5,
    )
    candles = [
        DummyCandle(high=103 + i * 0.2, low=99 + i * 0.1, close=101 + i * 0.1, volume=1_000 + i * 10)
        for i in range(10)
    ]

    seeded = detector.seed_from_ohlcv(candles)
    state = detector.get_range_state()

    assert seeded is True
    assert state.is_valid is True
    assert state.support < state.resistance


def test_auto_range_seed_rejects_flat_invalid_range():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        auto_lookback=10,
        trend_enabled=False,
    )
    candles = [
        DummyCandle(high=100.0, low=100.0, close=100.0, volume=1_000)
        for _ in range(10)
    ]

    logger = logging.getLogger("src.strategy.range_detector")
    original_disabled = logger.disabled
    logger.disabled = True
    try:
        seeded = detector.seed_from_ohlcv(candles)
        state = detector.get_range_state()
    finally:
        logger.disabled = original_disabled

    assert seeded is False
    assert state.is_valid is False


def test_auto_range_seed_rejects_too_narrow_range():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        auto_lookback=10,
        trend_enabled=False,
        min_profit_per_trade=1.0,
        min_range_width_pct=0.8,
    )
    candles = [
        DummyCandle(high=100.20, low=100.00, close=100.10, volume=1_000 + i * 10)
        for i in range(10)
    ]

    seeded = detector.seed_from_ohlcv(candles)

    assert seeded is False
    assert detector.get_range_state().is_valid is False


def test_auto_range_seed_allows_lower_priced_stock_with_dynamic_spread_floor():
    detector = RangeDetector(
        ticker="SOFI",
        mode="auto",
        auto_lookback=10,
        trend_enabled=False,
        min_profit_per_trade=1.0,
        min_range_width_pct=0.8,
    )
    candles = [
        DummyCandle(high=18.60, low=17.70, close=18.00 + i * 0.02, volume=10_000 + i * 50)
        for i in range(10)
    ]
    detector._calc_volume_weighted_range = lambda: (17.90, 18.62, 0.5, 0.5)

    seeded = detector.seed_from_ohlcv(candles)
    state = detector.get_range_state()

    assert seeded is True
    assert state.is_valid is True
    assert state.spread_dollars >= 0.35


def test_auto_range_seed_uses_bar_extrema_when_volume_profile_is_too_narrow():
    detector = RangeDetector(
        ticker="SOFI",
        mode="auto",
        auto_lookback=10,
        trend_enabled=False,
        min_profit_per_trade=1.0,
        min_range_width_pct=0.8,
    )
    candles = [
        DummyCandle(high=18.60, low=17.70, close=18.00 + i * 0.02, volume=10_000 + i * 50)
        for i in range(10)
    ]
    detector._calc_volume_weighted_range = lambda: (18.05, 18.28, 0.5, 0.5)

    seeded = detector.seed_from_ohlcv(candles)
    state = detector.get_range_state()

    assert seeded is True
    assert state.source == "bar_extrema_seed"
    assert state.support == 17.7
    assert state.resistance == 18.6


def test_auto_range_seed_uses_multi_day_extrema_when_recent_window_is_too_narrow():
    detector = RangeDetector(
        ticker="SOFI",
        mode="auto",
        auto_lookback=5,
        trend_enabled=False,
        min_profit_per_trade=0.72,
        min_range_width_pct=0.8,
    )
    candles = [
        DummyCandle(high=16.9, low=16.7, close=16.8, volume=10_000),
        DummyCandle(high=17.2, low=16.9, close=17.1, volume=10_100),
        DummyCandle(high=17.8, low=17.1, close=17.5, volume=10_200),
        DummyCandle(high=18.4, low=17.6, close=18.0, volume=10_300),
        DummyCandle(high=19.2, low=18.1, close=18.8, volume=10_400),
        DummyCandle(high=18.2, low=18.0, close=18.1, volume=10_500),
        DummyCandle(high=18.21, low=18.02, close=18.11, volume=10_600),
        DummyCandle(high=18.22, low=18.03, close=18.12, volume=10_700),
        DummyCandle(high=18.23, low=18.04, close=18.13, volume=10_800),
        DummyCandle(high=18.24, low=18.05, close=18.14, volume=10_900),
    ]
    detector._calc_volume_weighted_range = lambda: (18.05, 18.24, 0.5, 0.5)

    seeded = detector.seed_from_ohlcv(candles)
    state = detector.get_range_state()

    assert seeded is True
    assert state.source == "multi_day_extrema_seed"
    assert state.support == 16.7
    assert state.resistance == 19.2


def test_auto_range_buy_is_blocked_when_support_confidence_is_weak():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        tolerance_pct=0.5,
        trend_enabled=False,
    )
    detector._auto_support = 100.0
    detector._auto_resistance = 105.0
    detector._support_confidence = 0.099

    signal = detector.evaluate(100.2, has_position=False)

    assert signal.type == SignalType.HOLD
    assert "Weak support" in signal.reason


def test_auto_range_buy_is_blocked_by_downtrend():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        tolerance_pct=0.5,
        trend_enabled=True,
        trend_ma_period=5,
        trend_min_strength=0.1,
    )
    detector._auto_support = 100.0
    detector._auto_resistance = 105.0
    detector._support_confidence = 0.5
    for price in [105.0, 104.0, 103.0, 102.0, 100.2]:
        detector.feed_price(price)

    signal = detector.evaluate(100.2, has_position=False)

    assert signal.type == SignalType.TREND_BLOCK
    assert "daily_drop" in signal.reason or "downtrend" in signal.reason


def test_auto_range_buy_is_not_blocked_by_mild_downtrend():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        tolerance_pct=1.0,
        trend_enabled=True,
        trend_ma_period=5,
        trend_min_strength=0.1,
    )
    detector._auto_support = 100.0
    detector._auto_resistance = 105.0
    detector._support_confidence = 0.5
    for price in [101.5, 101.0, 100.7, 100.3, 100.2]:
        detector.feed_price(price)

    signal = detector.evaluate(100.2, has_position=False)

    assert signal.type == SignalType.BUY
    assert "in lower range" in signal.reason or "near support" in signal.reason


def test_quick_stop_triggers_for_open_position():
    detector = RangeDetector(
        ticker="TOP1",
        mode="manual",
        support_price=100.0,
        resistance_price=110.0,
        trend_enabled=False,
        quick_stop_pct=3.0,
    )
    detector.record_entry(100.0)

    signal = detector.evaluate(96.5, has_position=True)

    assert signal.type == SignalType.STOP_LOSS
    assert "QUICK STOP" in signal.reason


def test_needs_auto_refresh_respects_refresh_interval():
    detector = RangeDetector(
        ticker="TOP1",
        mode="auto",
        auto_refresh_minutes=15,
        range_lock_minutes=0,
        trend_enabled=False,
    )

    assert detector.needs_auto_refresh() is True

    detector._last_auto_refresh = datetime.now() - timedelta(minutes=5)
    assert detector.needs_auto_refresh() is False

    detector._last_auto_refresh = datetime.now() - timedelta(minutes=16)
    assert detector.needs_auto_refresh() is True
