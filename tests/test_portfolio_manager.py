from __future__ import annotations

from src.portfolio.manager import PortfolioManager


def _manager() -> PortfolioManager:
    return PortfolioManager()


def _portfolio_state() -> dict:
    return {
        "account_equity": 2100.0,
        "cash": 1000.0,
        "positions": {
            "YINN": {"market_value": 300.0, "quantity": 12},
            "LABD": {"market_value": 280.0, "quantity": 20},
        },
    }


def test_leveraged_single_position_exceeded_is_rejected():
    result = _manager().check_portfolio_risk(
        {"ticker": "SOXS", "side": "BUY", "quantity": 100, "price": 4.0, "target_capital": 600.0, "reduce_only": False, "regime": "RANGE"},
        _portfolio_state(),
    )

    assert result["allowed"] is False
    assert result["reason"] == "single_position_exceeded"


def test_leveraged_group_exposure_exceeded_is_rejected():
    state = _portfolio_state()
    state["positions"]["SOXS"] = {"market_value": 600.0, "quantity": 120}
    state["positions"]["DRIP"] = {"market_value": 300.0, "quantity": 60}
    result = _manager().check_portfolio_risk(
        {"ticker": "YINN", "side": "BUY", "quantity": 12, "price": 25.0, "target_capital": 300.0, "reduce_only": False, "regime": "RANGE"},
        state,
    )

    assert result["allowed"] is False
    assert result["reason"] == "leveraged_group_exposure_exceeded"


def test_total_exposure_exceeded_is_rejected():
    state = {
        "account_equity": 1000.0,
        "cash": 100.0,
        "positions": {
            "AAA": {"market_value": 700.0, "quantity": 70},
            "BBB": {"market_value": 200.0, "quantity": 20},
        },
    }
    result = _manager().check_portfolio_risk(
        {"ticker": "CCC", "side": "BUY", "quantity": 20, "price": 10.0, "target_capital": 300.0, "reduce_only": False, "regime": "RANGE"},
        state,
    )

    assert result["allowed"] is False
    assert result["reason"] == "total_exposure_exceeded"


def test_max_positions_exceeded_is_rejected():
    state = {
        "account_equity": 2000.0,
        "cash": 1000.0,
        "positions": {
            "AAA": {"market_value": 200.0, "quantity": 10},
            "BBB": {"market_value": 200.0, "quantity": 10},
            "CCC": {"market_value": 200.0, "quantity": 10},
        },
    }
    result = _manager().check_portfolio_risk(
        {"ticker": "DDD", "side": "BUY", "quantity": 10, "price": 10.0, "target_capital": 100.0, "reduce_only": False, "regime": "RANGE"},
        state,
    )

    assert result["allowed"] is False
    assert result["reason"] == "max_positions_exceeded"


def test_event_regime_blocks_buy():
    result = _manager().check_portfolio_risk(
        {"ticker": "SOXS", "side": "BUY", "quantity": 10, "price": 5.0, "target_capital": 50.0, "reduce_only": False, "regime": "EVENT"},
        _portfolio_state(),
    )

    assert result["allowed"] is False
    assert result["reason"] == "event_regime_blocked"


def test_non_positive_equity_blocks_buy():
    state = _portfolio_state()
    state["account_equity"] = 0.0
    result = _manager().check_portfolio_risk(
        {"ticker": "SOXS", "side": "BUY", "quantity": 10, "price": 5.0, "target_capital": 50.0, "reduce_only": False, "regime": "RANGE"},
        state,
    )

    assert result["allowed"] is False
    assert result["reason"] == "invalid_order"


def test_invalid_price_or_quantity_blocks_buy():
    base_state = _portfolio_state()
    zero_price = _manager().check_portfolio_risk(
        {"ticker": "SOXS", "side": "BUY", "quantity": 10, "price": 0.0, "target_capital": 50.0, "reduce_only": False, "regime": "RANGE"},
        base_state,
    )
    zero_qty = _manager().check_portfolio_risk(
        {"ticker": "SOXS", "side": "BUY", "quantity": 0, "price": 5.0, "target_capital": 50.0, "reduce_only": False, "regime": "RANGE"},
        base_state,
    )

    assert zero_price["allowed"] is False
    assert zero_price["reason"] == "invalid_order"
    assert zero_qty["allowed"] is False
    assert zero_qty["reason"] == "invalid_order"


def test_sell_order_is_allowed():
    result = _manager().check_portfolio_risk(
        {"ticker": "YINN", "side": "SELL", "quantity": 5, "price": 25.0, "target_capital": 125.0, "reduce_only": False, "regime": "EVENT"},
        _portfolio_state(),
    )

    assert result["allowed"] is True
    assert result["reason"] == "sell_allowed"


def test_reduce_only_order_is_allowed():
    result = _manager().check_portfolio_risk(
        {"ticker": "YINN", "side": "BUY", "quantity": 5, "price": 25.0, "target_capital": 125.0, "reduce_only": True, "regime": "EVENT"},
        _portfolio_state(),
    )

    assert result["allowed"] is True
    assert result["reason"] == "reduce_only_allowed"


def test_qualified_buy_is_allowed():
    state = {
        "account_equity": 5000.0,
        "cash": 2000.0,
        "positions": {
            "SOFI": {"market_value": 400.0, "quantity": 20},
        },
    }
    result = _manager().check_portfolio_risk(
        {"ticker": "AAPL", "side": "BUY", "quantity": 10, "price": 20.0, "target_capital": 200.0, "reduce_only": False, "regime": "RANGE"},
        state,
    )

    assert result["allowed"] is True
    assert result["reason"] == "ok"
    assert result["current_exposure"] <= result["projected_exposure"]
