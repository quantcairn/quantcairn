from __future__ import annotations

from src.risk.risk_engine import RiskEngine


def test_risk_engine_blocks_event_regime():
    engine = RiskEngine()
    assert not engine.check_trade_allowed({"regime": "EVENT", "estimated_position_pct": 0.1}, {"capital": 100000})


def test_risk_engine_blocks_single_position_over_limit():
    engine = RiskEngine()
    signal = {"regime": "TREND", "estimated_position_pct": 0.31}
    portfolio_state = {"capital": 100000}
    assert not engine.check_trade_allowed(signal, portfolio_state)


def test_risk_engine_blocks_total_exposure_over_limit():
    engine = RiskEngine()
    signal = {"regime": "TREND", "estimated_position_pct": 0.05}
    portfolio_state = {"capital": 100000, "current_total_exposure_pct": 1.01}
    assert not engine.check_trade_allowed(signal, portfolio_state)


def test_risk_engine_blocks_daily_loss_and_drawdown():
    engine = RiskEngine()
    signal = {"regime": "TREND", "estimated_position_pct": 0.05}
    assert not engine.check_trade_allowed(signal, {"capital": 100000, "daily_loss_pct": 0.031})
    assert not engine.check_trade_allowed(signal, {"capital": 100000, "drawdown_pct": 0.11})


def test_risk_engine_allows_normal_trade():
    engine = RiskEngine()
    signal = {"regime": "TREND", "estimated_position_pct": 0.1}
    assert engine.check_trade_allowed(signal, {"capital": 100000})


def test_risk_engine_blocks_new_buys_when_capital_is_below_floor():
    engine = RiskEngine(min_open_capital=1000, min_open_buying_power=1000)
    signal = {"regime": "TREND", "ticker": "AAPL.US", "trade_action": "buy", "estimated_position_pct": 0.05}
    portfolio_state = {"capital": 850, "account_mode": "live"}
    assert not engine.check_trade_allowed(signal, portfolio_state)


def test_risk_engine_includes_existing_live_positions_and_options():
    engine = RiskEngine()
    signal = {"regime": "TREND", "estimated_position_pct": 0.05}
    portfolio_state = {
        "capital": 1000,
        "positions_snapshot": {
            "positions": {
                "channels": [
                    {
                        "account_channel": "lb_sg",
                        "positions": [
                            {"symbol": "SOXS.US", "quantity": 132, "cost_price": 5.915},
                            {"symbol": "SPCX260717C265000.US", "quantity": 2, "cost_price": 0.6},
                            {"symbol": "SPCX260717C260000.US", "quantity": 1, "cost_price": 2.1},
                        ],
                    }
                ]
            }
        },
    }

    summary = engine.summarize_positions(portfolio_state, capital=1000)
    assert summary["account_mode"] == "live"
    assert summary["account_channel"] == "lb_sg"
    assert summary["current_single_position_pct"] > 0.3
    assert summary["current_total_exposure_pct"] > 1.0
    assert not engine.check_trade_allowed(signal, portfolio_state)


def test_risk_engine_blocks_buy_on_existing_position_but_allows_new_symbol():
    engine = RiskEngine()
    portfolio_state = {
        "capital": 100000,
        "positions_snapshot": {
            "account_channel": "lb_sg",
            "positions": {
                "channels": [
                    {
                        "account_channel": "lb_sg",
                        "positions": [
                            {"symbol": "SOXS.US", "quantity": 132, "cost_price": 5.915},
                        ],
                    }
                ]
            },
        },
    }

    assert not engine.check_trade_allowed({"regime": "TREND", "ticker": "SOXS.US", "trade_action": "buy", "estimated_position_pct": 0.05}, portfolio_state)
    assert engine.check_trade_allowed({"regime": "TREND", "ticker": "AAPL.US", "trade_action": "buy", "estimated_position_pct": 0.05}, portfolio_state)
    assert engine.check_trade_allowed({"regime": "TREND", "ticker": "SOXS.US", "trade_action": "sell", "estimated_position_pct": 0.05}, portfolio_state)


def test_risk_engine_allows_reduce_only_exit_even_when_overexposed():
    engine = RiskEngine()
    portfolio_state = {
        "capital": 1000,
        "positions_snapshot": {
            "account_channel": "lb_sg",
            "positions": {
                "channels": [
                    {
                        "account_channel": "lb_sg",
                        "positions": [
                            {"symbol": "SOXS.US", "quantity": 132, "cost_price": 5.915},
                            {"symbol": "SPCX260717C265000.US", "quantity": 2, "cost_price": 0.6},
                            {"symbol": "SPCX260717C260000.US", "quantity": 1, "cost_price": 2.1},
                        ],
                    }
                ]
            },
        },
    }

    assert engine.summarize_positions(portfolio_state, capital=1000)["current_total_exposure_pct"] > 1.0
    assert engine.check_trade_allowed({"regime": "TREND", "ticker": "SOXS.US", "trade_action": "sell", "estimated_position_pct": 0.05}, portfolio_state)


def test_risk_engine_blocks_new_buys_when_reduce_only_mode_is_active():
    engine = RiskEngine()
    portfolio_state = {"capital": 1000, "reduce_only": True}
    assert not engine.check_trade_allowed({"regime": "TREND", "ticker": "AAPL.US", "trade_action": "buy", "estimated_position_pct": 0.05}, portfolio_state)
    assert engine.check_trade_allowed({"regime": "TREND", "ticker": "AAPL.US", "trade_action": "sell", "estimated_position_pct": 0.05}, portfolio_state)


def test_risk_engine_blocks_new_buys_after_loss_streak():
    engine = RiskEngine()
    portfolio_state = {"capital": 1000, "consecutive_losses": 3}
    assert not engine.check_trade_allowed({"regime": "TREND", "ticker": "AAPL.US", "trade_action": "buy", "estimated_position_pct": 0.05}, portfolio_state)
    assert engine.check_trade_allowed({"regime": "TREND", "ticker": "AAPL.US", "trade_action": "sell", "estimated_position_pct": 0.05}, portfolio_state)
