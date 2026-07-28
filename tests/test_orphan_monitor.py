import tempfile
from pathlib import Path
import json
import math

import pytest

from src.broker.base import AccountInfo, Order, OrderSide, OrderStatus, OrderType, Position
from src.engine.orphan_monitor import OrphanPositionMonitor, should_run_orphan_monitor
from src.engine.trading_engine import TradingEngine, check_exit_conditions


@pytest.fixture(autouse=True)
def _isolate_remote_trade_notifications(monkeypatch):
    for name in (
        "SOXS_TELEGRAM_BOT_TOKEN",
        "SOXS_TELEGRAM_CHAT_ID",
        "SOXS_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)


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


class FakeNotifier:
    def __init__(self):
        self.trades = []
        self.order_submitted_calls = []
        self.alerts = []

    def trade(self, ticker, side, quantity, price, pnl=None, mode=None, **kwargs):
        self.trades.append(
            {
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "price": price,
                "pnl": pnl,
                "mode": mode,
            }
        )

    def order_submitted(self, *args, **kwargs):
        self.order_submitted_calls.append((args, kwargs))

    def alert(self, message, level="info"):
        self.alerts.append((message, level))


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


@pytest.mark.parametrize(
    ("price", "reason"),
    [
        (94.99, "stop_loss"),
        (95.00, "stop_loss"),
        (110.00, "take_profit"),
        (110.01, "take_profit"),
    ],
)
def test_normal_stock_exit_boundaries_trigger(price, reason):
    result = check_exit_conditions("AAPL", price, 100.0, 10)
    assert result["should_exit"] is True
    assert result["reason"] == reason


@pytest.mark.parametrize("price", [95.01, 109.99])
def test_normal_stock_exit_boundaries_do_not_trigger(price):
    result = check_exit_conditions("AAPL", price, 100.0, 10)
    assert result["should_exit"] is False
    assert result["reason"] is None


@pytest.mark.parametrize(
    ("symbol", "price", "reason"),
    [
        ("SOXS", 105.00, "stop_loss"),
        ("SOXS", 105.01, "stop_loss"),
        ("SOXS", 90.00, "take_profit"),
        ("SOXS", 89.99, "take_profit"),
        ("soxs", 105.00, "stop_loss"),
        (" SOXS ", 90.00, "take_profit"),
    ],
)
def test_soxs_special_exit_boundaries_trigger(symbol, price, reason):
    result = check_exit_conditions(symbol, price, 100.0, 10, is_inverse_etf=True)
    assert result["should_exit"] is True
    assert result["reason"] == reason


@pytest.mark.parametrize("price", [104.99, 90.01])
def test_soxs_special_exit_boundaries_do_not_trigger(price):
    result = check_exit_conditions("SOXS", price, 100.0, 10, is_inverse_etf=True)
    assert result["should_exit"] is False
    assert result["reason"] is None


@pytest.mark.parametrize(
    ("current_price", "avg_cost"),
    [
        (None, 100.0),
        (100.0, None),
        (0, 100.0),
        (100.0, 0),
        (math.nan, 100.0),
        (100.0, math.nan),
        (-1.0, 100.0),
        (100.0, -1.0),
        ("not-a-price", 100.0),
        (100.0, "not-a-cost"),
    ],
)
def test_exit_conditions_ignore_invalid_price_inputs(current_price, avg_cost):
    result = check_exit_conditions("SOXS", current_price, avg_cost, 10, is_inverse_etf=True)
    assert result["should_exit"] is False
    assert result["reason"] is None


def test_orphan_normal_stock_stop_loss_triggers():
    result = check_exit_conditions("PLTR", 92.0, 100.0, 5, mode="orphan")
    assert result["should_exit"] is True
    assert result["reason"] == "stop_loss"


def test_orphan_soxs_stop_loss_triggers():
    result = check_exit_conditions("SOXS", 105.0, 100.0, 5, is_inverse_etf=True, mode="orphan")
    assert result["should_exit"] is True
    assert result["reason"] == "stop_loss"


def test_orphan_soxs_take_profit_triggers():
    result = check_exit_conditions("SOXS", 90.0, 100.0, 5, is_inverse_etf=True, mode="orphan")
    assert result["should_exit"] is True
    assert result["reason"] == "take_profit"


def test_soxl_is_not_treated_as_inverse_etf():
    from src.engine.trading_engine import is_inverse_etf_symbol

    assert is_inverse_etf_symbol("SOXL") is False


def test_orphan_monitor_never_submits_buy():
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("PLTR", 2, 100.0, 92.0)
    engine = monitor._engine_for_symbol("PLTR")
    _use_test_state(engine, "never-buy")

    monitor._evaluate_symbol("PLTR", pos)

    assert len(broker.orders) == 1
    assert broker.orders[0]["side"] == OrderSide.SELL


def test_orphan_monitor_startup_does_not_depend_on_live_top_configs(monkeypatch, tmp_path):
    import src.engine.orphan_monitor as module

    monkeypatch.delenv("SOXS_DISABLE_ORPHAN_MONITOR", raising=False)
    monkeypatch.setattr(module, "TOP_CONFIGS", [tmp_path / "TOP1.yaml", tmp_path / "TOP2.yaml"])
    for path in module.TOP_CONFIGS:
        path.write_text("enabled: false\nticker:\nmode: paper\n", encoding="utf-8")

    assert should_run_orphan_monitor() is True


def test_orphan_monitor_startup_respects_explicit_disable(monkeypatch, tmp_path):
    import src.engine.orphan_monitor as module

    monkeypatch.setenv("SOXS_DISABLE_ORPHAN_MONITOR", "1")
    monkeypatch.setattr(module, "TOP_CONFIGS", [tmp_path / "TOP1.yaml"])
    module.TOP_CONFIGS[0].write_text("ticker: SOXS\nmode: live\n", encoding="utf-8")

    assert should_run_orphan_monitor() is False


def test_orphan_monitor_internal_engine_is_reduce_only():
    monitor = OrphanPositionMonitor(broker=FakeBroker())
    engine = monitor._engine_for_symbol("PLTR")

    assert engine.config.position.reduce_only is True


@pytest.mark.parametrize(
    ("current_price", "reason"),
    [
        (105.0, "stop_loss"),
        (90.0, "take_profit"),
    ],
)
def test_orphan_monitor_soxs_special_exits_submit_sell(current_price, reason):
    broker = FakeBroker(positions=[_position("SOXS", 7, 100.0, current_price)])
    monitor = OrphanPositionMonitor(broker=broker)
    # B4: mock TOP engine offline so SOXS is treated as an orphan
    monitor._active_assigned_symbols = lambda: set()
    pos = _position("SOXS", 7, 100.0, current_price)
    engine = monitor._engine_for_symbol("SOXS")
    _use_test_state(engine, f"soxs-{reason}")

    monitor._evaluate_symbol("SOXS", pos)

    assert len(broker.orders) == 1
    assert broker.orders[0]["side"] == OrderSide.SELL
    assert broker.orders[0]["quantity"] == 7
    assert broker.orders[0]["notes"] == f"orphan:{reason}"


def test_orphan_exit_trade_notification_uses_live_mode():
    broker = FakeBroker(positions=[_position("SOXS", 7, 100.0, 105.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    # B4: mock TOP engine offline so SOXS is treated as an orphan
    monitor._active_assigned_symbols = lambda: set()
    pos = _position("SOXS", 7, 100.0, 105.0)
    engine = monitor._engine_for_symbol("SOXS")
    engine.notifier = FakeNotifier()
    _use_test_state(engine, "soxs-live-mode-notification")

    monitor._evaluate_symbol("SOXS", pos)

    assert engine.notifier.trades
    assert engine.notifier.trades[0]["mode"] == "live"


def test_orphan_monitor_soxs_pending_sell_does_not_repeat():
    broker = FakeBroker(positions=[_position("SOXS", 7, 100.0, 105.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("SOXS", 7, 100.0, 105.0)
    engine = monitor._engine_for_symbol("SOXS")
    _use_test_state(engine, "soxs-pending-sell")
    engine._pending_order = {"side": "SELL", "order_id": "PENDING"}

    monitor._evaluate_symbol("SOXS", pos)

    assert broker.orders == []


def test_orphan_monitor_does_not_take_profit():
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("PLTR", 2, 100.0, 112.0)
    engine = monitor._engine_for_symbol("PLTR")
    _use_test_state(engine, "no-take-profit")

    monitor._evaluate_symbol("PLTR", pos)

    assert broker.orders == []


def test_orphan_monitor_uses_report_range_take_profit(monkeypatch):
    import src.engine.orphan_monitor as module

    monkeypatch.setattr(
        module,
        "load_latest_ai_selection_state",
        lambda _root: {
            "selection_date": "2026-07-07",
            "protected_positions": [
                {
                    "ticker": "NVDA",
                    "range_low": 190.0,
                    "range_high": 200.0,
                    "protected_position": True,
                }
            ],
        },
    )
    broker = FakeBroker()
    monitor = OrphanPositionMonitor(broker=broker)
    pos = _position("NVDA", 2, 195.0, 200.0)
    engine = monitor._engine_for_symbol("NVDA")
    _use_test_state(engine, "report-take-profit")

    monitor._evaluate_symbol("NVDA", pos)

    assert len(broker.orders) == 1
    assert broker.orders[0]["side"] == OrderSide.SELL
    assert broker.orders[0]["notes"] == "orphan_range:take_profit"


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
    # Mock the status fetch to always return None (simulate offline process)
    monitor._fetch_engine_status = lambda _port: None
    import src.engine.orphan_monitor as module

    original = module._load_configured_assignments
    module._load_configured_assignments = lambda: {
        8091: {
            "ticker": "PLTR",
            "expected_mode": "live",
            "expected_environment": "prod",
            "expected_account_type": "live",
        }
    }
    try:
        assert "PLTR" not in monitor.scan_orphans([pos])
        assert "PLTR" not in monitor.scan_orphans([pos])
        assert "PLTR" in monitor.scan_orphans([pos])
    finally:
        module._load_configured_assignments = original


# ── Orphan identity verification tests ────────────────────────────────

def test_identity_correct_ticker_mode_account_returns_active():
    """Correct ticker, mode, environment, account_type → ASSIGNED_ACTIVE."""
    monitor = OrphanPositionMonitor(broker=FakeBroker())
    monitor._fetch_engine_status = lambda _port: {
        "running": True,
        "ticker": "SOXS",
        "execution_mode": "live",
        "broker_environment": "prod",
        "account_type": "live",
    }
    expected = {
        "ticker": "SOXS",
        "expected_mode": "live",
        "expected_environment": "prod",
        "expected_account_type": "live",
    }
    status = monitor._verify_engine_identity(8091, expected)
    assert status == "ASSIGNED_ACTIVE"


def test_identity_wrong_ticker_returns_unverified():
    """Port online but wrong ticker → ASSIGNED_UNVERIFIED."""
    monitor = OrphanPositionMonitor(broker=FakeBroker())
    monitor._fetch_engine_status = lambda _port: {
        "running": True,
        "ticker": "LABD",
        "execution_mode": "live",
        "broker_environment": "prod",
        "account_type": "live",
    }
    expected = {
        "ticker": "SOXS",
        "expected_mode": "live",
        "expected_environment": "prod",
        "expected_account_type": "live",
    }
    status = monitor._verify_engine_identity(8091, expected)
    assert status == "ASSIGNED_UNVERIFIED"


def test_identity_sandbox_vs_live_mode_mismatch_returns_unverified():
    """Ticker correct but live vs sandbox mode mismatch → ASSIGNED_UNVERIFIED."""
    monitor = OrphanPositionMonitor(broker=FakeBroker())
    monitor._fetch_engine_status = lambda _port: {
        "running": True,
        "ticker": "SOXS",
        "execution_mode": "sandbox",
        "broker_environment": "sandbox",
        "account_type": "paper",
    }
    expected = {
        "ticker": "SOXS",
        "expected_mode": "live",
        "expected_environment": "prod",
        "expected_account_type": "live",
    }
    status = monitor._verify_engine_identity(8091, expected)
    assert status == "ASSIGNED_UNVERIFIED"


def test_identity_account_type_mismatch_returns_unverified():
    """Account type paper vs live → ASSIGNED_UNVERIFIED."""
    monitor = OrphanPositionMonitor(broker=FakeBroker())
    monitor._fetch_engine_status = lambda _port: {
        "running": True,
        "ticker": "SOXS",
        "execution_mode": "live",
        "broker_environment": "prod",
        "account_type": "paper",
    }
    expected = {
        "ticker": "SOXS",
        "expected_mode": "live",
        "expected_environment": "prod",
        "expected_account_type": "live",
    }
    status = monitor._verify_engine_identity(8091, expected)
    assert status == "ASSIGNED_UNVERIFIED"


def test_identity_single_failure_does_not_takeover():
    """One fetch failure → unverified but ticker still assigned."""
    pos = _position("SOXS", 5, 100.0, 105.0)
    broker = FakeBroker(positions=[pos])
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._startup_at = 0
    monitor._fetch_engine_status = lambda _port: None
    import src.engine.orphan_monitor as module

    original = module._load_configured_assignments
    module._load_configured_assignments = lambda: {
        8091: {
            "ticker": "SOXS",
            "expected_mode": "live",
            "expected_environment": "prod",
            "expected_account_type": "live",
        }
    }
    try:
        # Single failure — SOXS still assigned, not orphaned
        orphans = monitor.scan_orphans([pos])
        assert "SOXS" not in orphans
    finally:
        module._load_configured_assignments = original


def test_identity_three_consecutive_failures_confirms_orphan():
    """Three consecutive fetch failures → ORPHAN_CONFIRMED."""
    pos = _position("SOXS", 5, 100.0, 105.0)
    broker = FakeBroker(positions=[pos])
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._startup_at = 0
    monitor._fetch_engine_status = lambda _port: None
    import src.engine.orphan_monitor as module

    original = module._load_configured_assignments
    module._load_configured_assignments = lambda: {
        8091: {
            "ticker": "SOXS",
            "expected_mode": "live",
            "expected_environment": "prod",
            "expected_account_type": "live",
        }
    }
    try:
        assert "SOXS" not in monitor.scan_orphans([pos])
        assert "SOXS" not in monitor.scan_orphans([pos])
        orphans = monitor.scan_orphans([pos])
        assert "SOXS" in orphans
    finally:
        module._load_configured_assignments = original


def test_identity_owner_recovery_resets_failure_count():
    """After recovery (identity match), failure count resets."""
    pos = _position("SOXS", 5, 100.0, 105.0)
    broker = FakeBroker(positions=[pos])
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._startup_at = 0

    # Start with the engine offline (no fetch)
    monitor._fetch_engine_status = lambda _port: None
    import src.engine.orphan_monitor as module

    original = module._load_configured_assignments
    module._load_configured_assignments = lambda: {
        8091: {
            "ticker": "SOXS",
            "expected_mode": "live",
            "expected_environment": "prod",
            "expected_account_type": "live",
        }
    }
    try:
        # Two failures — still assigned
        assert "SOXS" not in monitor.scan_orphans([pos])
        assert "SOXS" not in monitor.scan_orphans([pos])
        assert monitor._assignment_failures.get(8091, 0) == 2

        # Engine comes back online with correct identity
        monitor._fetch_engine_status = lambda _port: {
            "running": True,
            "ticker": "SOXS",
            "execution_mode": "live",
            "broker_environment": "prod",
            "account_type": "live",
        }
        # This should reset failure count
        orphans = monitor.scan_orphans([pos])
        assert "SOXS" not in orphans
        assert monitor._assignment_failures.get(8091, 0) == 0
    finally:
        module._load_configured_assignments = original


def test_identity_pending_sell_prevents_duplicate():
    """Even when orphan confirmed, pending sell blocks duplicate submission."""
    broker = FakeBroker(positions=[_position("SOXS", 7, 100.0, 105.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._active_assigned_symbols = lambda: set()  # Force orphan
    pos = _position("SOXS", 7, 100.0, 105.0)
    engine = monitor._engine_for_symbol("SOXS")
    engine._pending_order_state_path = Path(tempfile.gettempdir()) / "soxs-identity-pending-order.json"
    engine._position_sync_state_path = Path(tempfile.gettempdir()) / "soxs-identity-pending-sync.json"
    engine._sell_lock_path = Path(tempfile.gettempdir()) / "soxs-identity-pending-sell.lock"
    engine._pending_order_state_path.unlink(missing_ok=True)
    engine._position_sync_state_path.unlink(missing_ok=True)
    engine._sell_lock_path.unlink(missing_ok=True)
    engine._pending_order = {"side": "SELL", "order_id": "PENDING-001"}
    engine._position_sync_fence = None

    monitor._evaluate_symbol("SOXS", pos)

    assert broker.orders == []


def test_identity_unreachable_engine_returns_unverified():
    """Unreachable engine → ASSIGNED_UNVERIFIED."""
    monitor = OrphanPositionMonitor(broker=FakeBroker())
    monitor._fetch_engine_status = lambda _port: None
    expected = {
        "ticker": "SOXS",
        "expected_mode": "live",
        "expected_environment": "prod",
        "expected_account_type": "live",
    }
    status = monitor._verify_engine_identity(8091, expected)
    assert status == "ASSIGNED_UNVERIFIED"


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
    test_soxl_is_not_treated_as_inverse_etf()
    test_orphan_normal_stock_stop_loss_triggers()
    test_orphan_soxs_stop_loss_triggers()
    test_orphan_monitor_never_submits_buy()
    test_orphan_monitor_does_not_take_profit()
    test_orphan_monitor_uses_report_range_take_profit()
    test_broker_position_verification_failure_skips_symbol()
    test_startup_safety_requires_reliable_account_snapshot()
    test_startup_safety_accepts_verified_positions_and_account()
    test_existing_sell_lock_prevents_duplicate_sell()
    test_position_qty_zero_stops_orphan_monitoring()
    test_offline_assigned_process_becomes_orphan_after_three_failures()
    test_identity_correct_ticker_mode_account_returns_active()
    test_identity_wrong_ticker_returns_unverified()
    test_identity_sandbox_vs_live_mode_mismatch_returns_unverified()
    test_identity_account_type_mismatch_returns_unverified()
    test_identity_single_failure_does_not_takeover()
    test_identity_three_consecutive_failures_confirms_orphan()
    test_identity_owner_recovery_resets_failure_count()
    test_identity_pending_sell_prevents_duplicate()
    test_identity_unreachable_engine_returns_unverified()
    test_market_hours_check_prevents_execution_outside_regular_hours()
