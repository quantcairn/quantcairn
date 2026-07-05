from __future__ import annotations

from collections.abc import Iterable
from typing import Any

try:
    from openbb import obb
except ImportError:
    obb = None


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "to_dict"):
        try:
            data = payload.to_dict()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        try:
            items = list(payload)
        except Exception:
            return {}
        if not items:
            return {}
        first = items[0]
        if isinstance(first, dict):
            return dict(first)
        if hasattr(first, "to_dict"):
            try:
                data = first.to_dict()
                if isinstance(data, dict):
                    return dict(data)
            except Exception:
                return {}
    return {}


class OpenBBClient:
    def get_fundamentals(self, ticker: str) -> dict:
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "metrics"),
                ("equity", "fundamental", "overview"),
                ("equity", "fundamental", "profile"),
            ),
        )

    def get_income_statement(self, ticker: str) -> dict:
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "income"),
                ("equity", "fundamental", "income_statement"),
            ),
        )

    def get_balance_sheet(self, ticker: str) -> dict:
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "balance"),
                ("equity", "fundamental", "balance_sheet"),
            ),
        )

    def get_cash_flow(self, ticker: str) -> dict:
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "cash"),
                ("equity", "fundamental", "cash_flow"),
            ),
        )

    def get_analyst_estimates(self, ticker: str) -> dict:
        return self._safe_call(
            ticker,
            (
                ("equity", "estimates", "consensus"),
                ("equity", "estimates", "analyst"),
                ("equity", "fundamental", "estimates"),
            ),
        )

    def _safe_call(self, ticker: str, paths: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
        if obb is None:
            return {}
        symbol = str(ticker or "").strip().upper()
        if not symbol:
            return {}
        for path in paths:
            try:
                target = obb
                for part in path:
                    target = getattr(target, part)
                payload = target(symbol=symbol)
                normalized = _normalize_payload(payload)
                if normalized:
                    return normalized
            except Exception:
                continue
        return {}
