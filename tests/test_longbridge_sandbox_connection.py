from types import SimpleNamespace

from scripts.test_longbridge_sandbox_connection import probe_sandbox
from src.config.loader import AppConfig, BrokerConfig, LongBridgeConfig
from src.broker.base import OrderSide, OrderType, OrderStatus


class FakeBroker:
    def __init__(self, *, safe=True, allow_order=True):
        self.safe = safe
        self.allow_order = allow_order
        self.calls = []
        self.confirmed = False

    def connect(self):
        self.calls.append("connect")
        return True

    def get_account(self):
        self.calls.append("account")
        return SimpleNamespace(cash=1000.0, equity=1000.0, buying_power=1000.0)

    def get_positions(self):
        self.calls.append("positions")
        return [SimpleNamespace(ticker="SOFI", quantity=1)]

    def get_active_orders(self, ticker):
        self.calls.append(("orders", ticker))
        return []

    def get_orders(self, ticker=""):
        self.calls.append(("get_orders", ticker))
        return self.get_active_orders(ticker)

    def confirm_sandbox_first_run(self, ticker=""):
        self.calls.append(("confirm", ticker))
        if not self.safe:
            return {"confirmed": False, "reason": "sandbox endpoints are not safe", "account_ok": False, "positions_ok": False, "orders_ok": False}
        self.confirmed = True
        return {
            "confirmed": True,
            "reason": "sandbox bootstrap confirmed",
            "ticker": ticker,
            "account_ok": True,
            "positions_ok": True,
            "orders_ok": True,
            "positions_count": 1,
            "orders_count": 0,
            "account": {"cash": 1000.0, "equity": 1000.0, "buying_power": 1000.0},
        }

    def _sandbox_endpoints_are_safe(self):
        return self.safe

    def get_realtime_quote(self, ticker):
        self.calls.append(("quote", ticker))
        return {"bid": 9.5, "ask": 9.6}

def _config(mode="sandbox", allow_live_order=False):
    return AppConfig(
        mode=mode,
        ticker="SOFI",
        broker=BrokerConfig(
            longbridge=LongBridgeConfig(
                enabled=True,
                environment="sandbox" if mode == "sandbox" else "prod",
                account_type="paper" if mode == "sandbox" else "",
                allow_live_order=allow_live_order,
                http_url="https://openapi.longbridge.com" if mode == "sandbox" else None,
                quote_ws_url="wss://openapi-quote.longbridge.com/v2" if mode == "sandbox" else None,
                trade_ws_url="wss://openapi-trade.longbridge.com/v2" if mode == "sandbox" else None,
            )
        ),
    )


def test_probe_sandbox_happy_path():
    summary = probe_sandbox(_config(), FakeBroker())
    assert summary["account"].ok is True
    assert summary["position"].ok is True
    assert summary["orders"].ok is True
    assert summary["order_protection"].ok is True
    assert summary["first_run"].ok is True


def test_probe_rejects_non_sandbox_mode():
    summary = probe_sandbox(_config(mode="paper"), FakeBroker())
    assert summary["account"].ok is False
    assert "sandbox required" in summary["connection_error"]


def test_probe_blocks_unsafe_sandbox_endpoints():
    summary = probe_sandbox(_config(), FakeBroker(safe=False))
    assert summary["order_protection"].ok is False
    assert "not safe" in summary["order_protection"].detail


def test_probe_blocks_allow_live_order_in_sandbox():
    summary = probe_sandbox(_config(allow_live_order=True), FakeBroker())
    assert summary["order_protection"].ok is False
    assert "allow_live_order" in summary["order_protection"].detail


def test_probe_blocks_real_account_in_sandbox():
    config = _config()
    config.broker.longbridge.account_type = "real"
    summary = probe_sandbox(config, FakeBroker())
    assert summary["order_protection"].ok is False
    assert "paper/demo account_type" in summary["order_protection"].detail
