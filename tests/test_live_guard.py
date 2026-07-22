"""Tests for src.safety.live_guard."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from src.safety import live_guard as live_guard_module
from src.safety.live_guard import LiveGuard


def _make_guard():
    return LiveGuard()


def test_mode_check_paper():
    guard = _make_guard()
    result = guard.validate_live_start({"mode": "paper"})
    # Paper is valid but warnings may appear
    assert "errors" in result


def test_mode_check_live():
    guard = _make_guard()
    result = guard.validate_live_start({"mode": "live"})
    assert "errors" in result
    assert "allowed_to_open_new_positions" in result


def test_mode_check_invalid():
    guard = _make_guard()
    result = guard.validate_live_start({"mode": "invalid"})
    assert len(result["errors"]) > 0


def test_broker_account_available():
    guard = _make_guard()
    fake_broker = MagicMock()
    fake_acct = MagicMock()
    fake_acct.equity = 10000.0
    fake_acct.cash = 5000.0
    fake_broker.get_account.return_value = fake_acct
    fake_broker.get_positions.return_value = []
    result = guard.validate_live_start({"mode": "live", "broker": fake_broker})
    # Other checks may fail (TOP configs etc.), but broker check passed
    assert not any("Broker account" in e for e in result["errors"])


def test_broker_account_none():
    guard = _make_guard()
    fake_broker = MagicMock()
    fake_broker.get_account.return_value = None
    result = guard.validate_live_start({"mode": "live", "broker": fake_broker})
    assert any("Broker returned no account" in e for e in result["errors"])


def test_broker_account_zero_equity():
    guard = _make_guard()
    fake_broker = MagicMock()
    fake_acct = MagicMock()
    fake_acct.equity = 0.0
    fake_broker.get_account.return_value = fake_acct
    result = guard.validate_live_start({"mode": "live", "broker": fake_broker})
    assert any("equity" in e and "0" in e for e in result["errors"])


def test_verdict_keys():
    guard = _make_guard()
    result = guard.validate_live_start({"mode": "live"})
    assert "allowed_to_open_new_positions" in result
    assert "allowed_reduce_only" in result
    assert "errors" in result
    assert "warnings" in result
    assert result["allowed_reduce_only"] is True


def test_reduce_only_always_allowed():
    guard = _make_guard()
    result = guard.validate_live_start({"mode": "live"})
    assert result["allowed_reduce_only"] is True


def test_live_guard_accepts_disabled_empty_top_slots_for_required_selection_date(monkeypatch, tmp_path):
    top_dir = tmp_path / "configs"
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    top_dir.mkdir()
    state_dir.mkdir()
    reports_dir.mkdir()
    monkeypatch.setattr(live_guard_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(live_guard_module, "_required_selection_day", lambda: "2026-07-20")
    monkeypatch.setattr(live_guard_module, "_et_today", lambda: __import__("datetime").date(2026, 7, 21))

    for idx in range(1, 4):
        (top_dir / f"TOP{idx}.yaml").write_text(
            yaml.safe_dump(
                {
                    "enabled": False,
                    "ticker": None,
                    "slot": idx,
                    "selection_date": "2026-07-20",
                    "reason": "top_n_not_filled",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    (state_dir / "ai_selection_state.json").write_text(
        json.dumps({"et_date": "2026-07-20", "selected_symbols": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        live_guard_module,
        "load_latest_ai_selection_state",
        lambda _root: {"selection_date": "2026-07-20"},
    )
    broker = MagicMock()
    broker.get_account.return_value = MagicMock(equity=10000.0)
    broker.get_positions.return_value = []

    result = LiveGuard().validate_live_start({"mode": "live", "broker": broker})

    assert not any("has no valid ticker" in error for error in result["errors"])
    assert not any("selection_date=2026-07-20, expected 2026-07-21" in error for error in result["errors"])
    assert not any("selection_state et_date=2026-07-20, expected 2026-07-21" in error for error in result["errors"])


def run_test_direct():
    test_mode_check_paper()
    test_mode_check_live()
    test_mode_check_invalid()
    test_broker_account_available()
    test_broker_account_none()
    test_broker_account_zero_equity()
    test_verdict_keys()
    test_reduce_only_always_allowed()
