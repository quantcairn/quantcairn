from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(0.0, min(100.0, number))


def _avg(values: list[float], default: float) -> float:
    items = [float(value) for value in values if value is not None]
    if not items:
        return float(default)
    return sum(items) / len(items)


def combine_scores(tradingagents_result, finrobot_result) -> list:
    ranked: list[dict[str, Any]] = []
    tickers = sorted(
        set((tradingagents_result or {}).keys()) | set((finrobot_result or {}).keys())
    )
    for ticker in tickers:
        ta = dict((tradingagents_result or {}).get(ticker) or {})
        fr = dict((finrobot_result or {}).get(ticker) or {})

        technical_score = _safe_float(ta.get("technical_score"), 50.0)
        news_score = _safe_float(ta.get("news_score"), 50.0)
        sentiment_score = _safe_float(ta.get("sentiment_score"), 50.0)
        fundamental_score = _avg(
            [
                fr.get("fundamental_score"),
                fr.get("valuation_score"),
                fr.get("earnings_score"),
            ],
            50.0,
        )
        risk_score = _avg(
            [ta.get("risk_score"), fr.get("risk_score")],
            50.0,
        )

        final_score = (
            0.35 * technical_score
            + 0.25 * news_score
            + 0.20 * sentiment_score
            + 0.10 * fundamental_score
            + 0.10 * risk_score
        )
        confidence = _avg(
            [
                (ta.get("confidence") or 0) * 100.0,
                (fr.get("confidence") or 0) * 100.0,
            ],
            max(40.0, min(95.0, final_score)),
        ) / 100.0
        reasons = [
            str(ta.get("reason") or "").strip(),
            str(fr.get("reason") or "").strip(),
        ]
        sources = [
            str(ta.get("source") or "").strip(),
            str(fr.get("source") or "").strip(),
        ]
        ranked.append(
            {
                "ticker": ticker,
                "score": round(final_score, 2),
                "confidence": round(max(0.0, min(1.0, confidence)), 2),
                "reason": " | ".join([item for item in reasons if item]) or "No reason provided",
                "source": "+".join([item for item in sources if item]) or "unknown",
                "technical_score": round(technical_score, 2),
                "news_score": round(news_score, 2),
                "sentiment_score": round(sentiment_score, 2),
                "fundamental_score": round(fundamental_score, 2),
                "risk_score": round(risk_score, 2),
            }
        )

    ranked.sort(key=lambda item: (-float(item.get("score") or 0.0), item.get("ticker") or ""))
    return ranked
