from __future__ import annotations

import importlib.util
import logging
from typing import Any


logger = logging.getLogger(__name__)


def _ticker_seed(ticker: str) -> int:
    return sum(ord(ch) for ch in str(ticker or "").upper())


def _clamp_score(value: Any, default: float = 50.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = float(default)
    return max(0.0, min(100.0, score))


class FinRobotProvider:
    def analyze(self, tickers: list) -> dict:
        results: dict[str, dict[str, Any]] = {}
        for ticker in [str(item or "").strip().upper() for item in tickers if str(item or "").strip()]:
            try:
                if self._is_available():
                    results[ticker] = self._analyze_with_compatible_interface(ticker)
                else:
                    results[ticker] = self._mock_result(ticker, reason="finrobot_not_installed")
            except Exception as exc:
                logger.warning("FinRobot analyze fallback for %s: %s", ticker, exc)
                results[ticker] = self._mock_result(ticker, reason="finrobot_error")
        return results

    def _is_available(self) -> bool:
        return (
            importlib.util.find_spec("finrobot") is not None
            or importlib.util.find_spec("finrobot_zh") is not None
            or importlib.util.find_spec("finrobot_equity") is not None
        )

    def _analyze_with_compatible_interface(self, ticker: str) -> dict[str, Any]:
        # FinRobot's open-source distribution is primarily report-oriented.
        # Keep the adapter narrow and fail-safe unless a compatible callable is present.
        for module_name in ("finrobot", "finrobot_zh", "finrobot_equity"):
            spec = importlib.util.find_spec(module_name)
            if spec is None:
                continue
            module = __import__(module_name, fromlist=["*"])
            for attr in ("analyze_equity", "run_equity_research", "analyze"):
                fn = getattr(module, attr, None)
                if callable(fn):
                    payload = fn(ticker)
                    return self._normalize_result(ticker, payload)
        raise RuntimeError("no_compatible_finrobot_callable")

    def _normalize_result(self, ticker: str, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            fundamental = _clamp_score(
                payload.get("fundamental_score")
                or payload.get("fundamental")
                or payload.get("quality_score"),
                61.0,
            )
            valuation = _clamp_score(
                payload.get("valuation_score")
                or payload.get("valuation"),
                59.0,
            )
            earnings = _clamp_score(
                payload.get("earnings_score")
                or payload.get("earnings")
                or payload.get("filings_score"),
                60.0,
            )
            risk = _clamp_score(
                payload.get("risk_score")
                or payload.get("risk")
                or payload.get("risk_assessment_score"),
                63.0,
            )
            confidence = float(payload.get("confidence") or 0.74)
            reason = str(
                payload.get("reason")
                or payload.get("summary")
                or payload.get("investment_thesis")
                or "FinRobot analysis completed"
            )
        else:
            text = str(payload or "")
            fundamental = self._score_from_text(text, positive=("balance sheet", "cash flow", "growth", "margin"))
            valuation = self._score_from_text(text, positive=("valuation", "discount", "upside"), negative=("overvalued", "expensive"))
            earnings = self._score_from_text(text, positive=("earnings", "guidance", "estimate beat"), negative=("miss", "downgrade"))
            risk = self._score_from_text(text, positive=("low risk", "strong moat", "resilient"), negative=("debt", "litigation", "volatile"))
            confidence = 0.70
            reason = text[:240] or "FinRobot text output"

        return {
            "ticker": ticker,
            "fundamental_score": fundamental,
            "valuation_score": valuation,
            "earnings_score": earnings,
            "risk_score": risk,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": reason,
            "source": "finrobot",
            "fallback": False,
            "raw": payload,
        }

    def _score_from_text(
        self,
        text: str,
        *,
        positive: tuple[str, ...] = (),
        negative: tuple[str, ...] = (),
    ) -> float:
        score = 59.0
        lowered = text.lower()
        for token in positive:
            if token in lowered:
                score += 6.0
        for token in negative:
            if token in lowered:
                score -= 5.0
        return _clamp_score(score, 59.0)

    def _mock_result(self, ticker: str, *, reason: str) -> dict[str, Any]:
        seed = _ticker_seed(ticker)
        return {
            "ticker": ticker,
            "fundamental_score": 56.0 + (seed % 18),
            "valuation_score": 54.0 + ((seed // 3) % 18),
            "earnings_score": 55.0 + ((seed // 5) % 18),
            "risk_score": 60.0 + ((seed // 7) % 16),
            "confidence": 0.45,
            "reason": f"Fallback FinRobot mock for {ticker}: {reason}",
            "source": "finrobot_mock",
            "fallback": True,
        }
