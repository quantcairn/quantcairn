"""
tests/test_order_rejection.py — verify that rejected orders are not re-submitted.

Scenarios:
  1. Insufficient buying power → order blocked, no re-submit
  2. Broker rejection → ticker enters cooldown, BUY signals skipped
  3. Cooldown expires → trading resumes
  4. Buying power recovers → blocked state lifted
  5. Pending order check prevents duplicate submission
"""
import json
import time
import unittest
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.order.order_state import (
    OrderStateManager,
    OrderState,
    FailedOrder,
    BlockedTicker,
)


class TestOrderStateManager(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.manager = OrderStateManager(
            ticker="YINN",
            mode="live",
            cooldown_seconds=2,
            state_dir=self._tmp,
        )

    def tearDown(self):
        shutil.rmtree(str(self._tmp), ignore_errors=True)

    # ---- 1. Basic lifecycle ----

    def test_initial_state_no_pending_no_block(self):
        self.assertFalse(self.manager.has_pending_order)
        self.assertFalse(self.manager.is_blocked)

    def test_record_submitted_creates_pending(self):
        self.manager.record_submitted("order-123", "BUY")
        self.assertTrue(self.manager.has_pending_order)
        self.assertEqual(self.manager.pending_order_id, "order-123")

    def test_record_filled_clears_pending(self):
        self.manager.record_submitted("order-123", "BUY")
        self.manager.record_filled("order-123")
        self.assertFalse(self.manager.has_pending_order)

    # ---- 2. Rejection creates cooldown ----

    def test_record_rejected_triggers_cooldown(self):
        self.manager.record_rejected(
            order_id="order-456",
            reason="The order amount exceeds the maximum buying power",
            quantity=11,
            price=25.35,
            buying_power=329.72,
        )
        self.assertTrue(self.manager.is_blocked)
        self.assertEqual(self.manager.failed_count_today, 1)
        self.assertIsNotNone(self.manager.blocked_until)
        self.assertIn("冷却", self.manager.blocked_reason)

    def test_rejected_order_no_longer_pending(self):
        self.manager.record_submitted("order-456", "BUY")
        self.manager.record_rejected("order-456", "insufficient buying power")
        self.assertFalse(self.manager.has_pending_order)

    # ---- 3. Cooldown blocks new orders ----

    def test_is_blocked_during_cooldown(self):
        self.manager.record_rejected("order-789", "buying power")
        self.assertTrue(self.manager.is_blocked)

    def test_cooldown_expires(self):
        # Create a block that has already expired
        self.manager._blocked = BlockedTicker(
            ticker="YINN",
            blocked_until=datetime.now() - timedelta(seconds=1),
            reason="expired test",
            buying_power_at_block=100.0,
        )
        self.assertFalse(self.manager.is_blocked)

    # ---- 4. Buying-power check ----

    def test_check_buying_power_passes(self):
        allowed, reason = self.manager.check_buying_power(
            price=25.0, quantity=10, available_cash=300.0, fee_buffer=5.0
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_check_buying_power_blocks(self):
        allowed, reason = self.manager.check_buying_power(
            price=25.0, quantity=10, available_cash=200.0, fee_buffer=5.0
        )
        self.assertFalse(allowed)
        self.assertIn("BUY_BLOCKED", reason)
        self.assertIn("insufficient buying power", reason)

    def test_check_buying_power_with_fees(self):
        allowed, reason = self.manager.check_buying_power(
            price=25.0, quantity=10, available_cash=254.0, fee_buffer=5.0
        )
        self.assertFalse(allowed)

    # ---- 5. Block clearing on BP change ----

    def test_clear_block_on_bp_change(self):
        self.manager.record_rejected(
            "order-999", "buying power",
            buying_power=100.0,
        )
        self.assertTrue(self.manager.is_blocked)
        cleared = self.manager.maybe_clear_block_on_bp_change(500.0)
        self.assertTrue(cleared)
        self.assertFalse(self.manager.is_blocked)

    def test_no_clear_when_bp_unchanged(self):
        self.manager.record_rejected(
            "order-999", "buying power",
            buying_power=100.0,
        )
        cleared = self.manager.maybe_clear_block_on_bp_change(100.0)
        self.assertFalse(cleared)
        self.assertTrue(self.manager.is_blocked)

    # ---- 6. Failed order tracking ----

    def test_failed_orders_accumulate(self):
        self.manager.record_rejected("o1", "reason 1")
        self.manager.record_rejected("o2", "reason 2")
        self.assertEqual(self.manager.failed_count_today, 2)
        self.assertEqual(len(self.manager.failed_orders_today), 2)
        self.assertEqual(self.manager.failed_orders_today[0].reason, "reason 1")

    def test_reset_daily_clears_failed(self):
        self.manager.record_rejected("o1", "reason 1")
        self.manager.record_rejected("o2", "reason 2")
        self.manager.reset_daily()
        self.assertEqual(self.manager.failed_count_today, 0)

    # ---- 7. No cooldown when disabled ----

    def test_no_cooldown_when_cooldown_zero(self):
        mgr = OrderStateManager(
            ticker="TEST", mode="paper", cooldown_seconds=0, state_dir=self._tmp,
        )
        mgr.record_rejected("o1", "reason")
        self.assertFalse(mgr.is_blocked)


class TestFailedOrderDataclass(unittest.TestCase):

    def test_roundtrip_serialization(self):
        fo = FailedOrder(
            ticker="YINN",
            timestamp=datetime(2026, 7, 8, 22, 43, 0),
            reason="exceeds buying power",
            quantity=11,
            price=25.35,
            buying_power=329.72,
        )
        d = fo.to_dict()
        restored = FailedOrder.from_dict(d)
        self.assertEqual(restored.ticker, fo.ticker)
        self.assertEqual(restored.reason, fo.reason)
        self.assertEqual(restored.quantity, 11)
        self.assertEqual(restored.buying_power, 329.72)


class TestBlockedTicker(unittest.TestCase):

    def test_expired_detection(self):
        bt = BlockedTicker(
            ticker="YINN",
            blocked_until=datetime.now() - timedelta(seconds=10),
            reason="test",
            buying_power_at_block=100.0,
        )
        self.assertTrue(bt.is_expired)
        self.assertEqual(bt.remaining_seconds, 0)

    def test_active_detection(self):
        bt = BlockedTicker(
            ticker="YINN",
            blocked_until=datetime.now() + timedelta(minutes=60),
            reason="test",
            buying_power_at_block=100.0,
        )
        self.assertFalse(bt.is_expired)
        self.assertGreater(bt.remaining_seconds, 0)


class TestIntegration_OrderDedup(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(str(self._tmp), ignore_errors=True)

    def test_no_duplicate_after_rejection(self):
        mgr = OrderStateManager(
            ticker="YINN", mode="live", cooldown_seconds=3600, state_dir=self._tmp,
        )
        mgr.record_submitted("order-1", "BUY")
        mgr.record_rejected(
            order_id="order-1",
            reason="The order amount exceeds the maximum buying power",
            quantity=11,
            price=25.35,
            buying_power=329.72,
        )
        self.assertTrue(mgr.is_blocked, "Should be blocked after rejection")
        self.assertFalse(mgr.has_pending_order, "Pending order should be cleared")
        self.assertIn("冷却", mgr.blocked_reason)

    def test_buy_signal_resumes_after_bp_recovery(self):
        mgr = OrderStateManager(
            ticker="YINN", mode="live", cooldown_seconds=3600, state_dir=self._tmp,
        )
        mgr.record_rejected(
            order_id="order-2",
            reason="insufficient buying power",
            buying_power=329.72,
        )
        self.assertTrue(mgr.is_blocked)
        mgr.maybe_clear_block_on_bp_change(800.00)
        self.assertFalse(mgr.is_blocked)


if __name__ == "__main__":
    unittest.main()
