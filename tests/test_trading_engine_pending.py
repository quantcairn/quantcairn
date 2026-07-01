import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.broker.base import Order, OrderSide, OrderStatus, OrderType
from src.config.loader import AppConfig, PositionConfig
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


def use_test_pending_path(engine: TradingEngine, name: str) -> None:
    path = Path(tempfile.gettempdir()) / f"soxs-test-pending-{name}.json"
    path.unlink(missing_ok=True)
    engine._pending_order_state_path = path


def test_reconcile_pending_buy_fill_updates_local_state():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    use_test_pending_path(engine, "buy-fill")
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
    use_test_pending_path(engine, "partial-buy")
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
    use_test_pending_path(engine, "sell-fill")
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


def test_reduce_only_blocks_new_buy_orders():
    engine = TradingEngine(
        AppConfig(ticker="SOFI", position=PositionConfig(reduce_only=True)),
        ignore_trading_hours=True,
    )
    use_test_pending_path(engine, "reduce-only")
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()
    place_calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0),
        place_order=lambda **kwargs: place_calls.append(kwargs),
    )

    engine._handle_buy_signal(SimpleNamespace(type="BUY", reason="test"), 100.0, 100.1)

    assert place_calls == []
    assert engine._last_signal_reason == "仅减仓模式：今晚不新开仓"


def test_buy_sizing_does_not_use_margin_buying_power():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    use_test_pending_path(engine, "cash-only")
    engine.notifier = FakeNotifier()
    place_calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=50.0, buying_power=1000.0),
        place_order=lambda **kwargs: place_calls.append(kwargs),
    )

    engine._handle_buy_signal(
        SimpleNamespace(type=SimpleNamespace(value="BUY")), 100.0, 100.0
    )

    assert place_calls == []
    assert "现金 $50.00" in engine._last_signal_reason


def test_partial_immediate_sell_keeps_unfilled_position():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    use_test_pending_path(engine, "partial-sell")
    engine.strategy = FakeStrategy()
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()
    engine._entry_price = 10.0
    engine._position_shares = 5
    engine.broker = SimpleNamespace(
        place_order=lambda **kwargs: Order(
            order_id="PARTIAL-SELL",
            ticker="SOFI",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=5,
            filled_quantity=2,
            avg_fill_price=11.0,
            status=OrderStatus.PARTIALLY_FILLED,
        )
    )

    engine._handle_sell_signal(SimpleNamespace(type=SimpleNamespace(value="SELL")), 11.0, 11.0)

    assert engine._position_shares == 3
    assert engine._entry_price == 10.0
    assert engine.strategy.cleared is False
    assert engine.risk.records[0].shares == 2
    assert engine.risk.records[0].pnl == 2.0


def test_pending_order_survives_engine_restart():
    first = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    use_test_pending_path(first, "restart")
    pending = Order(
        order_id="LB-RESTART-1",
        ticker="SOFI",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=2,
        status=OrderStatus.PENDING,
    )
    first._remember_pending_order(pending, "SELL", "SELL")

    second = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    second._pending_order_state_path = first._pending_order_state_path
    second._pending_order = None
    second._load_pending_order()

    assert second._pending_order is not None
    assert second._pending_order["order_id"] == "LB-RESTART-1"
    second._clear_pending_order()


def test_live_startup_adopts_existing_broker_order():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    use_test_pending_path(engine, "adopt-active")
    active = Order(
        order_id="LB-ACTIVE-1",
        ticker="SOFI",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=2,
        status=OrderStatus.PENDING,
    )
    engine.broker = SimpleNamespace(get_active_orders=lambda ticker: [active])

    assert engine._adopt_active_live_order() is True
    assert engine._pending_order["order_id"] == "LB-ACTIVE-1"
    engine._clear_pending_order()


def run_test_direct():
    test_reconcile_pending_buy_fill_updates_local_state()
    test_reconcile_partial_buy_fill_keeps_pending_order()
    test_reconcile_pending_sell_fill_records_trade()
    test_reduce_only_blocks_new_buy_orders()
    test_buy_sizing_does_not_use_margin_buying_power()
    test_partial_immediate_sell_keeps_unfilled_position()
    test_pending_order_survives_engine_restart()
    test_live_startup_adopts_existing_broker_order()


if __name__ == "__main__":
    run_test_direct()
