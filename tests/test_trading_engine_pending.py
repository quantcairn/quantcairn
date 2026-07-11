import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
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

    def trade(self, ticker, side, quantity, price, pnl=None, mode=None, **kwargs):
        self.trades.append((ticker, side, quantity, price, pnl, mode, kwargs))

    def alert(self, message, level="info"):
        self.alerts.append((message, level))


class FakeRisk:
    def __init__(self):
        self.records = []
        self.equity_updates = []

    def record_trade(self, trade):
        self.records.append(trade)

    def update_equity(self, equity: float):
        self.equity_updates.append(equity)


def use_test_pending_path(engine: TradingEngine, name: str) -> None:
    path = Path(tempfile.gettempdir()) / f"soxs-test-pending-{name}.json"
    path.unlink(missing_ok=True)
    engine._pending_order_state_path = path
    sync_path = Path(tempfile.gettempdir()) / f"soxs-test-position-sync-{name}.json"
    sync_path.unlink(missing_ok=True)
    engine._position_sync_state_path = sync_path
    engine._position_sync_fence = None
    lock_path = Path(tempfile.gettempdir()) / f"soxs-test-sell-lock-{name}.lock"
    lock_path.unlink(missing_ok=True)
    engine._sell_lock_path = lock_path


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
    assert engine.notifier.trades == [("SOFI", "BUY", 5, 7.25, None, "paper", {})]


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
    assert engine.notifier.trades == [("SOFI", "BUY", 2, 7.2, None, "paper", {})]


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


def test_adopt_active_live_order_retries_after_rate_limit():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
    use_test_pending_path(engine, "adopt-retry")
    engine.notifier = FakeNotifier()

    calls = {"count": 0}

    def fake_get_active_orders(_ticker):
        calls["count"] += 1
        if calls["count"] < 3:
            return None
        return []

    with patch("src.engine.trading_engine.time.sleep", lambda _seconds: None):
        engine.broker = SimpleNamespace(get_active_orders=fake_get_active_orders)
        assert engine._adopt_active_live_order() is True
        assert calls["count"] == 3
        assert engine.notifier.alerts == []


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


def test_market_calendar_blocks_holidays_and_uses_early_close():
    engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=False)

    assert engine._market_session_end(date(2026, 7, 3)) is None
    assert engine._market_session_end(date(2026, 7, 2)) == "13:00"
    assert engine._market_session_end(date(2026, 11, 27)) == "13:00"
    assert engine._market_session_end(date(2026, 7, 6)) == "16:00"


def test_live_anytime_override_cannot_bypass_market_calendar():
    engine = TradingEngine(
        AppConfig(ticker="SOFI", mode="live"),
        ignore_trading_hours=True,
    )
    engine._market_session_end = lambda _day: None

    assert engine._is_trading_hours() is False


def test_sell_fill_blocks_stale_position_from_triggering_duplicate_sell():
    config = AppConfig(ticker="PLTR", mode="live")
    engine = TradingEngine(config, ignore_trading_hours=True)
    use_test_pending_path(engine, "stale-after-sell")
    engine._position_shares = 2

    engine._set_position_sync_fence(0)

    assert engine._apply_position_sync_fence(2) == 0
    assert engine._position_sync_fence is not None
    assert "等待券商确认" in engine._last_signal_reason
    assert engine._apply_position_sync_fence(0) == 0
    assert engine._position_sync_fence is None


def test_position_sync_fence_survives_restart():
    first = TradingEngine(AppConfig(ticker="PLTR", mode="live"), ignore_trading_hours=True)
    use_test_pending_path(first, "sync-restart")
    first._set_position_sync_fence(0)

    second = TradingEngine(AppConfig(ticker="PLTR", mode="live"), ignore_trading_hours=True)
    second._position_sync_state_path = first._position_sync_state_path
    second._position_sync_fence = None
    second._load_position_sync_fence()

    assert second._apply_position_sync_fence(2) == 0
    assert second._position_sync_fence is not None
    second._clear_position_sync_fence()


def test_live_startup_safety_blocks_when_reduce_only_disabled():
    engine = TradingEngine(AppConfig(ticker="PLTR", mode="live"), ignore_trading_hours=True)
    engine._reduce_only = False

    assert engine._verify_live_startup_safety() is False


def test_live_startup_safety_blocks_unreliable_account():
    engine = TradingEngine(AppConfig(ticker="PLTR", mode="live"), ignore_trading_hours=True)
    engine._reduce_only = True
    engine.broker = SimpleNamespace(
        get_positions=lambda: [],
        is_positions_snapshot_reliable=lambda: True,
        get_account=lambda: SimpleNamespace(equity=0.0),
        is_account_snapshot_reliable=lambda: False,
    )

    assert engine._verify_live_startup_safety() is False


def test_cross_process_sell_lock_allows_only_one_engine():
    first = TradingEngine(AppConfig(ticker="PLTR", mode="live"), ignore_trading_hours=True)
    second = TradingEngine(AppConfig(ticker="PLTR", mode="live"), ignore_trading_hours=True)
    use_test_pending_path(first, "atomic-sell-lock")
    second._sell_lock_path = first._sell_lock_path

    assert first._acquire_sell_lock("first") is True
    assert second._acquire_sell_lock("second") is False

    first._release_sell_lock("test_complete")
    assert second._acquire_sell_lock("retry") is True
    second._release_sell_lock("test_complete")


def test_paper_sell_does_not_create_live_position_fence():
    engine = TradingEngine(AppConfig(ticker="SOFI", mode="paper"), ignore_trading_hours=True)
    use_test_pending_path(engine, "paper-no-fence")

    engine._set_position_sync_fence(0)

    assert engine._position_sync_fence is None
    assert not engine._position_sync_state_path.exists()


def test_refresh_broker_snapshots_updates_live_cache_outside_hours():
    engine = TradingEngine(AppConfig(ticker="SOFI", mode="live"), ignore_trading_hours=False)
    use_test_pending_path(engine, "outside-hours-refresh")
    engine.risk = FakeRisk()
    engine._pending_order = None
    engine._position_sync_fence = None
    engine.broker = SimpleNamespace(
        get_position_for_ticker=lambda ticker: SimpleNamespace(
            quantity=30,
            avg_entry_price=18.09,
            unrealized_pnl=-4.95,
        ),
        is_positions_snapshot_reliable=lambda: True,
        get_account=lambda: SimpleNamespace(
            cash=707.61,
            buying_power=707.43,
            equity=1558.11,
        ),
        is_account_snapshot_reliable=lambda: True,
    )

    with patch("src.engine.trading_engine.random.random", lambda: 0.9):
        engine._refresh_broker_snapshots(outside_trading_hours=True)

    assert engine._latest_position.quantity == 30
    assert engine._latest_account.cash == 707.61
    assert engine._position_shares == 30
    assert engine._entry_price == 18.09
    assert engine.risk.equity_updates == [1558.11]
    assert engine._last_signal_reason == "盘后仅同步真实持仓，不执行新交易"


def test_refresh_broker_snapshots_keeps_reduce_only_and_places_no_order():
    engine = TradingEngine(AppConfig(ticker="SOFI", mode="live"), ignore_trading_hours=False)
    use_test_pending_path(engine, "snapshot-no-order")
    engine.risk = FakeRisk()
    order_calls = []
    engine.broker = SimpleNamespace(
        get_position_for_ticker=lambda ticker: None,
        is_positions_snapshot_reliable=lambda: True,
        get_account=lambda: SimpleNamespace(
            cash=707.61,
            buying_power=707.43,
            equity=707.61,
        ),
        is_account_snapshot_reliable=lambda: True,
        place_order=lambda **kwargs: order_calls.append(kwargs),
    )
    engine._reduce_only = True

    engine._refresh_broker_snapshots(outside_trading_hours=True)

    assert engine._reduce_only is True
    assert order_calls == []
    assert engine._position_shares == 0


def run_test_direct():
    test_reconcile_pending_buy_fill_updates_local_state()
    test_reconcile_partial_buy_fill_keeps_pending_order()
    test_reconcile_pending_sell_fill_records_trade()
    test_reduce_only_blocks_new_buy_orders()
    test_buy_sizing_does_not_use_margin_buying_power()
    test_partial_immediate_sell_keeps_unfilled_position()
    test_pending_order_survives_engine_restart()
    test_live_startup_adopts_existing_broker_order()
    test_market_calendar_blocks_holidays_and_uses_early_close()
    test_live_anytime_override_cannot_bypass_market_calendar()
    test_sell_fill_blocks_stale_position_from_triggering_duplicate_sell()
    test_position_sync_fence_survives_restart()
    test_live_startup_safety_blocks_when_reduce_only_disabled()
    test_live_startup_safety_blocks_unreliable_account()
    test_cross_process_sell_lock_allows_only_one_engine()
    test_paper_sell_does_not_create_live_position_fence()


if __name__ == "__main__":
    run_test_direct()
