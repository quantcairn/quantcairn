from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
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


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _balance_summary_from_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    summary = snapshot.get("account_balance_summary")
    if isinstance(summary, dict):
        return dict(summary)
    raw = snapshot.get("account_balance")
    if isinstance(raw, dict):
        return {
            key: raw[key]
            for key in (
                "available_buying_power",
                "buying_power",
                "cash_balance",
                "available_cash",
                "equity",
                "net_liquidation",
                "net_liquidation_value",
                "total_equity",
            )
            if key in raw and raw[key] is not None
        }
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return {
                key: first[key]
                for key in (
                    "available_buying_power",
                    "buying_power",
                    "cash_balance",
                    "available_cash",
                    "equity",
                    "net_liquidation",
                    "net_liquidation_value",
                    "total_equity",
                )
                if key in first and first[key] is not None
            }
    return {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TradingEngine:
    def __init__(
        self,
        capital: float = 100_000.0,
        selector: Any | None = None,
        broker: Any | None = None,
        allocator: PortfolioAllocator | None = None,
        risk_engine: RiskEngine | None = None,
        log_dir: str | None = None,
    ):
        self.capital = capital
        self.selector = selector or AISelector()
        self.broker = broker
        self.allocator = allocator or PortfolioAllocator()
        self.risk_engine = risk_engine or RiskEngine()
        self._trade_log_dir = Path(log_dir) if log_dir else Path(__file__).resolve().parents[2] / "logs"
        self._trade_log_dir.mkdir(parents=True, exist_ok=True)

    def _trade_log_path(self) -> Path:
        return self._trade_log_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"

    def _write_trade_log(self, record: dict[str, Any]) -> None:
        payload = dict(record)
        payload.setdefault("timestamp", _utc_now())
        with self._trade_log_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

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

    def _effective_capital(self, portfolio_state: dict[str, Any]) -> float:
        return float(
            _first_non_none(
                portfolio_state.get("available_buying_power"),
                portfolio_state.get("buying_power"),
                portfolio_state.get("cash_balance"),
                portfolio_state.get("available_cash"),
                portfolio_state.get("equity"),
                portfolio_state.get("net_liquidation"),
                portfolio_state.get("total_equity"),
                portfolio_state.get("capital"),
                self.capital,
            )
            or self.capital
        )

    def run(self, market_df: Any, signal_frames: dict[str, Any] | None = None, portfolio_state: dict[str, Any] | None = None) -> dict[str, Any]:
        raw_signals = list(self.selector.get_signals())
        market_regime = detect_market_regime(market_df)
        router = select_strategy(market_df)
        strategy = router.get("strategy_object") or router["strategy"]
        regime = market_regime
        execution_mode = str(getattr(self.broker, "runtime_mode", "paper") if self.broker is not None else "paper")
        is_live_execution = execution_mode == "live"

        provisional = self._provisional_weights(raw_signals)
        state = dict(portfolio_state or {})
        state["regime"] = regime
        state.setdefault("capital", self.capital)
        state.setdefault("execution_mode", execution_mode)

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
                for key, value in _balance_summary_from_snapshot(balance_snapshot).items():
                    state.setdefault(key, value)

        capital_base = self._effective_capital(state)
        state["capital"] = capital_base
        state.update(self.risk_engine.derive_session_state(state))

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
                    "reduce_only": bool(state.get("reduce_only", False)),
                    "risk_pause_reason": state.get("risk_pause_reason", ""),
                }
            )
            if allowed and regime != "EVENT":
                approved.append(candidate)
            self._write_trade_log(
                {
                    "phase": "decision",
                    "ticker": ticker,
                    "regime": regime,
                    "strategy": strategy.__class__.__name__,
                    "score": float(candidate.get("score", 0.0)),
                    "risk_approval": allowed,
                    "position_size": 0.0,
                    "trade_signal": trade_signal,
                    "execution_mode": execution_mode,
                    "reduce_only": bool(state.get("reduce_only", False)),
                    "risk_pause_reason": state.get("risk_pause_reason", ""),
                }
            )

        allocation = self.allocator.allocate(approved)
        results: list[dict[str, Any]] = []
        for item in decisions:
            ticker = item["ticker"]
            alloc = allocation.get(ticker, {})
            weight = float(alloc.get("position_size", 0.0))
            dollar_size = weight * capital_base * RISK_MULTIPLIERS.get(regime, 0.0)
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
            quantity = int(item["position_size"] / price)
            if quantity <= 0:
                if item.get("trade_signal", {}).get("action") == "sell":
                    quantity = 1
                else:
                    continue
            order = {
                "symbol": item["ticker"],
                "side": trade_signal["action"],
                "qty": quantity,
                "order_type": "market",
            }
            response = None
            if self.broker is not None:
                response = self.broker.place_order(order)
            self._write_trade_log(
                {
                    "phase": "execution",
                    "ticker": item["ticker"],
                    "regime": regime,
                    "strategy": item["strategy"],
                    "score": item["score"],
                    "risk_approval": item["risk_approval"],
                    "position_size": item["position_size"],
                    "trade_signal": trade_signal,
                    "execution_mode": execution_mode,
                    "reduce_only": bool(state.get("reduce_only", False)),
                    "risk_pause_reason": state.get("risk_pause_reason", ""),
                    "order": order,
                    "response": response,
                }
            )
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
            "execution_mode": execution_mode,
            "is_live_execution": is_live_execution,
        }
