import tempfile
from pathlib import Path

from src.broker.base import AccountInfo, Order, OrderSide, OrderStatus, OrderType, Position
from src.engine.orphan_monitor import OrphanPositionMonitor
from src.engine.trading_engine import TradingEngine, check_exit_conditions


def _use_test_state(engine: TradingEngine, name: str) -> None:
    pending = Path(tempfile.gettempdir()) / f"soxs-orphan-pending-{name}.json"
    sync = Path(tempfile.gettempdir()) / f"soxs-orphan-sync-{name}.json"
    pending.unlink(missing_ok=True)
    sync.unlink(missing_ok=True)
    engine._pending_order_state_path = pending
    engine._position_sync_state_path = sync
    engine._pending_order = None
    engine._position_sync_fence = None
    lock = Path(tempfile.gettempdir()) / f"soxs-orphan-sell-lock-{name}.lock"
    lock.unlink(missing_ok=True)
    engine._sell_lock_path = lock


class FakeBroker:
    def __init__(self, positions=None, reliable=True, account_reliable=True):
        self.positions = list(positions or [])
        self.reliable = reliable
        self.account_reliable = account_reliable
        self.orders = []

    def get_positions(self):
        return list(self.positions)

    def is_positions_snapshot_reliable(self):
        return self.reliable

    def get_account(self):
        return AccountInfo(cash=100.0, equity=100.0, buying_power=100.0, positions=self.positions)

    def is_account_snapshot_reliable(self):
        return self.account_reliable

    def get_position_for_ticker(self, ticker):
        for pos in self.positions:
            if str(pos.ticker).split(".")[0].upper() == str(ticker).split(".")[0].upper():
                return pos
        return None

    def get_order(self, order_id):
        return None

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return Order(
            order_id=f"SELL-{len(self.orders)}",
            ticker=kwargs["ticker"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=float(kwargs.get("current_bid") or kwargs.get("current_ask") or 0.0),
            status=OrderStatus.FILLED,
        )


def _position(ticker: str, qty: int, avg_cost: float, current_price: float) -> Position:
    market_value = qty * current_price
    pnl = (current_price - avg_cost) * qty
    pnl_pct = (pnl / (avg_cost * qty) * 100.0) if qty > 0 and avg_cost > 0 else 0.0
    return Position(
        ticker=ticker,
        quantity=qty,
        avg_entry_price=avg_cost,
        current_price=current_price,
        market_value=market_value,
        unrealized_pnl=pnl,
        unrealized_pnl_pct=pnl_pct,
    )


def test_normal_stock_stop_loss_triggers():
    result = check_exit_conditions("AAPL", 95.0, 100.0, 10)
    assert result["should_exit"] is True
    assert result["reason"] == "stop_loss"


def test_normal_stock_take_profit_triggers():
    result = check_exit_conditions("AAPL", 110.0, 100.0, 10)
    assert result["should_exit"] is True
    assert result["reason"] == "take_profit"


def test_soxs_stop_loss_triggers():
    result = check_exit_conditions("SOXS", 10.5, 10.0, 10, is_inverse_etf=True)
    assert result["should_exit"] is True
    assert result["reason"] == "stop_loss"


def test_soxs_take_profit_triggers():
    result = check_exit_conditions("SOXS", 9.0, 10.0, 10, is_inverse_etf=True)
    assert result["should_exit"] is True
    assert result["reason"] == "take_profit"


def test_orphan_normal_stock_stop_loss_triggers():
    result = check_exit_conditions("PLTR", 92.0, 100.0, 5, mode="orphan")
    assert result["should_exit"] is True
    assert result["reason"] == "stop_loss"


def test_orphan_soxs_stop_loss_triggers():
    result = check_exit_conditions("SOXS", 10.8, 10.0, 5, is_inverse_etf=True, mode="orphan")
    assert result["should_exit"] is True
    assert result["reason"] == "stop_loss"


def test_orphan_monitor_never_submits_buy():
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("PLTR", 2, 100.0, 92.0)
    engine = monitor._engine_for_symbol("PLTR")
    _use_test_state(engine, "never-buy")

    monitor._evaluate_symbol("PLTR", pos)

    assert len(broker.orders) == 1
    assert broker.orders[0]["side"] == OrderSide.SELL


def test_orphan_monitor_does_not_take_profit():
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("PLTR", 2, 100.0, 112.0)
    engine = monitor._engine_for_symbol("PLTR")
    _use_test_state(engine, "no-take-profit")

    monitor._evaluate_symbol("PLTR", pos)

    assert broker.orders == []


def test_broker_position_verification_failure_skips_symbol():
    broker = FakeBroker(positions=[_position("PLTR", 2, 100.0, 92.0)], reliable=False)
    monitor = OrphanPositionMonitor(broker=broker)

    assert monitor.verify_broker_positions() is None
    assert monitor.scan_orphans() == {}


def test_startup_safety_requires_reliable_account_snapshot():
    broker = FakeBroker(
        positions=[_position("PLTR", 2, 100.0, 92.0)],
        reliable=True,
        account_reliable=False,
    )
    monitor = OrphanPositionMonitor(broker=broker)

    assert monitor.verify_startup_safety() is None


def test_startup_safety_accepts_verified_positions_and_account():
    positions = [_position("PLTR", 2, 100.0, 92.0)]
    broker = FakeBroker(positions=positions, reliable=True, account_reliable=True)
    monitor = OrphanPositionMonitor(broker=broker)

    verified = monitor.verify_startup_safety()

    assert verified is not None
    assert verified[0] == positions


def test_existing_sell_lock_prevents_duplicate_sell():
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("SOXS", 3, 10.0, 10.8)
    engine = monitor._engine_for_symbol("SOXS")
    _use_test_state(engine, "sell-lock")
    engine._pending_order = {"side": "SELL", "order_id": "LOCKED"}

    monitor._evaluate_symbol("SOXS", pos)

    assert broker.orders == []


def test_position_qty_zero_stops_orphan_monitoring():
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("PLTR", 0, 100.0, 92.0)
    engine = monitor._engine_for_symbol("PLTR")
    _use_test_state(engine, "zero-qty")

    monitor._evaluate_symbol("PLTR", pos)

    assert broker.orders == []


def test_offline_assigned_process_becomes_orphan_after_three_failures():
    pos = _position("PLTR", 2, 100.0, 92.0)
    broker = FakeBroker(positions=[pos])
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._startup_at = 0
    monitor._is_top_process_active = lambda _port: False

    assert "PLTR" not in monitor.scan_orphans([pos])
    assert "PLTR" not in monitor.scan_orphans([pos])
    assert "PLTR" in monitor.scan_orphans([pos])


def test_market_hours_check_prevents_execution_outside_regular_hours():
    broker = FakeBroker(positions=[_position("PLTR", 2, 100.0, 92.0)], reliable=True)
    monitor = OrphanPositionMonitor(broker=broker, poll_interval_seconds=60)
    engine = monitor._engine_for_symbol("PLTR")
    _use_test_state(engine, "market-hours")
    engine._is_trading_hours = lambda: False
    calls = {"evaluate": 0}

    def fake_evaluate(symbol, pos):
        calls["evaluate"] += 1

    monitor._evaluate_symbol = fake_evaluate
    sleeps = {"count": 0}

    def fake_sleep(_seconds):
        sleeps["count"] += 1
        monitor.stop()

    import src.engine.orphan_monitor as module

    original_sleep = module.time.sleep
    module.time.sleep = fake_sleep
    try:
        assert monitor.run() == 0
    finally:
        module.time.sleep = original_sleep

    assert calls["evaluate"] == 0
    assert sleeps["count"] >= 1


def run_test_direct():
    test_normal_stock_stop_loss_triggers()
    test_normal_stock_take_profit_triggers()
    test_soxs_stop_loss_triggers()
    test_soxs_take_profit_triggers()
    test_orphan_normal_stock_stop_loss_triggers()
    test_orphan_soxs_stop_loss_triggers()
    test_orphan_monitor_never_submits_buy()
    test_orphan_monitor_does_not_take_profit()
    test_broker_position_verification_failure_skips_symbol()
    test_startup_safety_requires_reliable_account_snapshot()
    test_startup_safety_accepts_verified_positions_and_account()
    test_existing_sell_lock_prevents_duplicate_sell()
    test_position_qty_zero_stops_orphan_monitoring()
    test_offline_assigned_process_becomes_orphan_after_three_failures()
    test_market_hours_check_prevents_execution_outside_regular_hours()
