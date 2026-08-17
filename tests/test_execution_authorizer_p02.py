import json

from src.broker.longbridge import LongBridgeBroker as RestBroker
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
