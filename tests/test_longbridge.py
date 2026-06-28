import json
from pathlib import Path

import src.broker.longbridge as longbridge_module
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
    assert record["request"]["payload"]["symbol"] == "AAPL"


def test_longbridge_validates_order(tmp_path):
    broker = LongBridgeBroker(dry_run=True, log_dir=str(tmp_path))
    try:
        broker.place_order({"symbol": "AAPL", "side": "hold", "qty": 1})
    except ValueError as exc:
        assert "side" in str(exc)
    else:
        raise AssertionError("invalid side should fail")


def test_longbridge_normalizes_us_symbol_suffix(tmp_path):
    broker = LongBridgeBroker(dry_run=True, log_dir=str(tmp_path))
    normalized = broker._normalize_order({"symbol": "AAPL", "side": "buy", "qty": 1})
    assert normalized["symbol"] == "AAPL.US"


def test_longbridge_apikey_flow_builds_sdk_and_calls_trade_context(monkeypatch, tmp_path):
    class FakeConfig:
        @staticmethod
        def from_apikey(api_key, api_secret, **kwargs):
            return {
                "api_key": api_key,
                "api_secret": api_secret,
                "kwargs": kwargs,
            }

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config

        def submit_order(self, symbol, order_type, side, submitted_quantity, time_in_force, submitted_price=None, trigger_price=None, limit_offset=None, trailing_amount=None, trailing_percent=None, expire_date=None, outside_rth=None, limit_depth_level=None, trigger_count=None, monitor_price=None, remark=None):
            return {
                "order_id": "LB-123",
                "symbol": symbol,
                "order_type": str(order_type),
                "side": str(side),
                "quantity": submitted_quantity,
                "time_in_force": str(time_in_force),
                "submitted_price": submitted_price,
                "remark": remark,
            }

        def cancel_order(self, order_id):
            return {"cancelled": order_id}

        def order_detail(self, order_id):
            return {"order_id": order_id, "status": "Filled"}

        def stock_positions(self, symbols=None):
            return {
                "channels": [
                    {
                        "account_channel": "lb_sg",
                        "positions": [
                            {"symbol": "AAPL.US", "quantity": 3, "cost_price": 190.0},
                            {"symbol": "SPCX260717C265000.US", "quantity": 2, "cost_price": 0.60},
                        ],
                    }
                ]
            }

        def account_balance(self):
            return {
                "available_buying_power": 850.0,
                "cash_balance": 850.0,
                "equity": 850.0,
            }

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config

    fake_lb = type("FakeLB", (), {
        "Config": FakeConfig,
        "TradeContext": FakeTradeContext,
        "QuoteContext": FakeQuoteContext,
    })()

    monkeypatch.setattr(longbridge_module, "lb", fake_lb, raising=False)

    broker = LongBridgeBroker(
        auth_mode="apikey",
        api_key="key-123",
        api_secret="secret-123",
        access_token="token-123",
        dry_run=False,
        log_dir=str(tmp_path),
    )

    response = broker.place_order({
        "symbol": "AAPL",
        "side": "buy",
        "qty": 2,
        "order_type": "market",
    })

    assert response["order_id"] == "LB-123"
    assert response["body"]["symbol"] == "AAPL.US"
    assert broker.get_order_status("LB-123")["order_id"] == "LB-123"
    assert broker.cancel_order("LB-123")["order_id"] == "LB-123"
    balance = broker.get_account_balance()
    assert balance["ok"] is True
    assert balance["account_balance"]["available_buying_power"] == 850.0
    logs = list(Path(tmp_path).glob("trades-*.jsonl"))
    assert logs
    records = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
    assert any(item["action"] == "place_order" and item["dry_run"] is False for item in records)


def test_longbridge_get_positions_marks_live_account(monkeypatch, tmp_path):
    class FakeConfig:
        @staticmethod
        def from_apikey(api_key, api_secret, **kwargs):
            return {
                "api_key": api_key,
                "api_secret": api_secret,
                "kwargs": kwargs,
            }

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config

        def submit_order(self, *args, **kwargs):
            return {"order_id": "LB-123"}

        def cancel_order(self, order_id):
            return {"cancelled": order_id}

        def order_detail(self, order_id):
            return {"order_id": order_id, "status": "Filled"}

        def stock_positions(self, symbols=None):
            return {
                "channels": [
                    {
                        "account_channel": "lb_sg",
                        "positions": [
                            {"symbol": "SOXS.US", "quantity": 132, "cost_price": 5.915},
                            {"symbol": "SPCX260717C265000.US", "quantity": 2, "cost_price": 0.60},
                        ],
                    }
                ]
            }

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config

    fake_lb = type("FakeLB", (), {
        "Config": FakeConfig,
        "TradeContext": FakeTradeContext,
        "QuoteContext": FakeQuoteContext,
    })()

    monkeypatch.setattr(longbridge_module, "lb", fake_lb, raising=False)

    broker = LongBridgeBroker(
        auth_mode="apikey",
        api_key="key-123",
        api_secret="secret-123",
        access_token="token-123",
        dry_run=False,
        log_dir=str(tmp_path),
    )

    response = broker.get_positions()

    assert response["account_channel"] == "lb_sg"
    assert response["account_mode"] == "live"
    assert response["account_mode_label"] == "实盘"
    assert response["is_paper_trading"] is False


def test_longbridge_account_balance_exposes_normalized_funds(monkeypatch, tmp_path):
    class FakeConfig:
        @staticmethod
        def from_apikey(api_key, api_secret, **kwargs):
            return {
                "api_key": api_key,
                "api_secret": api_secret,
                "kwargs": kwargs,
            }

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config

        def account_balance(self):
            return [
                {
                    "available_buying_power": 703.30,
                    "cash_balance": 703.30,
                    "equity": 1632.35,
                }
            ]

        def stock_positions(self, symbols=None):
            return {"channels": []}

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config

    fake_lb = type("FakeLB", (), {
        "Config": FakeConfig,
        "TradeContext": FakeTradeContext,
        "QuoteContext": FakeQuoteContext,
    })()

    monkeypatch.setattr(longbridge_module, "lb", fake_lb, raising=False)

    broker = LongBridgeBroker(
        auth_mode="apikey",
        api_key="key-123",
        api_secret="secret-123",
        access_token="token-123",
        dry_run=False,
        log_dir=str(tmp_path),
    )

    balance = broker.get_account_balance()
    assert balance["ok"] is True
    assert balance["available_buying_power"] == 703.30
    assert balance["cash_balance"] == 703.30
    assert balance["equity"] == 1632.35
    assert balance["account_balance_summary"]["buying_power"] == 703.30
