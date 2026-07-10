from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace

from src.broker.base import OrderSide, OrderStatus, OrderType
from src.config.loader import AiSelectorConfig, AppConfig, PositionConfig


if "yfinance" not in sys.modules:
    fake_yfinance = types.ModuleType("yfinance")

    class _FakeTicker:
        def __init__(self, *args, **kwargs):
            self.fast_info = {}

        def history(self, *args, **kwargs):
            return None

    fake_yfinance.Ticker = _FakeTicker
    sys.modules["yfinance"] = fake_yfinance


from src.engine.trading_engine import AISelectionDecision, TradingEngine


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


def _engine(
    *,
    mode: str = "paper",
    allow_paper: bool = False,
    allow_live: bool = False,
    multiplier: float = 0.25,
    size_per_trade: int = 10,
):
    config = AppConfig(
        ticker="SOFI",
        mode=mode,
        position=PositionConfig(size_per_trade=size_per_trade, initial_capital=1000.0),
        ai_selector=AiSelectorConfig(
            allow_fallback_paper_entries=allow_paper,
            allow_fallback_live_entries=allow_live,
            fallback_paper_position_multiplier=multiplier,
        ),
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
    engine._ai_selection = AISelectionDecision(
        enabled=True,
        active=True,
        signal_for_ticker={"ticker": "SOFI", "score": 88.0},
        regime="NORMAL",
        strategy="range_detector",
        risk_approved=True,
        allocation_weight=0.30,
        fallback_used=True,
    )
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: SimpleNamespace(
            order_id="O-1",
            ticker="SOFI",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_ask"],
            status=OrderStatus.FILLED,
            notes="",
        ),
    )
    return engine


def _buy_signal():
    return SimpleNamespace(type=SimpleNamespace(value="BUY"), reason="test", price=10.0)


def _sell_signal():
    return SimpleNamespace(type=SimpleNamespace(value="SELL"), reason="test", price=10.0)


def test_fallback_used_live_mode_blocks_buy():
    engine = _engine(mode="live", allow_paper=True)
    calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    assert engine._last_signal_reason == "fallback_used_live_blocked"
    assert any(
        item["phase"] == "risk_decision" and item["reason"] == "fallback_used_live_blocked"
        for item in engine._write_runtime_audit_events
    )


def test_fallback_used_paper_mode_blocks_when_disabled():
    engine = _engine(mode="paper", allow_paper=False)
    calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    assert engine._last_signal_reason == "fallback_used_blocked"
    assert any(
        item["phase"] == "risk_decision" and item["reason"] == "fallback_used_blocked"
        for item in engine._write_runtime_audit_events
    )


def test_detect_market_regime_ignores_missing_confidence():
    engine = TradingEngine(AppConfig(ticker="SOXS", mode="paper"), ignore_trading_hours=True)
    regime = engine._detect_market_regime(
        [
            {"ticker": "SOXS", "confidence": None},
            {"ticker": "YINN"},
            {"ticker": "DRIP", "confidence": 0.58},
        ],
        {"ticker": "SOXS", "confidence": None},
    )
    assert regime == "NORMAL"


def test_fallback_used_paper_mode_allows_smaller_buy():
    engine = _engine(mode="paper", allow_paper=True, multiplier=0.25, size_per_trade=10)
    calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            order_id="O-1",
            ticker="SOFI",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_ask"],
            status=OrderStatus.FILLED,
            notes="",
        ),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert len(calls) == 1
    assert calls[0]["quantity"] == 2
    assert any(
        item["phase"] == "risk_decision"
        and item["adjusted_target_shares"] == 2
        and item["reason"] == "fallback_used_paper_allowed_with_reduced_size"
        for item in engine._write_runtime_audit_events
    )


def test_fallback_scaled_below_minimum_blocks_buy():
    engine = _engine(mode="paper", allow_paper=True, multiplier=0.25, size_per_trade=1)
    calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert calls == []
    assert engine._last_signal_reason == "fallback_reduced_size_below_minimum"
    assert any(
        item["phase"] == "risk_decision"
        and item["reason"] == "fallback_reduced_size_below_minimum"
        for item in engine._write_runtime_audit_events
    )


def test_fallback_does_not_block_sell_or_reduce_only():
    engine = _engine(mode="paper", allow_paper=False)
    sell_calls = []
    engine._position_shares = 5
    engine._entry_price = 10.0
    engine.broker = SimpleNamespace(
        place_order=lambda **kwargs: sell_calls.append(kwargs)
        or SimpleNamespace(
            order_id="S-1",
            ticker="SOFI",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_bid"],
            status=OrderStatus.FILLED,
            notes="",
        ),
    )

    engine._handle_sell_signal(_sell_signal(), 10.0, 9.9)
    assert len(sell_calls) == 1

    engine._reduce_only = True
    buy_calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: buy_calls.append(kwargs),
    )

    engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert buy_calls == []
    assert engine._last_signal_reason == "仅减仓模式：今晚不新开仓"


def test_risk_decision_log_contains_reason_and_adjusted_shares(caplog):
    engine = _engine(mode="paper", allow_paper=True, multiplier=0.25, size_per_trade=10)
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0, positions=[]),
        place_order=lambda **kwargs: SimpleNamespace(
            order_id="O-1",
            ticker="SOFI",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_ask"],
            status=OrderStatus.FILLED,
            notes="",
        ),
    )

    with caplog.at_level(logging.INFO):
        engine._handle_buy_signal(_buy_signal(), 10.0, 10.1)

    assert any("risk_decision:" in record.message for record in caplog.records)
    assert any('"adjusted_target_shares": 2' in record.message for record in caplog.records)
    assert any('"reason": "fallback_used_paper_allowed_with_reduced_size"' in record.message for record in caplog.records)
