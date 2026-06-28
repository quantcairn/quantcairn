from __future__ import annotations

from src.engine.trading_engine import TradingEngine
from src.risk.risk_engine import RiskEngine


class FakeSeries(list):
    def tolist(self):
        return list(self)


class FakeFrame:
    def __init__(self, rows):
        self.rows = list(rows)
        self.columns = list(self.rows[0].keys()) if self.rows else []
        self.attrs = {}
        self._data = {column: FakeSeries([row[column] for row in self.rows]) for column in self.columns}

    def __getitem__(self, key):
        return self._data[key]


def _range_frame():
    rows = []
    for idx in range(60):
        close = 120.0 - idx * 0.5
        rows.append(
            {
                "open": close + 0.3,
                "high": close + 10.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_200_000,
            }
        )
    return FakeFrame(rows)


class FakeSelector:
    def get_signals(self):
        return [{"ticker": "AAPL", "score": 90.0, "volatility": 0.03, "regime": "TREND"}]


class CapturingRiskEngine:
    def __init__(self):
        self.calls = []

    def check_trade_allowed(self, signal, portfolio_state):
        self.calls.append((signal, portfolio_state))
        return True


class FakeBroker:
    def __init__(self):
        self.orders = []

    def get_positions(self):
        return {
            "account_channel": "lb_sg",
            "account_mode": "live",
            "positions": {
                "channels": [
                    {
                        "account_channel": "lb_sg",
                        "positions": [
                            {"symbol": "SOXS.US", "quantity": 132, "cost_price": 5.915},
                        ],
                    }
                ]
            },
        }

    def get_account_balance(self):
        return {
            "ok": True,
            "account_balance": {
                "available_buying_power": 850.0,
                "cash_balance": 850.0,
                "equity": 850.0,
            },
            "account_balance_summary": {
                "available_buying_power": 850.0,
                "cash_balance": 850.0,
                "equity": 850.0,
            },
            "available_buying_power": 850.0,
            "cash_balance": 850.0,
            "equity": 850.0,
        }

    def place_order(self, order):
        self.orders.append(order)
        return {"order_id": "LB-123", "status": "submitted"}


def test_trading_engine_loads_positions_into_risk_state():
    broker = FakeBroker()
    risk_engine = CapturingRiskEngine()
    engine = TradingEngine(capital=1000, selector=FakeSelector(), broker=broker, risk_engine=risk_engine)

    result = engine.run(_range_frame())

    assert broker.orders
    assert result["signals"][0]["risk_approval"] is True
    assert risk_engine.calls
    signal, portfolio_state = risk_engine.calls[0]
    assert signal["trade_action"] in {"buy", "sell", "hold"}
    assert portfolio_state["account_mode"] == "live"
    assert portfolio_state["account_channel"] == "lb_sg"
    assert "positions_snapshot" in portfolio_state
    assert portfolio_state["positions_snapshot"]["account_mode"] == "live"
    assert portfolio_state["available_buying_power"] == 850.0
    assert portfolio_state["capital"] == 850.0


def test_trading_engine_uses_buying_power_for_position_sizing():
    class BuySelector:
        def get_signals(self):
            return [{"ticker": "AAPL", "score": 100.0, "volatility": 0.02, "regime": "TREND"}]

    class RecordingBroker(FakeBroker):
        def __init__(self):
            super().__init__()
            self.orders = []

    broker = RecordingBroker()
    engine = TradingEngine(
        capital=1000,
        selector=BuySelector(),
        broker=broker,
        risk_engine=RiskEngine(min_open_capital=100, min_open_buying_power=100),
    )

    result = engine.run(_range_frame())

    assert result["signals"][0]["position_size"] <= 850.0
    assert broker.orders == [] or all(order["qty"] >= 0 for order in broker.orders)


def test_trading_engine_blocks_new_buys_when_buying_power_low():
    class BuySelector:
        def get_signals(self):
            return [{"ticker": "MSFT", "score": 95.0, "volatility": 0.04, "regime": "TREND"}]

    broker = FakeBroker()
    engine = TradingEngine(
        capital=1000,
        selector=BuySelector(),
        broker=broker,
        risk_engine=RiskEngine(min_open_capital=1000, min_open_buying_power=1000),
    )

    result = engine.run(_range_frame())

    assert result["signals"][0]["risk_approval"] is False
    assert result["executions"] == []
