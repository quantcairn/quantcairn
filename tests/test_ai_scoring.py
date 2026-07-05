from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.ai_selector.scoring import combine_scores


def test_combine_scores_sorts_descending():
    tradingagents_result = {
        "NVDA": {
            "technical_score": 90,
            "news_score": 80,
            "sentiment_score": 75,
            "risk_score": 70,
            "confidence": 0.8,
            "reason": "strong trend",
            "source": "tradingagents",
        },
        "AAPL": {
            "technical_score": 60,
            "news_score": 55,
            "sentiment_score": 58,
            "risk_score": 65,
            "confidence": 0.7,
            "reason": "mixed",
            "source": "tradingagents",
        },
    }
    finrobot_result = {
        "NVDA": {
            "fundamental_score": 78,
            "valuation_score": 74,
            "earnings_score": 82,
            "risk_score": 72,
            "confidence": 0.78,
            "reason": "healthy fundamentals",
            "source": "finrobot",
        },
        "AAPL": {
            "fundamental_score": 65,
            "valuation_score": 64,
            "earnings_score": 66,
            "risk_score": 68,
            "confidence": 0.75,
            "reason": "stable fundamentals",
            "source": "finrobot",
        },
    }

    ranked = combine_scores(tradingagents_result, finrobot_result)

    assert [item["ticker"] for item in ranked] == ["NVDA", "AAPL"]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["source"] == "tradingagents+finrobot"


def run_test_direct():
    test_combine_scores_sorts_descending()
