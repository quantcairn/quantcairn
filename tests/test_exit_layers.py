from __future__ import annotations

from src.strategy.exit_layers import ExitLayerManager


def test_layer_based_exits_are_generated_once_per_filled_layer():
    manager = ExitLayerManager()
    result = manager.plan_exits(
        filled_entry_layers=[
            {"layer_id": 1, "filled_quantity": 2, "average_fill_price": 10.0, "status": "filled"},
            {"layer_id": 2, "filled_quantity": 2, "average_fill_price": 9.8, "status": "filled"},
            {"layer_id": 3, "filled_quantity": 1, "average_fill_price": 9.6, "status": "filled"},
        ],
        current_price=10.2,
        grid_width=0.5,
        current_broker_position=5,
        pending_sell_exists=False,
    )

    orders = result["orders"]
    assert result["allowed"] is True
    assert [order["layer_id"] for order in orders] == [1, 2, 3]
    assert [order["sell_quantity"] for order in orders] == [2, 2, 1]
    assert [order["target_price"] for order in orders] == [10.5, 10.3, 10.1]


def test_unfilled_or_exited_layers_are_not_sold_again():
    manager = ExitLayerManager()
    result = manager.plan_exits(
        filled_entry_layers=[
            {"layer_id": 1, "filled_quantity": 2, "average_fill_price": 10.0, "status": "filled", "exit_status": "exited"},
            {"layer_id": 2, "filled_quantity": 2, "average_fill_price": 9.8, "status": "pending"},
        ],
        current_price=10.2,
        grid_width=0.5,
        current_broker_position=5,
        pending_sell_exists=False,
    )

    assert result["orders"] == []
    assert result["allowed"] is False
    assert result["reject_reason"] == "no_exit_layers"


def test_pending_sell_blocks_new_exit_orders():
    manager = ExitLayerManager()
    result = manager.plan_exits(
        filled_entry_layers=[
            {"layer_id": 1, "filled_quantity": 2, "average_fill_price": 10.0, "status": "filled"},
        ],
        current_price=10.2,
        grid_width=0.5,
        current_broker_position=2,
        pending_sell_exists=True,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "pending_sell_exists"
    assert result["orders"] == []


def test_exit_sells_do_not_exceed_broker_position():
    manager = ExitLayerManager()
    result = manager.plan_exits(
        filled_entry_layers=[
            {"layer_id": 1, "filled_quantity": 2, "average_fill_price": 10.0, "status": "filled"},
            {"layer_id": 2, "filled_quantity": 2, "average_fill_price": 9.8, "status": "filled"},
        ],
        current_price=10.2,
        grid_width=0.5,
        current_broker_position=2,
        pending_sell_exists=False,
    )

    assert sum(order["sell_quantity"] for order in result["orders"]) == 2
    assert all(order["sell_quantity"] <= 2 for order in result["orders"])


def test_zero_position_or_invalid_market_state_blocks_exit_generation():
    manager = ExitLayerManager()
    zero_result = manager.plan_exits(
        filled_entry_layers=[
            {"layer_id": 1, "filled_quantity": 2, "average_fill_price": 10.0, "status": "filled"},
        ],
        current_price=10.2,
        grid_width=0.5,
        current_broker_position=0,
        pending_sell_exists=False,
    )
    invalid_market_result = manager.plan_exits(
        filled_entry_layers=[
            {"layer_id": 1, "filled_quantity": 2, "average_fill_price": 10.0, "status": "filled"},
        ],
        current_price=0.0,
        grid_width=0.5,
        current_broker_position=2,
        pending_sell_exists=False,
    )

    assert zero_result["allowed"] is False
    assert zero_result["reject_reason"] == "no_position"
    assert invalid_market_result["allowed"] is False
    assert invalid_market_result["reject_reason"] == "invalid_market_state"


def run_test_direct():
    test_layer_based_exits_are_generated_once_per_filled_layer()
    test_unfilled_or_exited_layers_are_not_sold_again()
    test_pending_sell_blocks_new_exit_orders()
    test_exit_sells_do_not_exceed_broker_position()
    test_zero_position_or_invalid_market_state_blocks_exit_generation()


if __name__ == "__main__":
    run_test_direct()
