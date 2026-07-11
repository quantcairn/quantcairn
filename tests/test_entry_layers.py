from __future__ import annotations

from src.strategy.entry_layers import EntryLayerPlanner


def test_entry_layer_weights_and_quantities_are_monotonic():
    planner = EntryLayerPlanner()
    result = planner.plan_layers(
        support=10.0,
        grid_width=2.0,
        total_target_quantity=10,
        max_layers=5,
        existing_layers=[],
        pending_buy_exists=False,
        inventory_ratio=0.0,
        trend_buy_allowed=True,
    )

    quantities = [layer["target_quantity"] for layer in result["layers"]]
    assert quantities == [3, 3, 2, 1, 1]
    assert sum(quantities) == 10
    assert all(quantities[index] >= quantities[index + 1] for index in range(len(quantities) - 1))
    assert [layer["layer_id"] for layer in result["layers"]] == [1, 2, 3, 4, 5]


def test_entry_layer_planner_drops_zero_quantity_layers():
    planner = EntryLayerPlanner()
    result = planner.plan_layers(
        support=10.0,
        grid_width=2.0,
        total_target_quantity=2,
        max_layers=5,
        existing_layers=[],
        pending_buy_exists=False,
        inventory_ratio=0.0,
        trend_buy_allowed=True,
    )

    assert [layer["target_quantity"] for layer in result["layers"]] == [1, 1]
    assert len(result["layers"]) == 2


def test_pending_buy_blocks_new_layers():
    planner = EntryLayerPlanner()
    result = planner.plan_layers(
        support=10.0,
        grid_width=2.0,
        total_target_quantity=10,
        max_layers=5,
        existing_layers=[],
        pending_buy_exists=True,
        inventory_ratio=0.0,
        trend_buy_allowed=True,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "pending_buy_exists"
    assert result["layers"] == []


def test_trend_guard_blocks_new_layers():
    planner = EntryLayerPlanner()
    result = planner.plan_layers(
        support=10.0,
        grid_width=2.0,
        total_target_quantity=10,
        max_layers=5,
        existing_layers=[],
        pending_buy_exists=False,
        inventory_ratio=0.0,
        trend_buy_allowed=False,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "trend_buy_blocked"
    assert result["layers"] == []


def test_inventory_above_eighty_blocks_generation():
    planner = EntryLayerPlanner()
    result = planner.plan_layers(
        support=10.0,
        grid_width=2.0,
        total_target_quantity=10,
        max_layers=5,
        existing_layers=[],
        pending_buy_exists=False,
        inventory_ratio=0.8,
        trend_buy_allowed=True,
    )

    assert result["allowed"] is False
    assert result["reject_reason"] == "inventory_limit"
    assert result["layers"] == []


def test_existing_layers_are_not_repeated():
    planner = EntryLayerPlanner()
    result = planner.plan_layers(
        support=10.0,
        grid_width=2.0,
        total_target_quantity=10,
        max_layers=5,
        existing_layers=[{"layer_id": 1, "status": "planned"}],
        pending_buy_exists=False,
        inventory_ratio=0.0,
        trend_buy_allowed=True,
    )

    layer_ids = [layer["layer_id"] for layer in result["layers"]]
    assert 1 not in layer_ids
    assert len(layer_ids) == 4


def run_test_direct():
    test_entry_layer_weights_and_quantities_are_monotonic()
    test_entry_layer_planner_drops_zero_quantity_layers()
    test_pending_buy_blocks_new_layers()
    test_trend_guard_blocks_new_layers()
    test_inventory_above_eighty_blocks_generation()
    test_existing_layers_are_not_repeated()


if __name__ == "__main__":
    run_test_direct()
