import json

from src.broker.longbridge import LongBridgeBroker as RestBroker
from src.safety import execution_authorizer
from src.safety.execution_authorizer import authorize_mutation


class _NoNetworkSession:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("network mutation must not be reached")


def test_fail_closed_authorization_and_legacy_rest_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTCAIRN_LIVE_ARMED", "YES")
    monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "LIVE_EXECUTION")
    switch = tmp_path / "kill-switch.json"
    switch.write_text(json.dumps({"state": "CLOSED"}), encoding="utf-8")
    monkeypatch.setenv("QUANTCAIRN_LIVE_KILL_SWITCH_FILE", str(switch))
    assert not authorize_mutation().allowed

    session = _NoNetworkSession()
    broker = RestBroker(
        api_key="fake-key", api_secret="fake-secret", base_url="https://fake.invalid",
        dry_run=False, log_dir=str(tmp_path), session=session,
    )
    assert broker.place_order({"symbol": "TEST", "side": "sell", "quantity": 1})["status"] == "rejected"
    assert broker.cancel_order("order-1")["status"] == "rejected"
    assert not session.calls


def test_authorizer_reader_exception_is_denied(monkeypatch):
    def raise_reader(_path=None):
        raise RuntimeError("simulated authorization failure")

    monkeypatch.setattr(execution_authorizer, "read_kill_switch_state", raise_reader)

    result = authorize_mutation(execution_mode="LIVE_EXECUTION", armed="YES")

    assert result.allowed is False
    assert result.reason_code == "AUTHORIZATION_ERROR"
    assert result.kill_switch_state == "CLOSED"


def test_authorizer_internal_helper_exception_is_denied(monkeypatch):
    def raise_normalizer(_value):
        raise ValueError("simulated parsing failure")

    monkeypatch.setattr(execution_authorizer, "_normalized_mode", raise_normalizer)

    result = authorize_mutation(
        execution_mode="LIVE_EXECUTION",
        armed="YES",
        kill_switch_state="OPEN",
    )

    assert result.allowed is False
    assert result.reason_code == "AUTHORIZATION_ERROR"
    assert result.execution_mode == "UNKNOWN"


def test_authorizer_valid_live_execution_still_allows():
    result = authorize_mutation(
        execution_mode="LIVE_EXECUTION",
        armed="YES",
        kill_switch_state="OPEN",
    )

    assert result.allowed is True
    assert result.reason_code == "AUTHORIZED"
