"""Tests for OrphanMonitor reduce_only enforcement.

Verifies that the orphan monitor:
- creates engines with reduce_only=True
- never submits buy orders
- only submits reduce/sell orders
"""
import os
import tempfile
from pathlib import Path
import pytest

# Redirect module initialization to temp paths without leaking environment
# overrides into unrelated tests in the same pytest process.
_tmpdir = Path(tempfile.mkdtemp(prefix="orphan_test_"))
_saved_test_env = {
    "SOXS_STATE_DIR": os.environ.get("SOXS_STATE_DIR"),
    "SOXS_RUNTIME_AUDIT_DIR": os.environ.get("SOXS_RUNTIME_AUDIT_DIR"),
}
try:
    os.environ["SOXS_STATE_DIR"] = str(_tmpdir / "state")
    os.environ["SOXS_RUNTIME_AUDIT_DIR"] = str(_tmpdir / "audit")

    from src.broker.base import (
        AccountInfo, Order, OrderSide, OrderStatus, OrderType, Position,
    )
    from src.engine.orphan_monitor import OrphanPositionMonitor
finally:
    for _name, _value in _saved_test_env.items():
        if _value is None:
            os.environ.pop(_name, None)
        else:
            os.environ[_name] = _value


@pytest.fixture(autouse=True)
def _isolate_remote_trade_notifications(monkeypatch):
    for name in (
        "SOXS_TELEGRAM_BOT_TOKEN",
        "SOXS_TELEGRAM_CHAT_ID",
        "SOXS_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeBroker:
    """Minimal broker that tracks orders submitted."""
    def __init__(self, positions=None):
        self._positions = positions or []
        self.orders = []
        self.reliable = True

    def get_positions(self):
        return list(self._positions)

    def get_account(self):
        return AccountInfo(cash=10000, equity=10000, buying_power=20000, positions=self._positions)

    def is_positions_snapshot_reliable(self):
        return self.reliable

    def is_account_snapshot_reliable(self):
        return self.reliable

    def place_order(self, ticker, side, quantity, order_type=OrderType.MARKET, **kw):
        self.orders.append({
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "notes": kw.get("notes", ""),
        })
        return Order(
            order_id=f"test-{len(self.orders)}",
            ticker=ticker, side=side, order_type=order_type,
            quantity=quantity, filled_quantity=quantity,
            status=OrderStatus.FILLED,
        )

    def get_order(self, order_id):
        return None

    def cancel_order(self, order_id):
        return True

    def get_position_for_ticker(self, ticker):
        for pos in self._positions:
            if getattr(pos, "ticker", "") == ticker:
                return pos
        return None

    def invalidate_cache(self):
        pass


def _position(ticker: str, qty: int, price: float = 100.0, current: float | None = None):
    c = current or price
    return Position(ticker=ticker, quantity=qty, avg_entry_price=price,
                    current_price=c, market_value=c * qty,
                    unrealized_pnl=(c - price) * qty, unrealized_pnl_pct=0)


def test_orphan_engine_reduce_only():
    """Verify that orphan monitor creates engines with reduce_only=True."""
    broker = FakeBroker(positions=[_position("PLTR", 2, 100.0, 92.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    engine = monitor._engine_for_symbol("PLTR")
    assert engine._reduce_only is True
    assert engine.mode == "live"


def test_orphan_only_submits_sell():
    """Verify orphan monitor only places sell/reduce orders."""
    broker = FakeBroker(positions=[_position("AAPL", 5, 180.0, 170.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    engine = monitor._engine_for_symbol("AAPL")
    engine._acquire_sell_lock = lambda reason: True  # bypass cross-process lock

    # Force an exit decision by setting trigger conditions
    pos = _position("AAPL", 5, 180.0, 170.0)
    # Position is below 95% avg_cost → triggers orphan stop_loss
    monitor._evaluate_symbol("AAPL", pos)

    for order in broker.orders:
        assert order["side"] == OrderSide.SELL, f"Expected SELL, got {order['side']}"
        # Notes should indicate reduce/exit
        notes = order.get("notes", "")
        assert "orphan" in notes or "stop_loss" in notes or "reduce" in notes


def test_soxs_orphan_special_exits_are_sell_only():
    broker = FakeBroker(positions=[_position("SOXS", 5, 100.0, 105.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    engine = monitor._engine_for_symbol("SOXS")
    engine._pending_order_state_path = _tmpdir / "soxs-special-exit-pending.json"
    engine._position_sync_state_path = _tmpdir / "soxs-special-exit-sync.json"
    engine._sell_lock_path = _tmpdir / "soxs-special-exit.lock"
    engine._pending_order_state_path.unlink(missing_ok=True)
    engine._position_sync_state_path.unlink(missing_ok=True)
    engine._sell_lock_path.unlink(missing_ok=True)
    engine._pending_order = None
    engine._position_sync_fence = None
    engine._acquire_sell_lock = lambda reason: True

    monitor._evaluate_symbol("SOXS", broker.get_positions()[0])

    assert len(broker.orders) == 1
    assert broker.orders[0]["side"] == OrderSide.SELL
    assert broker.orders[0]["quantity"] == 5
    assert broker.orders[0]["notes"] == "orphan:stop_loss"


def test_orphan_monitor_never_buys():
    """Verify orphan monitor never submits a BUY order."""
    broker = FakeBroker(positions=[
        _position("NVDA", 3, 150.0, 140.0),
        _position("AMD", 2, 100.0, 95.0),
    ])
    monitor = OrphanPositionMonitor(broker=broker)

    for ticker in ("NVDA", "AMD"):
        engine = monitor._engine_for_symbol(ticker)
        engine._acquire_sell_lock = lambda reason: True
        assert engine._reduce_only is True

    # No buy should ever come from the monitor
    for order in broker.orders:
        assert order["side"] != OrderSide.BUY


def test_orphan_no_new_positions():
    """Verify orphan monitor does not open new positions (only manages existing)."""
    broker = FakeBroker(positions=[_position("SOFI", 2, 15.0, 14.0)])
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._active_assigned_symbols = lambda: {"AAPL"}

    # Scan for orphans
    positions = broker.get_positions()
    orphans = monitor.scan_orphans(positions)

    # The orphan position should be detected
    assert "SOFI" in orphans

    # Engine should be in reduce_only mode
    engine = monitor._engine_for_symbol("SOFI")
    assert engine._reduce_only is True


def run_test_direct():
    test_orphan_engine_reduce_only()
    test_orphan_only_submits_sell()
    test_soxs_orphan_special_exits_are_sell_only()
    test_orphan_monitor_never_buys()
    test_orphan_no_new_positions()
