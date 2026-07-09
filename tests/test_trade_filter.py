from __future__ import annotations

import tempfile
from pathlib import Path

from src.ai_selector.config import AISelectorRuntimeConfig
from src.ai_selector.integration import AISelector
from src.ai_selector.trade_filter import TradeEligibilityFilter


def _candidate(ticker: str, score: float = 80.0, range_score: float = 80.0) -> dict:
    return {
        "ticker": ticker,
        "final_score": score,
        "score": score,
        "range_score": range_score,
        "confidence": 0.75,
        "reason": f"stub:{ticker}",
        "source": "stub",
    }


def test_rejects_earnings_within_three_days():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 3, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2}},
    )

    assert result["rejected"][0]["reason"] == "earnings_too_close"
    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["ticker"] == "SOXS"
    assert result["accepted"][0]["fallback_used"] is True
    assert result["accepted"][0]["trade_filter_passed"] is False
    assert result["fallback_used"] is True


def test_rejects_extreme_five_day_move():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 16.1}},
    )

    assert result["rejected"][0]["reason"] == "extreme_5d_move"
    assert result["accepted"][0]["fallback_used"] is True


def test_rejects_low_volume():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 10, "avg_volume": 4_999_999, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2}},
    )

    assert result["rejected"][0]["reason"] == "low_volume"
    assert result["accepted"][0]["fallback_used"] is True


def test_rejects_spread_too_wide():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.21, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2}},
    )

    assert result["rejected"][0]["reason"] == "spread_too_wide"
    assert result["accepted"][0]["fallback_used"] is True


def test_rejects_event_regime():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "EVENT", "data_age_seconds": 10, "price_change_5d": 2}},
    )

    assert result["rejected"][0]["reason"] == "event_regime"
    assert result["accepted"][0]["fallback_used"] is True


def test_rejects_stale_data():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 121, "price_change_5d": 2}},
    )

    assert result["rejected"][0]["reason"] == "stale_data"
    assert result["accepted"][0]["fallback_used"] is True


def test_accepts_eligible_candidate():
    result = TradeEligibilityFilter().filter(
        [_candidate("SOXS")],
        {"SOXS": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2}},
    )

    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["ticker"] == "SOXS"
    assert result["accepted"][0]["trade_filter_passed"] is True
    assert result["fallback_used"] is False


def test_fallback_pool_fills_top3_without_crashing():
    candidates = [
        _candidate("AAA", 92.0, 92.0),
        _candidate("BBB", 89.0, 89.0),
        _candidate("CCC", 87.0, 87.0),
    ]
    market_data = {
        "AAA": {"earnings_within_days": 2, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2},
        "BBB": {"earnings_within_days": 2, "avg_volume": 4_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2},
        "CCC": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2},
    }

    result = TradeEligibilityFilter().filter(candidates, market_data)

    assert len(result["accepted"]) == 3
    assert result["fallback_used"] is True
    assert [item["ticker"] for item in result["accepted"]] == ["AAA", "BBB", "CCC"]
    assert result["accepted"][0]["trade_filter_passed"] is False
    assert result["accepted"][1]["trade_filter_passed"] is False
    assert result["accepted"][2]["trade_filter_passed"] is True


def test_ai_selector_applies_trade_filter_before_top3():
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "latest_top10.json"
        selector = AISelector(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["AAA", "BBB", "CCC"],
                top10_path=report_path,
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
            ),
            tradingagents_provider=_Provider(),
            finrobot_provider=_Provider(),
        )
        selector._market_data_snapshot = lambda ticker: {
            "AAA": {"earnings_within_days": 2, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2},
            "BBB": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2},
            "CCC": {"earnings_within_days": 10, "avg_volume": 8_000_000, "bid_ask_spread_pct": 0.1, "regime": "NORMAL", "data_age_seconds": 10, "price_change_5d": 2},
        }[ticker]
        selector.range_scorer.calculate = lambda symbol, market_data: {
            "ticker": symbol,
            "range_score": 80.0,
            "volatility_score": 80.0,
            "mean_reversion_score": 80.0,
            "liquidity_score": 80.0,
            "spread_score": 80.0,
            "stability_score": 80.0,
        }

        signals = selector.get_signals()

        assert {item["ticker"] for item in signals} == {"AAA", "BBB", "CCC"}
        assert sum(1 for item in signals if item.get("trade_filter_passed")) == 2
        assert any(item.get("fallback_used") for item in signals)
        assert report_path.exists()


class _Provider:
    def analyze(self, tickers: list) -> dict:
        result = {}
        for ticker in tickers:
            result[ticker] = {
                "technical_score": 80.0,
                "news_score": 80.0,
                "sentiment_score": 80.0,
                "risk_score": 80.0,
                "fundamental_score": 80.0,
                "valuation_score": 80.0,
                "earnings_score": 80.0,
                "confidence": 0.8,
                "reason": f"stub:{ticker}",
                "source": "stub",
            }
        return result
