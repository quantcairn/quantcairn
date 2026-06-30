from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.paper_broker import PaperBroker


def test_paper_broker_rejects_buy_when_cash_is_insufficient():
    broker = PaperBroker(initial_cash=100.0, commission_per_share=0.50)
    broker.connect()

    order = broker.place_order(
        ticker="SOXS",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        current_bid=10.0,
        current_ask=10.0,
    )

    assert order.status == OrderStatus.REJECTED
    assert order.notes == "Insufficient cash"
    assert broker.get_account().cash == 100.0


def test_paper_broker_round_trip_applies_commission_and_slippage():
    broker = PaperBroker(initial_cash=1_000.0, commission_per_share=0.005)
    broker.connect()

    buy = broker.place_order(
        ticker="SOXS",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        current_bid=10.0,
        current_ask=10.0,
    )
    sell = broker.place_order(
        ticker="SOXS",
        side=OrderSide.SELL,
        quantity=10,
        order_type=OrderType.MARKET,
        current_bid=10.0,
        current_ask=10.0,
    )

    assert buy.status == OrderStatus.FILLED
    assert sell.status == OrderStatus.FILLED
    assert broker.get_account().cash < 1_000.0


def test_paper_broker_rejects_sell_without_changing_cash():
    broker = PaperBroker(initial_cash=1_000.0, commission_per_share=0.005)
    broker.connect()
    broker.seed_position("SOXS", quantity=2, avg_price=10.0)

    order = broker.place_order(
        ticker="SOXS",
        side=OrderSide.SELL,
        quantity=5,
        order_type=OrderType.MARKET,
        current_bid=10.0,
        current_ask=10.0,
    )

    assert order.status == OrderStatus.REJECTED
    assert order.notes == "Insufficient position"
    assert broker.get_account().cash == 980.0
    assert broker.get_position_for_ticker("SOXS").quantity == 2


def test_paper_broker_refuses_seed_position_when_cash_is_insufficient():
    broker = PaperBroker(initial_cash=10.0)
    seeded = broker.seed_position("SOXS", quantity=2, avg_price=10.0)

    assert seeded is None
    assert broker.get_account().cash == 10.0
    assert broker.get_position_for_ticker("SOXS") is None


def run_test_direct():
    test_paper_broker_rejects_buy_when_cash_is_insufficient()
    test_paper_broker_round_trip_applies_commission_and_slippage()
    test_paper_broker_rejects_sell_without_changing_cash()
    test_paper_broker_refuses_seed_position_when_cash_is_insufficient()


if __name__ == "__main__":
    run_test_direct()
