from __future__ import annotations

import logging
import os
import time
from typing import Any

from ...data_sources.openbb.client import OpenBBClient, obb
from ..config import AISelectorRuntimeConfig, load_runtime_config


logger = logging.getLogger(__name__)


def _clamp_score(value: Any, default: float = 50.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = float(default)
    return max(0.0, min(100.0, score))


def _safe_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class OpenBBProvider:
    def __init__(
        self,
        config: AISelectorRuntimeConfig | None = None,
        client: OpenBBClient | None = None,
    ) -> None:
        self.config = config or load_runtime_config()
        self.client = client or OpenBBClient()

    def analyze(self, tickers: list) -> dict:
        results: dict[str, dict[str, Any]] = {}
        started_at = time.monotonic()
        for ticker in [str(item or "").strip().upper() for item in tickers if str(item or "").strip()]:
            if (time.monotonic() - started_at) >= self._total_budget_seconds():
                results[ticker] = self._fallback_result(ticker, "openbb_budget_exhausted")
                continue
            try:
                if not self._is_available():
                    results[ticker] = self._fallback_result(ticker, "openbb_not_installed")
                    continue
                results[ticker] = self._analyze_ticker(ticker)
            except Exception as exc:
                logger.warning("OpenBB analyze fallback for %s: %s", ticker, exc)
                results[ticker] = self._fallback_result(ticker, "openbb_error")
        return results

    def _is_available(self) -> bool:
        return obb is not None

    def _total_budget_seconds(self) -> int:
        raw_value = str(os.environ.get("SOXS_OPENBB_TOTAL_BUDGET_SECONDS", "15") or "15").strip()
        try:
            budget = int(raw_value)
        except (TypeError, ValueError):
            budget = 15
        return max(3, min(budget, 120))

    def _analyze_ticker(self, ticker: str) -> dict[str, Any]:
        fundamentals = self.client.get_fundamentals(ticker)
        income_statement = self.client.get_income_statement(ticker)
        balance_sheet = self.client.get_balance_sheet(ticker)
        cash_flow = self.client.get_cash_flow(ticker)
        analyst_estimates = self.client.get_analyst_estimates(ticker)

        growth_score = self._growth_score(fundamentals, income_statement, analyst_estimates)
        profitability_score = self._profitability_score(fundamentals, income_statement)
        balance_sheet_score = self._balance_sheet_score(fundamentals, balance_sheet)
        cash_flow_score = self._cash_flow_score(fundamentals, cash_flow)
        valuation_score = self._valuation_score(fundamentals, analyst_estimates)
        risk_score = self._risk_score(
            fundamentals,
            balance_sheet,
            cash_flow,
            growth_score=growth_score,
            profitability_score=profitability_score,
        )

        fundamental_score = (
            0.30 * growth_score
            + 0.25 * profitability_score
            + 0.20 * balance_sheet_score
            + 0.15 * cash_flow_score
            + 0.10 * valuation_score
        )
        reason = (
            f"Growth {round(growth_score, 1)}, profitability {round(profitability_score, 1)}, "
            f"balance sheet {round(balance_sheet_score, 1)}, cash flow {round(cash_flow_score, 1)}, "
            f"valuation {round(valuation_score, 1)}"
        )
        return {
            "ticker": ticker,
            "fundamental_score": round(fundamental_score, 2),
            "valuation_score": round(valuation_score, 2),
            "growth_score": round(growth_score, 2),
            "profitability_score": round(profitability_score, 2),
            "balance_sheet_score": round(balance_sheet_score, 2),
            "cash_flow_score": round(cash_flow_score, 2),
            "risk_score": round(risk_score, 2),
            "confidence": 0.65,
            "reason": reason,
            "source": "openbb",
            "fallback": False,
            "raw": {
                "fundamentals": fundamentals,
                "income_statement": income_statement,
                "balance_sheet": balance_sheet,
                "cash_flow": cash_flow,
                "analyst_estimates": analyst_estimates,
            },
        }

    def _fallback_result(self, ticker: str, reason: str) -> dict[str, Any]:
        return {
            "ticker": ticker,
            "fundamental_score": 50.0,
            "valuation_score": 50.0,
            "growth_score": 50.0,
            "profitability_score": 50.0,
            "balance_sheet_score": 50.0,
            "cash_flow_score": 50.0,
            "risk_score": 50.0,
            "confidence": 0.5,
            "reason": f"Fallback OpenBB result for {ticker}: {reason}",
            "source": "openbb_mock",
            "fallback": True,
        }

    def _flatten(self, payload: Any, prefix: str = "") -> dict[str, float]:
        flattened: dict[str, float] = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                flattened.update(self._flatten(value, next_prefix))
            return flattened
        if isinstance(payload, list):
            for index, value in enumerate(payload):
                next_prefix = f"{prefix}.{index}" if prefix else str(index)
                flattened.update(self._flatten(value, next_prefix))
            return flattened
        number = _safe_number(payload)
        if number is not None and prefix:
            flattened[prefix.lower()] = number
        return flattened

    def _average_keyword_score(
        self,
        payloads: list[dict[str, Any]],
        *,
        keywords: tuple[str, ...],
        transform,
        default: float = 50.0,
    ) -> float:
        values: list[float] = []
        for payload in payloads:
            for key, value in self._flatten(payload).items():
                if any(keyword in key for keyword in keywords):
                    score = transform(value)
                    if score is not None:
                        values.append(_clamp_score(score, default))
        if not values:
            return float(default)
        return _clamp_score(sum(values) / len(values), default)

    def _growth_score(self, fundamentals: dict, income_statement: dict, analyst_estimates: dict) -> float:
        return self._average_keyword_score(
            [fundamentals, income_statement, analyst_estimates],
            keywords=("growth", "revenue", "sales", "eps", "earnings"),
            transform=lambda value: 50.0 + (value * 100.0 if abs(value) <= 2 else value) * 1.2,
        )

    def _profitability_score(self, fundamentals: dict, income_statement: dict) -> float:
        return self._average_keyword_score(
            [fundamentals, income_statement],
            keywords=("margin", "profit", "roe", "roa", "roic"),
            transform=lambda value: 50.0 + (value * 100.0 if abs(value) <= 2 else value) * 0.8,
        )

    def _balance_sheet_score(self, fundamentals: dict, balance_sheet: dict) -> float:
        ratio_score = self._average_keyword_score(
            [fundamentals, balance_sheet],
            keywords=("currentratio", "quickratio", "cash", "liquidity"),
            transform=lambda value: 55.0 + min(value, 5.0) * 6.0,
        )
        leverage_score = self._average_keyword_score(
            [fundamentals, balance_sheet],
            keywords=("debttoequity", "debt", "leverage"),
            transform=lambda value: 80.0 - min(max(value, 0.0), 5.0) * 12.0,
        )
        return _clamp_score((ratio_score + leverage_score) / 2.0, 50.0)

    def _cash_flow_score(self, fundamentals: dict, cash_flow: dict) -> float:
        return self._average_keyword_score(
            [fundamentals, cash_flow],
            keywords=("cashflow", "freecashflow", "operatingcashflow", "fcf"),
            transform=lambda value: 50.0 + (value * 100.0 if abs(value) <= 2 else value) * 0.9,
        )

    def _valuation_score(self, fundamentals: dict, analyst_estimates: dict) -> float:
        pe_score = self._average_keyword_score(
            [fundamentals, analyst_estimates],
            keywords=("pe", "pricetoearnings", "forwardpe", "peg"),
            transform=lambda value: 90.0 - min(max(value, 0.0), 50.0) * 1.1,
        )
        ps_score = self._average_keyword_score(
            [fundamentals, analyst_estimates],
            keywords=("pricetosales", "pricesales", "pricetobook", "book"),
            transform=lambda value: 85.0 - min(max(value, 0.0), 30.0) * 1.2,
        )
        return _clamp_score((pe_score + ps_score) / 2.0, 50.0)

    def _risk_score(
        self,
        fundamentals: dict,
        balance_sheet: dict,
        cash_flow: dict,
        *,
        growth_score: float,
        profitability_score: float,
    ) -> float:
        leverage = self._average_keyword_score(
            [fundamentals, balance_sheet],
            keywords=("debttoequity", "debt", "leverage"),
            transform=lambda value: 85.0 - min(max(value, 0.0), 5.0) * 13.0,
        )
        liquidity = self._average_keyword_score(
            [fundamentals, balance_sheet, cash_flow],
            keywords=("currentratio", "quickratio", "cash", "liquidity"),
            transform=lambda value: 45.0 + min(max(value, 0.0), 5.0) * 10.0,
        )
        return _clamp_score((leverage + liquidity + growth_score + profitability_score) / 4.0, 50.0)
