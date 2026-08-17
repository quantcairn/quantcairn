"""Adversarial coverage for the final LongBridge mutation boundary."""

import json
from types import SimpleNamespace

import pytest

from src.broker.base import OrderSide, OrderStatus, Position
from src.engine.orphan_monitor import OrphanPositionMonitor
from src.broker.longbridge_broker import (
    DEFAULT_PROD_HTTP_URL,
    DEFAULT_PROD_QUOTE_WS_URL,
    DEFAULT_PROD_TRADE_WS_URL,
    LongBridgeBroker,
)


class FakeTradeContext:
    def __init__(self):
        self.submit_calls = []
        self.cancel_calls = []

    def submit_order(self, **kwargs):
        self.submit_calls.append(kwargs)
        return SimpleNamespace(order_id="fake-order-1", status="submitted")

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return SimpleNamespace(status="cancelled")


def _broker(tmp_path, monkeypatch, *, mode: str, armed: str = "", kill_file=None):
    monkeypatch.setenv("LONGBRIDGE_ENV", "sandbox")
    monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", mode)
    monkeypatch.setenv("QUANTCAIRN_LIVE_ARMED", armed)
    if kill_file is None:
        monkeypatch.delenv("QUANTCAIRN_LIVE_KILL_SWITCH_FILE", raising=False)
    else:
        monkeypatch.setenv("QUANTCAIRN_LIVE_KILL_SWITCH_FILE", str(kill_file))

    broker = LongBridgeBroker(
        app_key="fake-key",
        app_secret="fake-secret",
        access_token="fake-token",
        account_type="paper",
        environment="sandbox",
        http_url=DEFAULT_PROD_HTTP_URL,
        quote_ws_url=DEFAULT_PROD_QUOTE_WS_URL,
        trade_ws_url=DEFAULT_PROD_TRADE_WS_URL,
        audit_dir=str(tmp_path / "audit"),
        execution_mode=mode,
    )
    broker._connected = True
    broker._sandbox_first_run_confirmed = True
    broker._trade_ctx = FakeTradeContext()
    return broker


@pytest.mark.parametrize(
    ("mode", "armed"),
    [
        ("PAPER", "YES"),
        ("RESEARCH", ""),
        ("LIVE_OBSERVE_ONLY", "YES"),
        ("LIVE", "YES"),
        ("LIVE_EXECUTION", ""),
        ("LIVE_EXECUTION", "NO"),
        ("LIVE_EXECUTION", "false"),
        ("LIVE_EXECUTION", "yes"),
        ("LIV", "YES"),
        ("", "YES"),
    ],
)
def test_direct_sandbox_broker_denies_all_unauthorized_mutations(
    tmp_path, monkeypatch, mode, armed
):
    broker = _broker(tmp_path, monkeypatch, mode=mode, armed=armed)

    place = broker.place_order("TEST", OrderSide.SELL, 1)
    cancelled = broker.cancel_order("remote-order")

    assert place.status is OrderStatus.REJECTED
    assert cancelled is False
    assert broker._trade_ctx.submit_calls == []
    assert broker._trade_ctx.cancel_calls == []


def test_direct_sandbox_broker_requires_valid_kill_switch(tmp_path, monkeypatch):
    kill_file = tmp_path / "kill-switch.json"
    kill_file.write_text(json.dumps({"state": "CLOSED"}), encoding="utf-8")
    broker = _broker(
        tmp_path,
        monkeypatch,
        mode="LIVE_EXECUTION",
        armed="YES",
        kill_file=kill_file,
    )

    place = broker.place_order("TEST", OrderSide.SELL, 1)
    cancelled = broker.cancel_order("remote-order")

    assert place.status is OrderStatus.REJECTED
    assert cancelled is False
    assert broker._trade_ctx.submit_calls == []
    assert broker._trade_ctx.cancel_calls == []


def test_authorized_fake_control_reaches_only_fake_sdk(tmp_path, monkeypatch):
    kill_file = tmp_path / "kill-switch.json"
    kill_file.write_text(json.dumps({"state": "OPEN"}), encoding="utf-8")
    broker = _broker(
        tmp_path,
        monkeypatch,
        mode="LIVE_EXECUTION",
        armed="YES",
        kill_file=kill_file,
    )

    place = broker.place_order("TEST", OrderSide.SELL, 1)
    cancelled = broker.cancel_order("remote-order")

    assert place.status is OrderStatus.PENDING
    assert cancelled is True
    assert len(broker._trade_ctx.submit_calls) == 1
    assert len(broker._trade_ctx.cancel_calls) == 1


def test_authorizer_exception_fails_closed_before_sdk(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch, mode="PAPER", armed="YES")

    def raise_authorizer(*args, **kwargs):
        raise RuntimeError("parser failure")

    monkeypatch.setattr("src.broker.longbridge_broker.authorize_mutation", raise_authorizer)
    place = broker.place_order("TEST", OrderSide.SELL, 1)
    cancelled = broker.cancel_order("remote-order")

    assert place.status is OrderStatus.REJECTED
    assert place.notes == "AUTHORIZATION_ERROR"
    assert cancelled is False
    assert broker._trade_ctx.submit_calls == []
    assert broker._trade_ctx.cancel_calls == []


def test_orphan_reduce_path_reaches_final_broker_gate(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch, mode="PAPER", armed="YES")
    monitor = OrphanPositionMonitor(broker=broker)
    monitor._active_assigned_symbols = lambda: set()

    class FakeOrphanEngine:
        def _reconcile_pending_order(self):
            return None

        def _apply_position_sync_fence(self, quantity):
            return quantity

        def _has_active_sell_protection(self):
            return False

        def _submit_reduce_order(self, **kwargs):
            return broker.place_order("SOXS", OrderSide.SELL, kwargs["quantity"])

    monitor._engine_for_symbol = lambda symbol: FakeOrphanEngine()
    monitor._evaluate_symbol(
        "SOXS",
        Position(
            ticker="SOXS",
            quantity=3,
            avg_entry_price=100.0,
            current_price=105.0,
            market_value=315.0,
            unrealized_pnl=15.0,
            unrealized_pnl_pct=5.0,
        ),
    )

    assert broker._trade_ctx.submit_calls == []
    assert broker._trade_ctx.cancel_calls == []
