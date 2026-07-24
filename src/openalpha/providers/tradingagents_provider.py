from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from ..config import AISelectorRuntimeConfig, load_runtime_config


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
    def __init__(self, config: AISelectorRuntimeConfig | None = None) -> None:
        self.config = config or load_runtime_config()

    def analyze(self, tickers: list) -> dict:
        results: dict[str, dict[str, Any]] = {}
        started_at = time.monotonic()
        normalized_tickers = [
            str(item or "").strip().upper() for item in tickers if str(item or "").strip()
        ]
        remaining_fallback_reason: str | None = None
        for ticker in normalized_tickers:
            if remaining_fallback_reason is not None:
                results[ticker] = self._mock_result(
                    ticker,
                    reason=remaining_fallback_reason,
                    status="SKIPPED_BUDGET",
                    timed_out=False,
                    budget_exhausted=True,
                )
                continue
            if (time.monotonic() - started_at) >= self._total_budget_seconds():
                results[ticker] = self._mock_result(
                    ticker,
                    reason="tradingagents_budget_exhausted",
                    status="SKIPPED_BUDGET",
                    timed_out=False,
                    budget_exhausted=True,
                )
                continue
            try:
                if self._is_available():
                    results[ticker] = self._analyze_with_tradingagents(ticker)
                else:
                    results[ticker] = self._mock_result(
                        ticker,
                        reason="tradingagents_not_installed",
                        status="UNAVAILABLE",
                        timed_out=False,
                        budget_exhausted=False,
                    )
            except subprocess.TimeoutExpired as exc:
                logger.warning("TradingAgents analyze timeout for %s: %s", ticker, exc)
                results[ticker] = self._mock_result(
                    ticker,
                    reason="tradingagents_timeout",
                    status="TIMEOUT",
                    timed_out=True,
                    budget_exhausted=False,
                )
                remaining_fallback_reason = "tradingagents_timeout_budget_exhausted"
            except Exception as exc:
                logger.warning("TradingAgents analyze fallback for %s: %s", ticker, exc)
                message = str(exc).strip() or "tradingagents_error"
                if message == "tradingagents_missing_openai_api_key":
                    results[ticker] = self._mock_result(
                        ticker,
                        reason=message,
                        status="UNAVAILABLE",
                        timed_out=False,
                        budget_exhausted=False,
                    )
                    remaining_fallback_reason = "tradingagents_missing_openai_api_key"
                else:
                    results[ticker] = self._mock_result(
                        ticker,
                        reason="tradingagents_error",
                        status="MALFORMED_RESPONSE",
                        timed_out=False,
                        budget_exhausted=False,
                    )
        return results

    def _is_available(self) -> bool:
        return importlib.util.find_spec("tradingagents") is not None or self._resolve_source_path() is not None

    def _analyze_with_tradingagents(self, ticker: str) -> dict[str, Any]:
        if not self._has_required_runtime():
            raise RuntimeError("tradingagents_missing_openai_api_key")
        source_path = self._resolve_source_path()
        if source_path is not None and importlib.util.find_spec("tradingagents") is None:
            decision = self._run_with_source_path(ticker, source_path)
        else:
            decision = self._run_with_installed_package(ticker)
        return self._normalize_result(ticker, decision)

    def _has_required_runtime(self) -> bool:
        return bool(str(os.environ.get("OPENAI_API_KEY", "")).strip())

    def _timeout_seconds(self) -> int:
        raw_value = str(os.environ.get("SOXS_TRADINGAGENTS_TIMEOUT_SECONDS", "12") or "12").strip()
        try:
            timeout = int(raw_value)
        except (TypeError, ValueError):
            timeout = 12
        return max(5, min(timeout, 120))

    def _total_budget_seconds(self) -> int:
        raw_value = str(os.environ.get("SOXS_TRADINGAGENTS_TOTAL_BUDGET_SECONDS", "18") or "18").strip()
        try:
            budget = int(raw_value)
        except (TypeError, ValueError):
            budget = 18
        return max(self._timeout_seconds(), min(budget, 180))

    def _run_with_installed_package(self, ticker: str) -> Any:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        graph = TradingAgentsGraph(debug=False, config=config)
        _, decision = graph.propagate(ticker, self._analysis_date())
        return decision

    def _run_with_source_path(self, ticker: str, source_path: Path) -> Any:
        helper = """
import json
import sys
sys.path.insert(0, {repo!r})
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
config = DEFAULT_CONFIG.copy()
graph = TradingAgentsGraph(debug=False, config=config)
_, decision = graph.propagate({ticker!r}, {analysis_date!r})
print(json.dumps(decision, ensure_ascii=False, default=str))
""".format(repo=str(source_path), ticker=ticker, analysis_date=self._analysis_date())
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            [self.config.tradingagents_python, "-c", helper],
            capture_output=True,
            text=True,
            cwd=str(source_path),
            env=env,
            timeout=self._timeout_seconds(),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "tradingagents_source_run_failed")
        output = (proc.stdout or "").strip().splitlines()
        if not output:
            raise RuntimeError("tradingagents_source_empty_output")
        import json
        return json.loads(output[-1])

    def _resolve_source_path(self) -> Path | None:
        value = str(self.config.tradingagents_path or "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.exists():
            return None
        candidate = path / "tradingagents"
        if candidate.exists():
            return path
        return None

    def _analysis_date(self) -> str:
        return self.config.tradingagents_analysis_date or date.today().isoformat()

    def _normalize_result(self, ticker: str, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            technical = _clamp_score(self._extract_score(payload, "technical", 60.0), 60.0)
            news = _clamp_score(self._extract_score(payload, "news", 58.0), 58.0)
            sentiment = _clamp_score(self._extract_score(payload, "sentiment", 57.0), 57.0)
            risk = _clamp_score(self._extract_score(payload, "risk", 62.0), 62.0)
            confidence = float(self._extract_score(payload, "confidence", 75.0) or 75.0) / 100.0
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
            "mock_used": False,
            "timed_out": False,
            "budget_exhausted": False,
            "status": "COMPLETE",
            "raw": payload,
        }

    def _extract_score(self, payload: dict[str, Any], keyword: str, default: float) -> float:
        direct_keys = (
            f"{keyword}_score",
            keyword,
            f"{keyword}_analysis_score",
            f"{keyword}_analyst_score",
            f"{keyword}_rating",
        )
        for key in direct_keys:
            value = payload.get(key)
            if value is not None:
                if keyword == "confidence" and float(value) <= 1.0:
                    return float(value) * 100.0
                return float(value)
        nested_values: list[float] = []
        for key, value in payload.items():
            key_str = str(key).lower()
            if keyword not in key_str:
                continue
            if isinstance(value, (int, float)):
                nested_values.append(float(value))
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if "score" in str(sub_key).lower() and isinstance(sub_value, (int, float)):
                        nested_values.append(float(sub_value))
            elif isinstance(value, str) and keyword != "confidence":
                nested_values.append(self._score_from_text(value, positive=(keyword,), negative=("weak", "bearish", "negative")))
        if nested_values:
            return sum(nested_values) / len(nested_values)
        summary_text = str(payload.get("summary") or payload.get("decision") or "")
        if summary_text and keyword != "confidence":
            return self._score_from_text(summary_text, positive=(keyword,), negative=("weak", "bearish", "negative"))
        return float(default)

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

    def _mock_result(
        self,
        ticker: str,
        *,
        reason: str,
        status: str = "SKIPPED_BUDGET",
        timed_out: bool = False,
        budget_exhausted: bool = False,
    ) -> dict[str, Any]:
        return {
            "ticker": ticker,
            "technical_score": 50.0,
            "news_score": 50.0,
            "sentiment_score": 50.0,
            "risk_score": 50.0,
            "confidence": 0.5,
            "reason": f"Fallback TradingAgents mock for {ticker}: {reason}",
            "source": "tradingagents_mock",
            "fallback": True,
            "mock_used": True,
            "timed_out": bool(timed_out),
            "budget_exhausted": bool(budget_exhausted),
            "status": status,
        }
