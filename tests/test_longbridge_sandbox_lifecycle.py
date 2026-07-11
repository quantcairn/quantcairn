from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

from scripts.test_longbridge_sandbox_lifecycle import (
    _build_test_limit_price,
    _build_sell_limit_price,
    _jsonable,
    _is_valid_longbridge_price,
    run_lifecycle_test,
)
from src.broker.base import Order, OrderSide, OrderStatus, OrderType, Position
from src.config.loader import AppConfig, BrokerConfig, LongBridgeConfig


def _sandbox_config(*, mode="sandbox", account_type="paper", allow_live_order=False):
    return AppConfig(
        mode=mode,
        ticker="SOFI",
        broker=BrokerConfig(
            longbridge=LongBridgeConfig(
                enabled=True,
                environment="sandbox",
                account_type=account_type,
                allow_live_order=allow_live_order,
                http_url="https://openapi.longbridge.com",
                quote_ws_url="wss://openapi-quote.longbridge.com/v2",
                trade_ws_url="wss://openapi-trade.longbridge.com/v2",
            )
        ),
    )


class FakeLifecycleBroker:
    def __init__(self, *, start_qty=0):
        self.calls = []
        self.confirmed = False
        self.position_qty = start_qty
        self.orders = {}
        self.next_order_id = 1

    def connect(self):
        self.calls.append("connect")
        return True

    def confirm_sandbox_first_run(self, ticker=""):
        self.calls.append(("confirm", ticker))
        self.confirmed = True
        return {
            "confirmed": True,
            "reason": "sandbox bootstrap confirmed",
            "account_ok": True,
            "positions_ok": True,
            "orders_ok": True,
        }

    def get_account(self):
        self.calls.append("account")
        return SimpleNamespace(cash=1000.0, equity=1000.0, buying_power=1000.0, positions=self.get_positions())

    def get_positions(self):
        self.calls.append("positions")
        if self.position_qty <= 0:
            return []
        return [
            Position(
                ticker="SOFI",
                quantity=self.position_qty,
                avg_entry_price=10.0,
                current_price=10.5,
                market_value=round(self.position_qty * 10.5, 2),
                unrealized_pnl=round(self.position_qty * 0.5, 2),
                unrealized_pnl_pct=5.0,
            )
        ]

    def get_position_for_ticker(self, ticker):
        for position in self.get_positions():
            if position.ticker == ticker:
                return position
        return None

    def get_orders(self, ticker=""):
        self.calls.append(("get_orders", ticker))
        return []

    def get_active_orders(self, ticker):
        self.calls.append(("get_active_orders", ticker))
        return []

    def get_realtime_quote(self, ticker):
        self.calls.append(("quote", ticker))
        return {"last_done": 9.5, "bid": 9.4, "ask": 9.6}

    def place_order(self, ticker, side, quantity, order_type=OrderType.MARKET, limit_price=None, current_bid=0.0, current_ask=0.0, notes=""):
        self.calls.append(("place_order", ticker, side.value, quantity))
        order_id = f"OID-{self.next_order_id}"
        self.next_order_id += 1
        self.orders[order_id] = {
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "polls": 0,
            "status": OrderStatus.PENDING,
        }
        return Order(
            order_id=order_id,
            ticker=ticker,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            status=OrderStatus.PENDING,
            notes="submitted",
        )

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        state = self.orders[order_id]
        state["polls"] += 1
        if state["polls"] >= 2:
            state["status"] = OrderStatus.FILLED
            if state["side"] == OrderSide.BUY:
                self.position_qty += int(state["quantity"])
            else:
                self.position_qty = max(0, self.position_qty - int(state["quantity"]))
        return Order(
            order_id=order_id,
            ticker=state["ticker"],
            side=state["side"],
            order_type=state["order_type"],
            quantity=int(state["quantity"]),
            filled_quantity=int(state["quantity"]) if state["status"] == OrderStatus.FILLED else 0,
            limit_price=state.get("limit_price"),
            avg_fill_price=10.0,
            status=state["status"],
            notes="filled" if state["status"] == OrderStatus.FILLED else "pending",
        )

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return True

    def invalidate_cache(self):
        self.calls.append("invalidate_cache")


@dataclass
class ExampleData:
    name: str
    amount: Decimal


class ExampleEnum(Enum):
    A = "alpha"


class SlotObject:
    __slots__ = ("name", "value")

    def __init__(self):
        self.name = "slot"
        self.value = 7


class WeirdDictObject:
    @property
    def __dict__(self):  # type: ignore[override]
        return "not-a-dict"


def test_build_test_limit_price_uses_real_quote():
    broker = FakeLifecycleBroker(start_qty=0)
    price = _build_test_limit_price(broker, "SOFI")
    assert price == 9.61
    assert _is_valid_longbridge_price(price) is True


def test_build_test_limit_price_falls_back_to_fixed_legal_price():
    class NoQuoteBroker(FakeLifecycleBroker):
        def get_realtime_quote(self, ticker):
            self.calls.append(("quote", ticker))
            return None

    broker = NoQuoteBroker(start_qty=0)
    price = _build_test_limit_price(broker, "SOFI")
    assert price == 1.23
    assert _is_valid_longbridge_price(price) is True


def test_build_sell_limit_price_uses_real_quote():
    broker = FakeLifecycleBroker(start_qty=0)
    price = _build_sell_limit_price(broker, "SOFI")
    assert price == 9.39
    assert _is_valid_longbridge_price(price) is True


def test_longbridge_price_validator():
    assert _is_valid_longbridge_price(0) is False
    assert _is_valid_longbridge_price(None) is False
    assert _is_valid_longbridge_price("abc") is False
    assert _is_valid_longbridge_price(1) is True
    assert _is_valid_longbridge_price(1.234) is True


def test_jsonable_handles_builtin_function_and_method():
    assert "built-in function" in _jsonable(len)
    assert "built-in method" in _jsonable([].append)


def test_jsonable_handles_common_python_types():
    assert _jsonable({"a": 1}) == {"a": 1}
    assert _jsonable([1, (2, 3)]) == [1, [2, 3]]
    data = ExampleData(name="x", amount=Decimal("12.5"))
    assert _jsonable(data) == {"name": "x", "amount": 12.5}
    assert _jsonable(Decimal("1.25")) == 1.25
    assert _jsonable(datetime(2026, 7, 11, 8, 30, tzinfo=timezone.utc)) == "2026-07-11T08:30:00+00:00"
    assert _jsonable(ExampleEnum.A) == "alpha"


def test_jsonable_handles_slots_and_weird_dict_object():
    assert _jsonable(SlotObject()) == {"name": "slot", "value": 7}
    result = _jsonable(WeirdDictObject())
    assert isinstance(result, str)


def test_jsonable_unknown_object_never_raises():
    class Unknown:
        def __repr__(self):
            return "<Unknown>"

    result = _jsonable(Unknown())
    assert result == {} or result == "<Unknown>" or isinstance(result, dict)


def test_order_mapping_keeps_limit_price_from_order_fields():
    order = Order(
        order_id="OID-1",
        ticker="SOFI",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=4.09,
        status=OrderStatus.PENDING,
        notes="pending",
    )
    mapped = run_lifecycle_test.__globals__["_order_to_dict"](order)
    assert mapped["limit_price"] == 4.09


def test_order_mapping_keeps_limit_price_from_raw_price_fields():
    class RawOrder:
        order_id = "OID-2"
        ticker = "SOFI"
        side = OrderSide.BUY
        order_type = OrderType.LIMIT
        quantity = 1
        filled_quantity = 0
        avg_fill_price = 0.0
        status = OrderStatus.PENDING
        notes = "pending"
        price = 4.09

    mapped = run_lifecycle_test.__globals__["_order_to_dict"](RawOrder())
    assert mapped["limit_price"] == 4.09


def test_lifecycle_happy_path(monkeypatch):
    monkeypatch.setattr("scripts.test_longbridge_sandbox_lifecycle.time.sleep", lambda *_args, **_kwargs: None)
    fake_broker = FakeLifecycleBroker(start_qty=0)
    report = run_lifecycle_test(
        config=_sandbox_config(),
        broker_factory=lambda _config: fake_broker,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
    )

    assert report["ok"] is True
    assert report["precheck"]["buy_limit_price"] == 9.61
    assert report["precheck"]["buy_limit_price_valid"] is True
    assert report["buy"]["current_price"] == 9.5
    assert report["sell"]["current_price"] == 9.5
    assert report["checks"]["bootstrap_confirmed"] is True
    assert report["checks"]["buy_fill_confirmed"] is True
    assert report["checks"]["sell_fill_confirmed"] is True
    assert report["checks"]["position_returned_to_zero"] is True
    buy_calls = [call for call in fake_broker.calls if isinstance(call, tuple) and call and call[0] == "place_order" and call[2] == "BUY"]
    sell_calls = [call for call in fake_broker.calls if isinstance(call, tuple) and call and call[0] == "place_order" and call[2] == "SELL"]
    assert len(buy_calls) == 1
    assert len(sell_calls) == 1
    assert report["buy"]["submitted_order"]["order_type"] == OrderType.LIMIT.value
    assert report["sell"]["submitted_order"]["order_type"] == OrderType.LIMIT.value
    assert report["buy"]["submitted_order"]["limit_price"] == 9.61
    assert report["sell"]["submitted_order"]["limit_price"] == 9.39
    assert report["buy"]["filled_quantity"] == 1
    assert report["sell"]["filled_quantity"] == 1
    assert report["buy"]["unfilled_reason"] == ""
    assert report["sell"]["unfilled_reason"] == ""


def test_lifecycle_rejects_live_mode():
    fake_broker = FakeLifecycleBroker(start_qty=0)
    report = run_lifecycle_test(
        config=_sandbox_config(mode="live"),
        broker_factory=lambda _config: fake_broker,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
    )

    assert report["ok"] is False
    assert report["reason"] == "sandbox mode required"
    assert fake_broker.calls == []


def test_lifecycle_rejects_non_paper_account():
    fake_broker = FakeLifecycleBroker(start_qty=0)
    report = run_lifecycle_test(
        config=_sandbox_config(account_type="real"),
        broker_factory=lambda _config: fake_broker,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
    )

    assert report["ok"] is False
    assert "paper/demo" in report["reason"]
    assert fake_broker.calls == []


def test_lifecycle_rejects_existing_position():
    fake_broker = FakeLifecycleBroker(start_qty=1)
    report = run_lifecycle_test(
        config=_sandbox_config(),
        broker_factory=lambda _config: fake_broker,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
    )

    assert report["ok"] is False
    assert "starting position must be zero" in report["reason"]
    assert report["checks"]["start_position_zero"] is False
