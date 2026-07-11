from __future__ import annotations

import json
from pathlib import Path

from src.strategy.state_store import StrategyStateStore


def test_state_store_saves_and_loads_atomically(tmp_path):
    store = StrategyStateStore(tmp_path / "state")
    payload = {
        "strategy_version": "dynamic_range_v1",
        "active_range": {"center": 10.0, "support": 9.6, "resistance": 10.4},
        "range_timestamp": "2026-07-11T09:30:00-04:00",
        "entry_layers": [{"layer_id": 1, "status": "planned"}],
        "exit_layers": [{"layer_id": 1, "status": "planned"}],
        "realized_pnl": 12.5,
        "unrealized_pnl": 2.25,
        "inventory_ratio": 0.25,
        "trend_guard_state": {"regime": "RANGE"},
        "symbol_reduce_only": False,
        "last_buy_time": "2026-07-11T10:00:00-04:00",
        "last_sell_time": None,
        "cooldown_until": "2026-07-11T10:30:00-04:00",
        "last_reconciliation_time": "2026-07-11T10:35:00-04:00",
        "broker_position_snapshot": {"quantity": 10, "avg_entry_price": 9.9},
        "state_version": 7,
    }

    path = store.save("soxs", payload)
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()

    loaded = store.load("SOXS")
    assert loaded is not None
    assert loaded["strategy_version"] == "dynamic_range_v1"
    assert loaded["active_range"]["support"] == 9.6
    assert loaded["entry_layers"][0]["layer_id"] == 1
    assert loaded["state_version"] == 7
    assert loaded["schema_version"] == 1
    assert loaded["symbol"] == "SOXS"


def test_state_store_lists_symbols_and_deletes(tmp_path):
    store = StrategyStateStore(tmp_path / "state")
    store.save("SOXS", {"strategy_version": "v1"})
    store.save("YINN", {"strategy_version": "v1"})

    symbols = store.list_symbols()
    assert symbols == ["SOXS", "YINN"]
    assert store.delete("SOXS") is True
    assert store.load("SOXS") is None


def test_state_store_loads_none_for_malformed_json(tmp_path):
    store = StrategyStateStore(tmp_path / "state")
    path = store._path("TEST")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    assert store.load("TEST") is None


def test_state_store_reconcile_builds_broker_snapshot_state(tmp_path):
    store = StrategyStateStore(tmp_path / "state")
    state = store.reconcile(
        "TEST",
        {"quantity": 4, "market_value": 41.5},
        {"state_version": 3},
    )

    assert state["symbol"] == "TEST"
    assert state["broker_position_snapshot"]["quantity"] == 4
    assert state["state_version"] == 3
    assert state["schema_version"] == 1
    assert "last_reconciliation_time" in state


def run_test_direct():
    test_state_store_saves_and_loads_atomically(Path("/tmp/strategy-state-store-test"))
    test_state_store_lists_symbols_and_deletes(Path("/tmp/strategy-state-store-test"))
    test_state_store_loads_none_for_malformed_json(Path("/tmp/strategy-state-store-test"))
    test_state_store_reconcile_builds_broker_snapshot_state(Path("/tmp/strategy-state-store-test"))


if __name__ == "__main__":
    run_test_direct()
