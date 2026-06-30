from types import SimpleNamespace

from src.dashboard import server


def test_live_dashboard_uses_strategy_capital_basis():
    engine = SimpleNamespace(
        ticker="MSFT",
        mode="live",
        config=SimpleNamespace(position=SimpleNamespace(initial_capital=1000.0)),
        fetcher=SimpleNamespace(
            _cached_quote=SimpleNamespace(
                price=100.0,
                change_pct=0.5,
                bid=99.9,
                ask=100.1,
                high_1m=100.5,
                low_1m=99.5,
                volume=12345,
            )
        ),
        strategy=SimpleNamespace(
            get_range_state=lambda: SimpleNamespace(
                support=95.0,
                resistance=105.0,
                spread_dollars=10.0,
                spread_pct=10.53,
            )
        ),
        broker=SimpleNamespace(
            get_position_for_ticker=lambda ticker: None,
            get_account=lambda: SimpleNamespace(cash=707.61, equity=1558.11),
        ),
        risk=SimpleNamespace(
            get_stats=lambda: {
                "daily_pnl_today": 0.0,
                "total_trades": 0,
                "consecutive_losses": 0,
                "win_rate": 0.0,
                "halted": False,
            }
        ),
        _latest_position=None,
        _latest_account=None,
        _last_signal_type=None,
        _running=True,
    )

    original_engine = server._engine
    server._engine = engine
    try:
        data = server.get_dashboard_data()
    finally:
        server._engine = original_engine

    assert data["initial_capital"] == 1000.0
    assert data["cash"] == 1000.0
    assert data["equity"] == 1000.0


def run_test_direct():
    test_live_dashboard_uses_strategy_capital_basis()


if __name__ == "__main__":
    run_test_direct()
