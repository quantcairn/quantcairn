import json
import tempfile
from pathlib import Path

from src.broker.longbridge import LongBridgeBroker


def test_longbridge_dry_run_audit_log(tmp_path):
    broker = LongBridgeBroker(dry_run=True, log_dir=str(tmp_path))
    response = broker.place_order({
        "symbol": "AAPL",
        "side": "buy",
        "qty": 1,
        "order_type": "market",
    })

    assert response["status"] == "simulated_submitted"
    assert response["order_id"].startswith("dryrun-")

    logs = list(Path(tmp_path).glob("trades-*.jsonl"))
    assert len(logs) == 1
    line = logs[0].read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["action"] == "place_order"
    assert record["dry_run"] is True
    assert record["request"]["json"]["symbol"] == "AAPL"


def test_longbridge_validates_order(tmp_path):
    broker = LongBridgeBroker(dry_run=True, log_dir=str(tmp_path))
    try:
        broker.place_order({"symbol": "AAPL", "side": "hold", "qty": 1})
    except ValueError as exc:
        assert "side" in str(exc)
    else:
        raise AssertionError("invalid side should fail")


def test_longbridge_live_buy_is_blocked_by_global_reduce_only(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "trading_flags.json").write_text(
        json.dumps({"reduce_only_all": True}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))

    class NoNetworkSession:
        def request(self, *args, **kwargs):
            raise AssertionError("reduce-only rejection must happen before network access")

    broker = LongBridgeBroker(
        dry_run=False,
        log_dir=str(tmp_path),
        api_key="key",
        api_secret="secret",
        base_url="https://example.invalid",
        session=NoNetworkSession(),
    )

    response = broker.place_order(
        {"symbol": "AAPL", "side": "buy", "qty": 1, "order_type": "market"}
    )

    assert response["status"] == "rejected"
    assert response["reason"] == "global reduce-only blocks live BUY"


def run_test_direct():
    from pytest import MonkeyPatch

    tmp_root = Path(tempfile.mkdtemp(prefix="longbridge-legacy-test-"))
    test_longbridge_dry_run_audit_log(tmp_root)
    test_longbridge_validates_order(tmp_root)
    monkeypatch = MonkeyPatch()
    try:
        test_longbridge_live_buy_is_blocked_by_global_reduce_only(tmp_root, monkeypatch)
    finally:
        monkeypatch.undo()
