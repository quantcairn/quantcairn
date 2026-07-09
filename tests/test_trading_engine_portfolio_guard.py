from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from src.broker.base import Order, OrderSide, OrderStatus, OrderType
from src.config.loader import AppConfig, PortfolioConfig


if "yfinance" not in sys.modules:
    fake_yfinance = types.ModuleType("yfinance")

    class _FakeTicker:
        def __init__(self, *args, **kwargs):
            self.fast_info = {}

        def history(self, *args, **kwargs):
            return None

    fake_yfinance.Ticker = _FakeTicker
    sys.modules["yfinance"] = fake_yfinance

from src.engine.trading_engine import TradingEngine


class FakeStrategy:
    def record_entry(self, price: float) -> None:
        self.last_entry = price

    def clear_entry(self) -> None:
        self.cleared = True


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.trades = []
        self.order_submitted_calls = []

    def alert(self, message, level="info"):
        self.alerts.append((message, level))

    def trade(self, *args, **kwargs):
        self.trades.append((args, kwargs))

    def order_submitted(self, *args, **kwargs):
        self.order_submitted_calls.append((args, kwargs))


class FakeRisk:
    def check_entry(self, current_price, shares, position_shares):
        return SimpleNamespace(allowed=True, reason="ok")

    def record_trade(self, trade):
        self.last_trade = trade


class FakeOrderState:
    def __init__(self):
        self.has_pending_order = False
        self.is_blocked = False
        self.pending_order_id = ""
        self.blocked_reason = ""
        self.rejected = []
        self.submitted = []
        self.filled = []

    def check_buying_power(self, price, quantity, available_cash):
        return True, "ok"

    def record_rejected(self, **kwargs):
        self.rejected.append(kwargs)

    def record_submitted(self, order_id, side):
        self.submitted.append((order_id, side))

    def record_filled(self, order_id):
        self.filled.append(order_id)


class FakePortfolioManager:
    def __init__(self, allowed=True, reason="ok", check_result=None):
        self.allowed = allowed
        self.reason = reason
        self.check_result = check_result
        self.calls = []

    def check_portfolio_risk(self, proposed_order, portfolio_state):
        self.calls.append((proposed_order, portfolio_state))
        if self.check_result is not None:
            return self.check_result
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "current_exposure": 0.20,
            "projected_exposure": 0.35,
            "current_leveraged_etf_exposure": 0.10,
            "projected_leveraged_etf_exposure": 0.15,
        }


def _engine(portfolio_enabled=False):
    config = AppConfig(
        ticker="SOFI",
        portfolio=PortfolioConfig(enabled=portfolio_enabled),
    )
    engine = TradingEngine(config, ignore_trading_hours=True)
    engine.strategy = FakeStrategy()
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()
    engine.order_state = FakeOrderState()
    engine._write_runtime_audit_events = []
    engine._write_runtime_audit = lambda phase, **fields: engine._write_runtime_audit_events.append(
        {"phase": phase, **fields}
    )
    return engine


def _buy_signal():
    return SimpleNamespace(type=SimpleNamespace(value="BUY"), reason="test")


def test_portfolio_disabled_keeps_original_buy_behavior():
    engine = _engine(portfolio_enabled=False)
    calls = []
    engine.portfolio_manager = FakePortfolioManager(allowed=False, reason="should_not_run")
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs)
        or Order(
            order_id="O-1",
            ticker="SOFI",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_ask"],
            status=OrderStatus.FILLED,
        ),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert len(calls) == 1
    assert engine.portfolio_manager.calls == []


def test_portfolio_enabled_blocks_buy_without_calling_broker():
    engine = _engine(portfolio_enabled=True)
    calls = []
    engine.portfolio_manager = FakePortfolioManager(allowed=False, reason="single_position_exceeded")
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(
            cash=1000.0,
            buying_power=1000.0,
            positions=[SimpleNamespace(ticker="YINN", quantity=12, market_value=300.0)],
        ),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    assert engine.portfolio_manager.calls
    assert engine._last_signal_reason == "组合风控阻止买入：single_position_exceeded"
    assert any(item["phase"] == "portfolio_risk_blocked" for item in engine._write_runtime_audit_events)


def test_portfolio_guard_does_not_block_sell_orders():
    engine = _engine(portfolio_enabled=True)
    calls = []
    engine.portfolio_manager = FakePortfolioManager(allowed=False, reason="should_not_run")
    engine._position_shares = 5
    engine._entry_price = 10.0
    engine.broker = SimpleNamespace(
        place_order=lambda **kwargs: calls.append(kwargs)
        or Order(
            order_id="S-1",
            ticker="SOFI",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_bid"],
            status=OrderStatus.FILLED,
        )
    )

    engine._handle_sell_signal(_buy_signal(), 10.0, 9.9)

    assert len(calls) == 1
    assert engine.portfolio_manager.calls == []


def test_reduce_only_buy_bypasses_portfolio_guard():
    engine = _engine(portfolio_enabled=True)
    engine._reduce_only = True
    calls = []
    engine.portfolio_manager = FakePortfolioManager(allowed=False, reason="should_not_run")
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    assert engine.portfolio_manager.calls == []
    assert engine._last_signal_reason == "仅减仓模式：今晚不新开仓"


def test_portfolio_state_read_failure_blocks_new_buy():
    engine = _engine(portfolio_enabled=True)
    calls = []
    engine.portfolio_manager = FakePortfolioManager(allowed=True, reason="ok")
    engine.broker = SimpleNamespace(
        get_account=lambda: (_ for _ in ()).throw(RuntimeError("account missing")),
        get_positions=lambda: (_ for _ in ()).throw(RuntimeError("positions missing")),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    assert engine.portfolio_manager.calls == []
    assert engine._last_signal_reason == "组合风控启用但无法读取账户/持仓，禁止新买入"
    assert any(item["reason"] == "portfolio_state_unavailable" for item in engine._write_runtime_audit_events)


def test_portfolio_guard_records_rejection_reason():
    engine = _engine(portfolio_enabled=True)
    calls = []
    engine.portfolio_manager = FakePortfolioManager(
        check_result={
            "allowed": False,
            "reason": "leveraged_group_exposure_exceeded",
            "current_exposure": 0.40,
            "projected_exposure": 0.55,
            "current_leveraged_etf_exposure": 0.25,
            "projected_leveraged_etf_exposure": 0.60,
        }
    )
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    audit = [item for item in engine._write_runtime_audit_events if item["phase"] == "portfolio_risk_blocked"]
    assert audit
    assert audit[0]["reason"] == "leveraged_group_exposure_exceeded"
    assert audit[0]["current_exposure"] == 0.40
    assert audit[0]["projected_exposure"] == 0.55
