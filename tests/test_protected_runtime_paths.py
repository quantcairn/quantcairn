"""Regression coverage for protected mutable runtime state isolation."""

import json
from pathlib import Path

import pytest

from src.order.order_state import OrderStateManager
from src.safety import live_guard as live_guard_module


def test_order_state_writes_to_runtime_root_not_fake_source(tmp_path, monkeypatch):
    source_root = tmp_path / "fake-source"
    runtime_root = tmp_path / "runtime"
    source_root.mkdir()
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(source_root))
    monkeypatch.setenv("SOXS_STATE_DIR", str(runtime_root / "state"))

    manager = OrderStateManager("AAPL", mode="live", cooldown_seconds=60)
    manager.record_rejected("order-1", "test rejection")

    assert (runtime_root / "state" / "order_state_test" / "AAPL.json").exists()
    assert not list(source_root.rglob("*"))


def test_live_guard_reads_selection_state_from_runtime_root(tmp_path, monkeypatch):
    source_root = tmp_path / "fake-source"
    runtime_state = tmp_path / "runtime-state"
    source_root.mkdir()
    runtime_state.mkdir()
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(source_root))
    monkeypatch.setenv("SOXS_STATE_DIR", str(runtime_state))
    monkeypatch.setattr(live_guard_module, "PROJECT_DIR", source_root)

    (runtime_state / "ai_selection_state.json").write_text(
        json.dumps({"et_date": "2026-08-23", "selected_symbols": []}),
        encoding="utf-8",
    )
    guard = live_guard_module.LiveGuard()
    guard._context = {}
    monkeypatch.setattr(live_guard_module, "_required_selection_day", lambda: "2026-08-23")
    guard._check_selection_state()

    assert guard._errors == []
    assert not list(source_root.rglob("*"))


def test_trading_engine_audit_uses_runtime_log_root(tmp_path, monkeypatch):
    engine_module = pytest.importorskip("src.engine.trading_engine")
    source_root = tmp_path / "fake-source"
    runtime_logs = tmp_path / "runtime-logs"
    source_root.mkdir()
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(source_root))
    monkeypatch.setenv("SOXS_RUNTIME_AUDIT_DIR", str(runtime_logs))
    monkeypatch.setenv("SOXS_LOG_DIR", str(runtime_logs))
    monkeypatch.setenv("SOXS_LOGS_DIR", str(runtime_logs))

    engine_module.append_runtime_audit({"phase": "path-isolation-test"})

    assert list(runtime_logs.glob("trades-*.jsonl"))
    assert not list(source_root.rglob("*"))


def test_longbridge_cache_uses_runtime_state_root(tmp_path, monkeypatch):
    pytest.importorskip("longbridge.openapi")
    from src.broker.longbridge_broker import LongBridgeBroker

    source_root = tmp_path / "fake-source"
    runtime_state = tmp_path / "runtime-state"
    source_root.mkdir()
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(source_root))
    monkeypatch.setenv("SOXS_STATE_DIR", str(runtime_state))
    broker = LongBridgeBroker(environment="sandbox")

    assert broker._shared_snapshot_dir == (runtime_state / "broker_cache").resolve()
    assert broker._shared_snapshot_dir.exists()
    assert not list(source_root.rglob("*"))
