from __future__ import annotations

from src.openalpha.candidate_ranking import score_candidate


def test_high_liquidity_strong_trend_common_stock_scores_and_recommends_trend_following():
    candidate = score_candidate(
        {
            "ticker": "AAPL",
            "symbol": "AAPL.US",
            "asset_type": "common_stock",
            "average_dollar_volume_20d": 800_000_000,
            "spread_pct": 0.04,
            "current_price": 190.0,
            "ma50": 180.0,
            "ma200": 170.0,
            "ma20": 185.0,
            "relative_strength_60d": 12.0,
            "adx": 28.0,
            "atr_20_percentage": 2.5,
            "risk_score": 90.0,
            "strategy_family": "trend_following",
        }
    )

    assert candidate["candidate_score"] > 80.0
    assert candidate["liquidity_score"] >= 95.0
    assert candidate["trend_score"] >= 80.0
    assert candidate["recommended_strategy"] == "trend_following"
    assert "strategy_family_match" in candidate["score_reason"]


def test_inverse_etf_recommends_inverse_range_and_penalizes_mismatch():
    candidate = score_candidate(
        {
            "ticker": "SOXS",
            "symbol": "SOXS.US",
            "asset_type": "inverse_etf",
            "average_dollar_volume_20d": 120_000_000,
            "spread_pct": 0.08,
            "current_price": 20.0,
            "ma50": 18.0,
            "ma200": 16.0,
            "relative_strength_60d": -8.0,
            "adx": 24.0,
            "atr_20_percentage": 4.0,
            "risk_score": 88.0,
            "strategy_family": "inverse_range",
        }
    )

    assert candidate["recommended_strategy"] == "inverse_range"
    assert candidate["strategy_fit_score"] >= 95.0
    assert candidate["candidate_score"] > 60.0


def test_fallback_to_existing_score_without_factor_fields():
    candidate = score_candidate({"ticker": "XYZ", "score": 77.0, "ai_score": 80.0})

    assert candidate["candidate_score"] == 77.0
    assert candidate["score"] == 77.0
    assert candidate["final_score"] == 77.0
    assert candidate["score_reason"] == "fallback_to_existing_score"


def test_formal_ineligible_candidate_only_gets_diagnostic_score():
    candidate = score_candidate(
        {
            "ticker": "SOFI",
            "asset_type": "common_stock",
            "formal_scoring_eligibility": False,
            "average_dollar_volume_20d": 900_000_000,
            "current_price": 20.0,
            "ma20": 19.0,
            "ma50": 18.0,
            "atr_20_percentage": 6.0,
            "risk_score": 70.0,
        }
    )

    assert candidate["score_type"] == "DIAGNOSTIC"
    assert candidate["score_is_formal"] is False
    assert candidate["formal_candidate_score"] is None
    assert candidate["candidate_score"] is None
    assert candidate["final_score"] is None
    assert candidate["score"] is None
    assert candidate["diagnostic_score"] > 0
    assert candidate["diagnostic_factor_scores"]["liquidity"] > 0
