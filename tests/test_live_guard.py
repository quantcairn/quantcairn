"""Tests for src.safety.live_guard."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

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


def run_test_direct():
    test_mode_check_paper()
    test_mode_check_live()
    test_mode_check_invalid()
    test_broker_account_available()
    test_broker_account_none()
    test_broker_account_zero_equity()
    test_verdict_keys()
    test_reduce_only_always_allowed()
