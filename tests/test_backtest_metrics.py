from __future__ import annotations

from src.backtest.metrics import compute_backtest_metrics


def test_metrics_handle_no_trades_and_zero_variance():
    metrics = compute_backtest_metrics(
        initial_cash=1000.0,
        equity_curve=[{"timestamp": "2026-07-01T00:00:00Z", "equity": 1000.0}],
        trades=[],
        orders=[],
        rejected_signals=[],
    )

    assert metrics["ending_equity"] == 1000.0
    assert metrics["trade_count"] == 0
    assert metrics["profit_factor"] is None
    assert metrics["sharpe"] is None or metrics["sharpe"] == 0.0
    assert metrics["no_trade"] is True


def test_metrics_compute_from_simple_round_trip():
    equity_curve = [
        {"timestamp": "2026-07-01T00:00:00Z", "equity": 1000.0},
        {"timestamp": "2026-07-01T00:01:00Z", "equity": 1010.0},
        {"timestamp": "2026-07-01T00:02:00Z", "equity": 1005.0},
    ]
    trades = [
        {"symbol": "SOXS.US", "side": "BUY", "filled_quantity": 10, "filled_price": 10.0, "commission": 0.1, "fees": 0.0, "slippage": 0.2},
        {"symbol": "SOXS.US", "side": "SELL", "filled_quantity": 10, "filled_price": 10.6, "commission": 0.1, "fees": 0.0, "slippage": 0.2},
    ]
    metrics = compute_backtest_metrics(
        initial_cash=1000.0,
        equity_curve=equity_curve,
        trades=trades,
        orders=trades,
        rejected_signals=[{"reason": "trend_guard"}],
    )

    assert metrics["total_return"] > 0
    assert metrics["trade_count"] == 2
    assert metrics["blocked_by_trend_count"] == 1
    assert metrics["rejected_order_count"] == 1
    assert metrics["turnover_notional"] == 206.0
    assert metrics["turnover"] == round(206.0 / 1005.0, 6)


def test_metrics_scale_turnover_by_average_equity():
    equity_curve = [
        {"timestamp": "2026-07-01T00:00:00Z", "equity": 1000.0},
        {"timestamp": "2026-07-01T00:01:00Z", "equity": 1005.0},
        {"timestamp": "2026-07-01T00:02:00Z", "equity": 1010.0},
    ]
    trades = [
        {"symbol": "SOXS.US", "side": "BUY", "filled_quantity": 100, "filled_price": 10.0},
        {"symbol": "SOXS.US", "side": "SELL", "filled_quantity": 100, "filled_price": 10.5},
    ]
    metrics = compute_backtest_metrics(
        initial_cash=1000.0,
        equity_curve=equity_curve,
        trades=trades,
        orders=trades,
        rejected_signals=[],
    )

    assert metrics["turnover_notional"] == 2050.0
    assert 0 < metrics["turnover"] < 3.0
    assert metrics["turnover"] == round(2050.0 / 1005.0, 6)
