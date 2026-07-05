from __future__ import annotations

import importlib.util
import logging
from datetime import date
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


class TradingAgentsProvider:
    def analyze(self, tickers: list) -> dict:
        results: dict[str, dict[str, Any]] = {}
        for ticker in [str(item or "").strip().upper() for item in tickers if str(item or "").strip()]:
            try:
                if self._is_available():
                    results[ticker] = self._analyze_with_tradingagents(ticker)
                else:
                    results[ticker] = self._mock_result(ticker, reason="tradingagents_not_installed")
            except Exception as exc:
                logger.warning("TradingAgents analyze fallback for %s: %s", ticker, exc)
                results[ticker] = self._mock_result(ticker, reason="tradingagents_error")
        return results

    def _is_available(self) -> bool:
        return importlib.util.find_spec("tradingagents") is not None

    def _analyze_with_tradingagents(self, ticker: str) -> dict[str, Any]:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        graph = TradingAgentsGraph(debug=False, config=config)
        _, decision = graph.propagate(ticker, date.today().isoformat())
        return self._normalize_result(ticker, decision)

    def _normalize_result(self, ticker: str, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            technical = _clamp_score(
                payload.get("technical_score")
                or payload.get("technical")
                or payload.get("technical_analysis_score"),
                60.0,
            )
            news = _clamp_score(
                payload.get("news_score")
                or payload.get("news")
                or payload.get("news_analysis_score"),
                58.0,
            )
            sentiment = _clamp_score(
                payload.get("sentiment_score")
                or payload.get("sentiment")
                or payload.get("sentiment_analysis_score"),
                57.0,
            )
            risk = _clamp_score(
                payload.get("risk_score")
                or payload.get("risk")
                or payload.get("risk_management_score"),
                62.0,
            )
            confidence = float(payload.get("confidence") or 0.75)
            reason = str(
                payload.get("reason")
                or payload.get("summary")
                or payload.get("decision")
                or "TradingAgents analysis completed"
            )
        else:
            text = str(payload or "")
            technical = self._score_from_text(text, positive=("trend", "momentum", "breakout", "rsi"))
            news = self._score_from_text(text, positive=("news", "macro", "catalyst", "headline"))
            sentiment = self._score_from_text(text, positive=("sentiment", "bullish", "confidence", "social"))
            risk = self._score_from_text(text, positive=("low risk", "stable", "liquid"), negative=("volatile", "risk", "uncertain"))
            confidence = 0.72
            reason = text[:240] or "TradingAgents text output"

        return {
            "ticker": ticker,
            "technical_score": technical,
            "news_score": news,
            "sentiment_score": sentiment,
            "risk_score": risk,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": reason,
            "source": "tradingagents",
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
        score = 58.0
        lowered = text.lower()
        for token in positive:
            if token in lowered:
                score += 6.0
        for token in negative:
            if token in lowered:
                score -= 5.0
        return _clamp_score(score, 58.0)

    def _mock_result(self, ticker: str, *, reason: str) -> dict[str, Any]:
        seed = _ticker_seed(ticker)
        return {
            "ticker": ticker,
            "technical_score": 55.0 + (seed % 21),
            "news_score": 52.0 + ((seed // 3) % 19),
            "sentiment_score": 50.0 + ((seed // 5) % 18),
            "risk_score": 58.0 + ((seed // 7) % 16),
            "confidence": 0.45,
            "reason": f"Fallback TradingAgents mock for {ticker}: {reason}",
            "source": "tradingagents_mock",
            "fallback": True,
        }
