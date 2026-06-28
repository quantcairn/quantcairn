from __future__ import annotations

import os
import re
from typing import Any


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return default


def _lookup(mapping: Any, *names: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        for name in names:
            if name in mapping and mapping[name] is not None:
                return mapping[name]
    else:
        for name in names:
            if hasattr(mapping, name):
                value = getattr(mapping, name)
                if value is not None:
                    return value
    return default


_OPTION_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}\d{6}[CP]\d+(?:\.[A-Z]+)?$")
TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in TRUE_VALUES


class RiskEngine:
    def __init__(
        self,
        max_single_position: float = 0.30,
        max_total_exposure: float = 1.0,
        max_daily_loss: float = 0.03,
        max_drawdown: float = 0.10,
        min_open_capital: float | None = None,
        min_open_buying_power: float | None = None,
        reduce_only_below_min_capital: bool | None = None,
    ):
        self.max_single_position = max_single_position
        self.max_total_exposure = max_total_exposure
        self.max_daily_loss = max_daily_loss
        self.max_drawdown = max_drawdown
        self.min_open_capital = _env_float("RISK_MIN_OPEN_CAPITAL_USD", 1000.0) if min_open_capital is None else min_open_capital
        self.min_open_buying_power = _env_float("RISK_MIN_OPEN_BUYING_POWER_USD", 1000.0) if min_open_buying_power is None else min_open_buying_power
        self.reduce_only_below_min_capital = _env_bool("RISK_REDUCE_ONLY_BELOW_MIN_CAPITAL", True) if reduce_only_below_min_capital is None else reduce_only_below_min_capital

    def _is_option_symbol(self, symbol: str) -> bool:
        cleaned = str(symbol or "").strip().upper()
        return bool(_OPTION_SYMBOL_RE.match(cleaned))

    def _collect_position_entries(self, node: Any) -> list[Any]:
        if node is None:
            return []
        if isinstance(node, dict):
            symbol = _lookup(node, "symbol", "ticker", "code", "stock_symbol", default=None)
            if symbol:
                return [node]
            entries: list[Any] = []
            for key in ("positions", "positions_snapshot", "holdings", "items", "channels", "data"):
                if key in node and node[key] is not None:
                    entries.extend(self._collect_position_entries(node[key]))
            return entries
        if isinstance(node, list):
            entries: list[Any] = []
            for item in node:
                entries.extend(self._collect_position_entries(item))
            return entries
        if hasattr(node, "__dict__"):
            return self._collect_position_entries(vars(node))
        return []

    def _position_notional(self, position: Any) -> float:
        symbol = str(_lookup(position, "symbol", "ticker", "code", default="")).strip().upper()
        quantity = abs(_as_float(_lookup(position, "quantity", "qty", "shares", "position_qty", default=0.0)))
        market_value = _lookup(
            position,
            "market_value",
            "marketValue",
            "market_value_usd",
            "value",
            "notional",
            default=None,
        )
        if market_value is not None:
            return abs(_as_float(market_value))
        price = _lookup(
            position,
            "last_price",
            "last_done",
            "price",
            "cost_price",
            "avg_price",
            "average_cost",
            default=0.0,
        )
        multiplier = 100.0 if self._is_option_symbol(symbol) else 1.0
        return quantity * _as_float(price) * multiplier

    def summarize_positions(self, positions_state: Any, capital: float | None = None) -> dict[str, Any]:
        entries = self._collect_position_entries(positions_state)
        summary_positions: list[dict[str, Any]] = []
        position_symbols: list[str] = []
        total_notional = 0.0
        max_notional = 0.0

        for entry in entries:
            symbol = str(_lookup(entry, "symbol", "ticker", "code", default="")).strip().upper()
            if not symbol:
                continue
            position_symbols.append(symbol)
            notional = self._position_notional(entry)
            total_notional += notional
            max_notional = max(max_notional, notional)
            summary_positions.append(
                {
                    "symbol": symbol,
                    "quantity": _as_float(_lookup(entry, "quantity", "qty", "shares", "position_qty", default=0.0)),
                    "notional": round(notional, 6),
                    "account_channel": _lookup(entry, "account_channel", default=None),
                }
            )

        capital_value = _as_float(capital if capital is not None else _lookup(positions_state, "capital", default=0.0))
        if capital_value <= 0:
            capital_value = 0.0

        account_channel = _lookup(positions_state, "account_channel", default=None)
        if not account_channel:
            account_channel = _lookup(_lookup(positions_state, "positions", default=None), "account_channel", default=None)
        if not account_channel:
            account_channel = self._first_account_channel(positions_state)
        account_mode = str(_lookup(positions_state, "account_mode", default="")).strip().lower()
        if not account_mode:
            account_mode = "paper" if "paper" in str(account_channel or "").lower() else ("live" if account_channel else "unknown")

        summary = {
            "capital": capital_value,
            "account_channel": account_channel,
            "account_mode": account_mode,
            "position_count": len(summary_positions),
            "positions": summary_positions,
            "position_symbols": sorted(set(position_symbols)),
            "current_single_position_pct": (max_notional / capital_value) if capital_value > 0 else 0.0,
            "current_total_exposure_pct": (total_notional / capital_value) if capital_value > 0 else 0.0,
        }
        return summary

    def _extract_liquidity_value(self, source: Any) -> float | None:
        if source is None:
            return None
        for name in (
            "available_buying_power",
            "buying_power",
            "buying_power_usd",
            "cash_balance",
            "available_cash",
            "free_cash",
            "equity",
            "net_liquidation",
            "net_liquidation_value",
            "total_equity",
            "total_asset",
            "capital",
        ):
            value = _lookup(source, name, default=None)
            if value is not None:
                numeric = _as_float(value, default=-1.0)
                if numeric >= 0:
                    return numeric
        return None

    def _resolve_available_funds(self, portfolio_state: Any, positions_summary: dict[str, Any]) -> float | None:
        for candidate in (
            _lookup(portfolio_state, "available_buying_power", "buying_power", "buying_power_usd", default=None),
            _lookup(portfolio_state, "cash_balance", "available_cash", "free_cash", default=None),
            _lookup(portfolio_state, "equity", "net_liquidation", "net_liquidation_value", "total_equity", default=None),
            self._extract_liquidity_value(_lookup(portfolio_state, "account_balance_snapshot", default=None)),
            self._extract_liquidity_value(_lookup(portfolio_state, "balance_snapshot", default=None)),
            self._extract_liquidity_value(_lookup(portfolio_state, "positions_snapshot", default=None)),
        ):
            if candidate is not None:
                return _as_float(candidate)

        capital = _as_float(_lookup(portfolio_state, "capital", default=positions_summary.get("capital", 0.0)))
        if capital > 0:
            return capital
        return None

    def _first_account_channel(self, node: Any) -> str | None:
        if node is None:
            return None
        if isinstance(node, dict):
            account_channel = _lookup(node, "account_channel", default=None)
            if account_channel:
                return str(account_channel)
            for key in ("positions", "positions_snapshot", "holdings", "items", "channels", "data"):
                if key in node and node[key] is not None:
                    found = self._first_account_channel(node[key])
                    if found:
                        return found
            return None
        if isinstance(node, list):
            for item in node:
                found = self._first_account_channel(item)
                if found:
                    return found
            return None
        if hasattr(node, "__dict__"):
            return self._first_account_channel(vars(node))
        return None

    def _candidate_position_pct(self, signal: Any, portfolio_state: Any) -> float:
        for name in ("position_pct", "position_size_pct", "estimated_position_pct"):
            value = _lookup(signal, name, default=None)
            if value is not None:
                return _as_float(value)

        position_size = _lookup(signal, "position_size", default=None)
        capital = _lookup(portfolio_state, "capital", default=None)
        if position_size is not None and capital:
            return _as_float(position_size) / max(_as_float(capital), 1e-9)
        return 0.0

    def _reduce_only_mode(self, portfolio_state: Any) -> bool:
        if _env_bool("RISK_FORCE_REDUCE_ONLY", False):
            return True
        if _env_bool("RISK_DISABLE_NEW_BUYS", False):
            return True
        if _env_bool("RISK_ONLY_SELL", False):
            return True
        if _env_bool("RISK_PAUSE_NEW_ENTRIES", False):
            return True
        return bool(_lookup(portfolio_state, "reduce_only", "reduce_only_mode", default=False))

    def check_trade_allowed(self, signal: Any, portfolio_state: Any) -> bool:
        regime = str(_lookup(signal, "regime", default=_lookup(portfolio_state, "regime", default=""))).upper()
        if regime == "EVENT":
            return False

        positions_summary = self.summarize_positions(portfolio_state, capital=_lookup(portfolio_state, "capital", default=None))
        symbol = str(_lookup(signal, "symbol", "ticker", default="")).strip().upper()
        trade_action = str(_lookup(signal, "trade_action", "action", default="")).strip().lower()
        position_symbols = set(positions_summary.get("position_symbols", []))
        if symbol and trade_action == "buy" and symbol in position_symbols:
            return False
        if symbol and trade_action == "sell" and symbol in position_symbols:
            return True

        if self._reduce_only_mode(portfolio_state):
            return trade_action == "sell"

        available_funds = self._resolve_available_funds(portfolio_state, positions_summary)
        if trade_action == "buy":
            if available_funds is None:
                if self.reduce_only_below_min_capital and positions_summary.get("account_mode") == "live":
                    return False
            else:
                if available_funds < min(self.min_open_capital, self.min_open_buying_power):
                    return False

        candidate_pct = self._candidate_position_pct(signal, portfolio_state)
        current_single = max(
            _as_float(_lookup(portfolio_state, "current_single_position_pct", "single_position_pct", default=0.0)),
            _as_float(positions_summary.get("current_single_position_pct", 0.0)),
        )
        current_total = max(
            _as_float(_lookup(portfolio_state, "current_total_exposure_pct", "total_exposure_pct", "exposure_pct", default=0.0)),
            _as_float(positions_summary.get("current_total_exposure_pct", 0.0)),
        )
        daily_loss = _as_float(_lookup(portfolio_state, "daily_loss_pct", "loss_pct", default=0.0))
        drawdown = _as_float(_lookup(portfolio_state, "drawdown_pct", "max_drawdown_pct", default=0.0))
        consecutive_losses = int(_as_float(_lookup(portfolio_state, "consecutive_losses", "loss_streak", default=0.0)))

        if max(candidate_pct, current_single) > self.max_single_position:
            return False
        if current_total > self.max_total_exposure:
            return False
        if daily_loss > self.max_daily_loss:
            return False
        if drawdown > self.max_drawdown:
            return False
        if consecutive_losses >= 3 and trade_action == "buy":
            return False
        return True
