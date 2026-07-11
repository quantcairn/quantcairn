from __future__ import annotations

from src.strategy.trade_cost import TradeCostEstimator


def test_positive_edge_trade_is_allowed():
    estimator = TradeCostEstimator()
    result = estimator.estimate(
        entry_price=10.0,
        exit_price=10.6,
        quantity=10,
        commission_per_share=0.005,
        platform_fee_per_trade=1.0,
        spread_pct=0.01,
        slippage_pct=0.005,
        available_cash=200.0,
        minimum_net_profit=0.5,
        max_spread_profit_ratio=0.5,
    )

    assert result["allowed"] is True
    assert result["reject_reason"] == ""
    assert result["expected_gross_profit"] > 0
    assert result["expected_net_profit"] > 0


def test_low_edge_trade_is_blocked():
    estimator = TradeCostEstimator()
    result = estimator.estimate(
        entry_price=10.0,
        exit_price=10.03,
        quantity=10,
        commission_per_share=0.05,
        platform_fee_per_trade=1.0,
        spread_pct=0.001,
        slippage_pct=0.001,
        available_cash=200.0,
        minimum_net_profit=0.5,
        max_spread_profit_ratio=0.5,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] in {"expected_profit_too_low", "cost_exceeds_edge"}


def test_wide_spread_is_blocked():
    estimator = TradeCostEstimator()
    result = estimator.estimate(
        entry_price=10.0,
        exit_price=10.6,
        quantity=10,
        commission_per_share=0.005,
        platform_fee_per_trade=0.0,
        spread_pct=0.25,
        slippage_pct=0.005,
        available_cash=200.0,
        minimum_net_profit=0.5,
        max_spread_profit_ratio=0.2,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "spread_too_wide"


def test_costs_exceed_edge_is_blocked():
    estimator = TradeCostEstimator()
    result = estimator.estimate(
        entry_price=10.0,
        exit_price=10.5,
        quantity=10,
        commission_per_share=0.15,
        platform_fee_per_trade=1.0,
        spread_pct=0.01,
        slippage_pct=0.01,
        available_cash=200.0,
        minimum_net_profit=0.5,
        max_spread_profit_ratio=0.5,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "cost_exceeds_edge"


def test_insufficient_cash_is_blocked_without_margin_usage():
    estimator = TradeCostEstimator()
    result = estimator.estimate(
        entry_price=10.0,
        exit_price=11.0,
        quantity=10,
        commission_per_share=0.005,
        platform_fee_per_trade=0.0,
        spread_pct=0.01,
        slippage_pct=0.005,
        available_cash=50.0,
        minimum_net_profit=0.5,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "insufficient_cash"


def test_invalid_inputs_fail_closed():
    estimator = TradeCostEstimator()
    result = estimator.estimate(
        entry_price=10.0,
        exit_price=11.0,
        quantity=0,
        available_cash=100.0,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "invalid_inputs"


def run_test_direct():
    test_positive_edge_trade_is_allowed()
    test_low_edge_trade_is_blocked()
    test_wide_spread_is_blocked()
    test_costs_exceed_edge_is_blocked()
    test_insufficient_cash_is_blocked_without_margin_usage()
    test_invalid_inputs_fail_closed()


if __name__ == "__main__":
    run_test_direct()
