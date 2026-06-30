from src.strategy.range_detector import RangeDetector, SignalType


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

    assert signal.type == SignalType.HOLD
    assert "dist to support" in signal.reason
