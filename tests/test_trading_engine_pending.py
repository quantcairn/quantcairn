from types import SimpleNamespace

from src.broker.base import Order, OrderSide, OrderStatus, OrderType
from src.config.loader import AppConfig
from src.engine.trading_engine import TradingEngine


class FakeStrategy:
    def __init__(self):
        self.recorded_entries = []
        self.cleared = False

    def record_entry(self, price: float) -> None:
        self.recorded_entries.append(price)

    def clear_entry(self) -> None:
        self.cleared = True


class FakeNotifier:
    def __init__(self):
        self.trades = []
        self.alerts = []

    def trade(self, ticker, side, quantity, price, pnl=None):
        self.trades.append((ticker, side, quantity, price, pnl))

    def alert(self, message, level="info"):
        self.alerts.append((message, level))


class FakeRisk:
    def __init__(self):
        self.records = []

    def record_trade(self, trade):
        self.records.append(trade)


def test_reconcile_pending_buy_fill_updates_local_state():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    engine.strategy = FakeStrategy()
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()

    engine.broker = SimpleNamespace(
        get_order=lambda order_id: Order(
            order_id=order_id,
            ticker="SOFI",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=5,
            filled_quantity=5,
            avg_fill_price=7.25,
            status=OrderStatus.FILLED,
        )
    )

    pending = Order(
        order_id="LB-BUY-1",
        ticker="SOFI",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=5,
        status=OrderStatus.PENDING,
    )
    engine._remember_pending_order(pending, "BUY", "BUY")
    engine._reconcile_pending_order()

    assert engine._pending_order is None
    assert engine._entry_price == 7.25
    assert engine._position_shares == 5
    assert engine.strategy.recorded_entries == [7.25]
    assert engine.notifier.trades == [("SOFI", "BUY", 5, 7.25, None)]


def test_reconcile_partial_buy_fill_keeps_pending_order():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    engine.strategy = FakeStrategy()
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()

    engine.broker = SimpleNamespace(
        get_order=lambda order_id: Order(
            order_id=order_id,
            ticker="SOFI",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=5,
            filled_quantity=2,
            avg_fill_price=7.2,
            status=OrderStatus.PARTIALLY_FILLED,
        )
    )

    pending = Order(
        order_id="LB-BUY-2",
        ticker="SOFI",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=5,
        status=OrderStatus.PENDING,
    )
    engine._remember_pending_order(pending, "BUY", "BUY")
    engine._reconcile_pending_order()

    assert engine._pending_order is not None
    assert engine._pending_order["acknowledged_filled_quantity"] == 2
    assert engine._position_shares == 2
    assert engine.notifier.trades == [("SOFI", "BUY", 2, 7.2, None)]


def test_reconcile_pending_sell_fill_records_trade():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    engine.strategy = FakeStrategy()
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()
    engine._entry_price = 7.0
    engine._position_shares = 5

    engine.broker = SimpleNamespace(
        get_order=lambda order_id: Order(
            order_id=order_id,
            ticker="SOFI",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=5,
            filled_quantity=5,
            avg_fill_price=7.8,
            status=OrderStatus.FILLED,
        )
    )

    pending = Order(
        order_id="LB-SELL-1",
        ticker="SOFI",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=5,
        status=OrderStatus.PENDING,
    )
    engine._remember_pending_order(pending, "SELL", "SELL")
    engine._reconcile_pending_order()

    assert engine._pending_order is None
    assert engine._position_shares == 0
    assert engine._entry_price is None
    assert engine.strategy.cleared is True
    assert len(engine.notifier.trades) == 1
    assert engine.notifier.trades[0][:4] == ("SOFI", "SELL", 5, 7.8)
    assert round(engine.notifier.trades[0][4], 2) == 4.0
    assert len(engine.risk.records) == 1
    assert round(engine.risk.records[0].pnl, 2) == 4.0


def run_test_direct():
    test_reconcile_pending_buy_fill_updates_local_state()
    test_reconcile_partial_buy_fill_keeps_pending_order()
    test_reconcile_pending_sell_fill_records_trade()


if __name__ == "__main__":
    run_test_direct()
