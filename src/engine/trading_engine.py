from __future__ import annotations

from typing import Any

from src.ai_selector.integration import AISelector
from src.market_regime.detector import detect_market_regime
from src.portfolio.allocator import PortfolioAllocator
from src.risk.risk_engine import RiskEngine
from src.strategy_router.router import select_strategy


RISK_MULTIPLIERS = {
    "RANGE": 1.0,
    "TREND": 0.7,
    "EVENT": 0.0,
}


class TradingEngine:
    def __init__(
        self,
        capital: float = 100_000.0,
        selector: Any | None = None,
        broker: Any | None = None,
        allocator: PortfolioAllocator | None = None,
        risk_engine: RiskEngine | None = None,
    ):
        self.capital = capital
        self.selector = selector or AISelector()
        self.broker = broker
        self.allocator = allocator or PortfolioAllocator()
        self.risk_engine = risk_engine or RiskEngine()

    def _signal_frame(self, ticker: str, market_df: Any, signal_frames: dict[str, Any] | None) -> Any:
        if signal_frames and ticker in signal_frames:
            return signal_frames[ticker]
        return market_df

    def _provisional_weights(self, signals: list[dict[str, Any]]) -> dict[str, float]:
        total_score = sum(max(float(signal.get("score", 0.0)), 0.0) for signal in signals)
        if total_score <= 0:
            return {}
        provisional: dict[str, float] = {}
        for signal in signals:
            ticker = str(signal.get("ticker", "")).upper()
            if not ticker:
                continue
            score = max(float(signal.get("score", 0.0)), 0.0)
            volatility = max(float(signal.get("volatility", 0.0)), 1e-9)
            raw_weight = score / total_score
            provisional[ticker] = raw_weight * (1.0 / volatility)
        adjusted_total = sum(provisional.values())
        if adjusted_total <= 0:
            return {}
        return {ticker: value / adjusted_total for ticker, value in provisional.items()}

    def _latest_price(self, frame: Any) -> float:
        if hasattr(frame, "columns") and "close" in frame.columns:
            close_series = frame["close"]
            try:
                value = close_series.iloc[-1]
            except Exception:
                value = close_series[-1]
            return float(value)
        return 0.0

    def run(self, market_df: Any, signal_frames: dict[str, Any] | None = None, portfolio_state: dict[str, Any] | None = None) -> dict[str, Any]:
        raw_signals = list(self.selector.get_signals())
        market_regime = detect_market_regime(market_df)
        router = select_strategy(market_df)
        strategy = router.get("strategy_object") or router["strategy"]
        regime = market_regime

        provisional = self._provisional_weights(raw_signals)
        state = dict(portfolio_state or {})
        state["regime"] = regime
        state.setdefault("capital", self.capital)

        broker_positions = None
        if self.broker is not None and "positions" not in state and "positions_snapshot" not in state:
            try:
                broker_positions = self.broker.get_positions()
            except Exception:
                broker_positions = None
            if isinstance(broker_positions, dict):
                state["positions_snapshot"] = broker_positions
                state.setdefault("account_channel", broker_positions.get("account_channel"))
                state.setdefault("account_mode", broker_positions.get("account_mode"))
                state.setdefault("is_paper_trading", broker_positions.get("is_paper_trading"))
                state.setdefault("positions", broker_positions.get("positions"))
        if self.broker is not None and "account_balance_snapshot" not in state:
            try:
                balance_snapshot = self.broker.get_account_balance()
            except Exception:
                balance_snapshot = None
            if isinstance(balance_snapshot, dict):
                state["account_balance_snapshot"] = balance_snapshot
                snapshot_body = balance_snapshot.get("account_balance") if isinstance(balance_snapshot, dict) else None
                if isinstance(snapshot_body, dict):
                    for key in ("available_buying_power", "buying_power", "cash_balance", "available_cash", "equity", "net_liquidation", "total_equity"):
                        if key in snapshot_body and snapshot_body[key] is not None:
                            state.setdefault(key, snapshot_body[key])

        approved: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for signal in raw_signals:
            ticker = str(signal.get("ticker", "")).upper()
            if not ticker:
                continue
            frame = self._signal_frame(ticker, market_df, signal_frames)
            trade_signal = strategy.generate_signal(frame)
            candidate = dict(signal)
            candidate["regime"] = regime
            candidate["estimated_position_pct"] = provisional.get(ticker, 0.0)
            candidate["trade_action"] = trade_signal.get("action")
            candidate["trade_side"] = trade_signal.get("side")
            candidate["trade_signal"] = trade_signal
            allowed = self.risk_engine.check_trade_allowed(candidate, state)
            decisions.append(
                {
                    "ticker": ticker,
                    "regime": regime,
                    "strategy": strategy.__class__.__name__,
                    "score": float(candidate.get("score", 0.0)),
                    "risk_approval": allowed,
                    "position_size": 0.0,
                    "trade_signal": trade_signal,
                }
            )
            if allowed and regime != "EVENT":
                approved.append(candidate)

        allocation = self.allocator.allocate(approved)
        results: list[dict[str, Any]] = []
        for item in decisions:
            ticker = item["ticker"]
            alloc = allocation.get(ticker, {})
            weight = float(alloc.get("position_size", 0.0))
            dollar_size = weight * self.capital * RISK_MULTIPLIERS.get(regime, 0.0)
            item["position_size"] = round(dollar_size, 2)
            results.append(item)

        print("ticker | regime | strategy | score | position size | risk approval")
        for item in results:
            print(
                f"{item['ticker']} | {item['regime']} | {item['strategy']} | "
                f"{item['score']:.2f} | {item['position_size']:.2f} | {str(item['risk_approval']).lower()}"
            )

        executions: list[dict[str, Any]] = []
        for item in results:
            if not item["risk_approval"] or regime == "EVENT":
                continue
            trade_signal = item.get("trade_signal") or {}
            if trade_signal.get("action") not in {"buy", "sell"}:
                continue
            frame = self._signal_frame(item["ticker"], market_df, signal_frames)
            price = self._latest_price(frame)
            if price <= 0:
                continue
            quantity = max(int(item["position_size"] / price), 1)
            order = {
                "symbol": item["ticker"],
                "side": trade_signal["action"],
                "qty": quantity,
                "order_type": "market",
            }
            response = None
            if self.broker is not None:
                response = self.broker.place_order(order)
            executions.append(
                {
                    "ticker": item["ticker"],
                    "order": order,
                    "response": response,
                    "trade_signal": trade_signal,
                }
            )

        return {
            "regime": regime,
            "strategy": strategy,
            "signals": results,
            "allocation": allocation,
            "executions": executions,
        }
