from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.ai_selector.config import AISelectorRuntimeConfig
from src.ai_selector.integration import AISelector
from src.ai_selector.range_score import RangeFitnessScorer


class StubProvider:
    def __init__(self, payload: dict[str, dict]):
        self.payload = payload

    def analyze(self, tickers: list) -> dict:
        result = {}
        for ticker in tickers:
            row = dict(self.payload.get(ticker, self.payload.get("*", {})))
            row.setdefault("ticker", ticker)
            row.setdefault("confidence", 0.5)
            row.setdefault("reason", f"stub:{ticker}")
            row.setdefault("source", "stub")
            row.setdefault("fallback", False)
            result[ticker] = row
        return result


class RaisingProvider:
    def analyze(self, tickers: list) -> dict:
        raise RuntimeError("provider failed")


def test_range_score_outputs_complete_fields_and_bounds():
    scorer = RangeFitnessScorer()
    payload = scorer.calculate(
        "SOXS",
        {
            "current_price": 4.85,
            "avg_10d_volume": 2_500_000,
            "bid": 4.84,
            "ask": 4.86,
            "spread_pct": 0.41,
            "close_history": [4.60, 4.72, 4.78, 4.81, 4.85],
            "returns": [0.01, -0.02, 0.015, -0.01],
            "three_day_change_pct": 1.2,
            "recent_low": 4.55,
            "recent_high": 4.92,
        },
    )

    assert payload["ticker"] == "SOXS"
    assert set(payload) == {
        "ticker",
        "range_score",
        "volatility_score",
        "mean_reversion_score",
        "liquidity_score",
        "spread_score",
        "stability_score",
    }
    for key in ("range_score", "volatility_score", "mean_reversion_score", "liquidity_score", "spread_score", "stability_score"):
        assert 0.0 <= payload[key] <= 100.0


def test_ai_selector_final_score_sorting_uses_range_score():
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "latest_top10.json"
        selector = AISelector(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["AAA", "BBB"],
                top10_path=report_path,
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
            ),
            tradingagents_provider=StubProvider(
                {
                    "*": {
                        "technical_score": 80,
                        "news_score": 80,
                        "sentiment_score": 80,
                        "risk_score": 80,
                    }
                }
            ),
            finrobot_provider=StubProvider(
                {
                    "*": {
                        "fundamental_score": 80,
                        "valuation_score": 80,
                        "earnings_score": 80,
                        "risk_score": 80,
                    }
                }
            ),
        )
        selector._market_data_snapshot = lambda ticker: {"current_price": 10.0}
        selector.range_scorer.calculate = lambda symbol, market_data: {
            "ticker": symbol,
            "range_score": 95.0 if symbol == "BBB" else 40.0,
            "volatility_score": 90.0,
            "mean_reversion_score": 90.0,
            "liquidity_score": 90.0,
            "spread_score": 90.0,
            "stability_score": 90.0,
        }

        signals = selector.get_signals()

        assert [item["ticker"] for item in signals] == ["BBB", "AAA"]
        assert signals[0]["final_score"] > signals[1]["final_score"]
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["top3"][0]["ticker"] == "BBB"
        assert payload["top10"][0]["range_score"] == 95.0


def test_ai_provider_failure_still_returns_range_ranked_signals():
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "latest_top10.json"
        selector = AISelector(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["AAA", "BBB"],
                top10_path=report_path,
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
            ),
            tradingagents_provider=RaisingProvider(),
            finrobot_provider=RaisingProvider(),
        )
        selector._market_data_snapshot = lambda ticker: {"current_price": 10.0}
        selector.range_scorer.calculate = lambda symbol, market_data: {
            "ticker": symbol,
            "range_score": 90.0 if symbol == "AAA" else 60.0,
            "volatility_score": 80.0,
            "mean_reversion_score": 80.0,
            "liquidity_score": 80.0,
            "spread_score": 80.0,
            "stability_score": 80.0,
        }

        signals = selector.get_signals()

        assert [item["ticker"] for item in signals] == ["AAA", "BBB"]
        assert signals[0]["score"] > signals[1]["score"]
        assert signals[0]["range_score"] == 90.0
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["providers_used"] == ["tradingagents", "finrobot"]
        assert payload["top3"][0]["ticker"] == "AAA"
