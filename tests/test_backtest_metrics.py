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
    assert metrics["closed_trade_count"] == 0
    assert metrics["win_rate"] is None
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
        portfolio_snapshot={
            "available_cash": 1005.8,
            "total_equity": 1005.8,
            "realized_pnl": 6.0,
            "unrealized_pnl": 0.0,
            "positions": {},
        },
    )

    assert metrics["total_return"] > 0
    assert metrics["trade_count"] == 2
    assert metrics["fill_count"] == 2
    assert metrics["order_count"] == 2
    assert metrics["closed_trade_count"] == 1
    assert metrics["round_trip_trade_count"] == 1
    assert metrics["winning_trade_count"] == 1
    assert metrics["losing_trade_count"] == 0
    assert metrics["win_rate"] == 1.0
    assert metrics["blocked_by_trend_count"] == 1
    assert metrics["rejected_order_count"] == 1
    assert metrics["turnover_notional"] == 206.0
    assert metrics["turnover"] == round(206.0 / 1005.0, 6)
    assert metrics["reconciliation_status"] == "OK"
    assert metrics["reconciliation_difference"] == 0.0


def test_metrics_handle_open_positions_and_no_closed_trades():
    equity_curve = [
        {"timestamp": "2026-07-01T00:00:00Z", "equity": 1000.0},
        {"timestamp": "2026-07-01T00:01:00Z", "equity": 1001.0},
    ]
    trades = [
        {"symbol": "SOXS.US", "side": "BUY", "filled_quantity": 10, "filled_price": 10.0, "commission": 0.1, "fees": 0.0, "slippage": 0.2},
    ]
    metrics = compute_backtest_metrics(
        initial_cash=1000.0,
        equity_curve=equity_curve,
        trades=trades,
        orders=trades,
        rejected_signals=[],
        portfolio_snapshot={
            "available_cash": 899.9,
            "total_equity": 999.9,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "positions": {"SOXS.US": {"quantity": 10, "market_value": 100.0, "unrealized_pnl": 0.0}},
        },
    )

    assert metrics["closed_trade_count"] == 0
    assert metrics["open_position_count"] == 1
    assert metrics["win_rate"] is None
    assert metrics["profit_factor"] is None
    assert metrics["reconciliation_status"] == "OK"


def test_metrics_build_layer_reconciliation_rows():
    metrics = compute_backtest_metrics(
        initial_cash=1000.0,
        equity_curve=[{"timestamp": "2026-07-01T00:00:00Z", "equity": 1000.0}],
        trades=[
            {"symbol": "SOXS.US", "side": "BUY", "layer_id": 1, "filled_quantity": 10, "filled_price": 10.0, "commission": 0.1, "fees": 0.0},
            {"symbol": "SOXS.US", "side": "SELL", "layer_id": 1, "filled_quantity": 10, "filled_price": 10.5, "commission": 0.1, "fees": 0.0},
            {"symbol": "SOXS.US", "side": "BUY", "layer_id": 2, "filled_quantity": 5, "filled_price": 9.5, "commission": 0.05, "fees": 0.0},
        ],
        orders=[],
        rejected_signals=[],
        portfolio_snapshot={
            "available_cash": 957.25,
            "total_equity": 1004.75,
            "realized_pnl": 5.0,
            "unrealized_pnl": 0.0,
            "positions": {"SOXS.US": {"quantity": 5, "market_value": 47.5, "unrealized_pnl": 0.0}},
        },
    )

    rows = metrics["layer_reconciliation"]
    assert len(rows) == 2
    closed = next(row for row in rows if row["layer_id"] == 1)
    open_row = next(row for row in rows if row["layer_id"] == 2)
    assert closed["status"] == "closed"
    assert closed["remaining_quantity"] == 0
    assert open_row["status"] == "open"
    assert open_row["remaining_quantity"] == 5


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
