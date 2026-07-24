from __future__ import annotations

from src.openalpha.data_sufficiency import evaluate_data_sufficiency


def test_evaluate_data_sufficiency_accepts_complete_market_snapshot():
    candidate = {
        "ticker": "AAPL",
        "data_mode": "live",
        "current_price": 188.5,
        "quote_timestamp": "2026-07-14T01:00:00Z",
        "quote_age_seconds": 120,
        "daily_data_status": "LATEST_COMPLETED_SESSION",
        "freshness_status": "SAFE",
        "benchmark_status": "VALID",
        "close_history": [180.0] * 30,
        "average_dollar_volume_20d": 150_000_000,
        "ma20": 185.0,
        "ma50": 182.0,
        "ma200": 175.0,
        "atr_20_percentage": 2.5,
        "liquidity_score": 88.0,
        "trend_score": 79.0,
        "volatility_score": 74.0,
        "risk_score": 83.0,
        "strategy_fit_score": 91.0,
    }

    result = evaluate_data_sufficiency(candidate)

    assert result.scoring_eligible is True
    assert result.data_status == "VALID"
    assert result.quote_status == "OK"
    assert result.history_status == "OK"
    assert result.factor_status == "OK"
    assert result.scoring_block_reason == ""


def test_evaluate_data_sufficiency_blocks_missing_quote_and_history():
    candidate = {
        "ticker": "SOXS",
        "data_mode": "fallback",
        "freshness_status": "STALE",
        "daily_data_status": "STALE",
        "benchmark_status": "INVALID",
        "close_history": [],
        "fallback_history_incomplete": True,
    }

    result = evaluate_data_sufficiency(candidate)

    assert result.scoring_eligible is False
    assert result.data_status == "INVALID"
    assert result.history_status == "INVALID"
    assert result.quote_status == "MISSING"
    assert "missing_quote" in result.scoring_block_reason
    assert "missing_history" in result.scoring_block_reason
    assert "benchmark_invalid" in result.scoring_block_reason


def test_evaluate_data_sufficiency_marks_stale_quote_as_not_eligible():
    candidate = {
        "ticker": "AMD",
        "data_mode": "live",
        "current_price": 120.0,
        "quote_timestamp": "2026-07-14T01:00:00Z",
        "quote_age_seconds": 1800,
        "daily_data_status": "LATEST_COMPLETED_SESSION",
        "freshness_status": "STALE",
        "benchmark_status": "VALID",
        "close_history": [120.0] * 30,
        "average_dollar_volume_20d": 80_000_000,
        "ma20": 119.0,
        "ma50": 117.0,
        "ma200": 111.0,
        "atr_20_percentage": 3.0,
    }

    result = evaluate_data_sufficiency(candidate)

    assert result.scoring_eligible is False
    assert result.data_status == "STALE"
    assert result.quote_status == "STALE"
    assert "stale_data" in result.scoring_block_reason
