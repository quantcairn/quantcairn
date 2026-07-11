from __future__ import annotations

from src.strategy.inventory_sizing import InventoryAwareSizer


def test_inventory_ratio_zero_to_thirty_keeps_full_size():
    sizer = InventoryAwareSizer()
    result = sizer.adjust_quantity(
        base_quantity=20,
        current_position_value=0.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=10.0,
        leveraged_etf=False,
        leveraged_etf_limit=0.1,
        cash_reserve_ratio=0.0,
    )

    assert result["inventory_ratio"] == 0.0
    assert result["adjusted_quantity"] == 20
    assert result["adjustment_factor"] == 1.0
    assert result["allowed"] is True


def test_inventory_ratio_thirty_to_sixty_reduces_size():
    sizer = InventoryAwareSizer()
    result = sizer.adjust_quantity(
        base_quantity=20,
        current_position_value=400.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=10.0,
        cash_reserve_ratio=0.0,
    )

    assert 0.3 <= result["inventory_ratio"] < 0.6
    assert result["adjusted_quantity"] == 14
    assert result["allowed"] is True


def test_inventory_ratio_sixty_to_eighty_reduces_more():
    sizer = InventoryAwareSizer()
    result = sizer.adjust_quantity(
        base_quantity=20,
        current_position_value=700.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=10.0,
        cash_reserve_ratio=0.0,
    )

    assert 0.6 <= result["inventory_ratio"] < 0.8
    assert result["adjusted_quantity"] == 8
    assert result["allowed"] is True


def test_inventory_ratio_above_eighty_blocks_new_buys():
    sizer = InventoryAwareSizer()
    result = sizer.adjust_quantity(
        base_quantity=20,
        current_position_value=800.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=10.0,
    )

    assert result["inventory_ratio"] >= 0.8
    assert result["adjusted_quantity"] == 0
    assert result["allowed"] is False
    assert result["reject_reason"] == "inventory_limit"


def test_cash_reserve_and_cash_only_sizing_apply_before_allowing_trade():
    sizer = InventoryAwareSizer()
    result = sizer.adjust_quantity(
        base_quantity=20,
        current_position_value=0.0,
        allowed_position_value=1000.0,
        available_cash=100.0,
        current_price=10.0,
        cash_reserve_ratio=0.2,
    )

    assert result["adjusted_quantity"] == 8
    assert result["allowed"] is True


def test_leveraged_etf_uses_lower_allowed_position_value():
    sizer = InventoryAwareSizer()
    result = sizer.adjust_quantity(
        base_quantity=20,
        current_position_value=50.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=10.0,
        leveraged_etf=True,
        leveraged_etf_limit=0.1,
        cash_reserve_ratio=0.0,
    )

    assert result["effective_allowed_position_value"] == 100.0
    assert 0.3 <= result["inventory_ratio"] < 0.6
    assert result["adjusted_quantity"] == 5
    assert result["allowed"] is True


def test_invalid_price_or_quantity_fails_closed():
    sizer = InventoryAwareSizer()
    invalid_qty = sizer.adjust_quantity(
        base_quantity=0,
        current_position_value=0.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=10.0,
    )
    invalid_price = sizer.adjust_quantity(
        base_quantity=10,
        current_position_value=0.0,
        allowed_position_value=1000.0,
        available_cash=1000.0,
        current_price=0.0,
    )

    assert invalid_qty["allowed"] is False
    assert invalid_price["allowed"] is False


def run_test_direct():
    test_inventory_ratio_zero_to_thirty_keeps_full_size()
    test_inventory_ratio_thirty_to_sixty_reduces_size()
    test_inventory_ratio_sixty_to_eighty_reduces_more()
    test_inventory_ratio_above_eighty_blocks_new_buys()
    test_cash_reserve_and_cash_only_sizing_apply_before_allowing_trade()
    test_leveraged_etf_uses_lower_allowed_position_value()
    test_invalid_price_or_quantity_fails_closed()


if __name__ == "__main__":
    run_test_direct()
