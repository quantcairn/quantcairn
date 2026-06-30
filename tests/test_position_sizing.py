from src.engine.position_sizing import determine_buy_quantity


def test_fixed_size_is_capped_by_cash_and_max_position():
    shares = determine_buy_quantity(
        current_price=10.0,
        available_cash=1_000.0,
        configured_size=250,
        max_position=200,
        execution_price=10.20,
    )

    assert shares == 97


def test_auto_size_uses_cash_when_configured_size_is_zero():
    shares = determine_buy_quantity(
        current_price=20.0,
        available_cash=1_000.0,
        configured_size=0,
        max_position=9999,
        execution_price=20.10,
    )

    assert shares == 40


def test_invalid_price_returns_zero_shares():
    shares = determine_buy_quantity(
        current_price=0.0,
        available_cash=1_000.0,
        configured_size=100,
        max_position=9999,
    )

    assert shares == 0


def test_insufficient_cash_returns_zero_shares():
    shares = determine_buy_quantity(
        current_price=10.0,
        available_cash=5.0,
        configured_size=10,
        max_position=9999,
        execution_price=10.20,
    )

    assert shares == 0


def test_execution_price_and_commission_reduce_affordable_shares():
    shares = determine_buy_quantity(
        current_price=10.0,
        available_cash=100.0,
        configured_size=20,
        max_position=9999,
        execution_price=10.0,
        commission_per_share=0.50,
    )

    assert shares == 9


def run_test_direct():
    test_fixed_size_is_capped_by_cash_and_max_position()
    test_auto_size_uses_cash_when_configured_size_is_zero()
    test_invalid_price_returns_zero_shares()
    test_insufficient_cash_returns_zero_shares()
    test_execution_price_and_commission_reduce_affordable_shares()


if __name__ == "__main__":
    run_test_direct()
