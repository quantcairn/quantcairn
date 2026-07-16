from __future__ import annotations

from src.ai_selector.data_quality import enrich_candidate_quality, evaluate_candidate_data_quality


def _complete_candidate() -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "quote_timestamp": "2026-07-16T13:00:00Z",
        "quote_age_seconds": 60,
        "current_price": 150.0,
        "open": 149.0,
        "high": 151.0,
        "low": 148.5,
        "close": 150.0,
        "volume": 1_200_000,
        "history_rows": 30,
        "close_history": [150.0] * 30,
        "average_dollar_volume_20d": 120_000_000.0,
        "atr_20_percentage": 4.2,
        "ma20": 148.0,
        "ma50": 145.0,
        "benchmark_status": "VALID",
        "benchmark_alignment_status": "VALID",
        "market_cap": 3_000_000_000.0,
        "daily_data_status": "VALID",
        "freshness_status": "SAFE",
        "asset_type": "common_stock",
    }


def test_explanation_only_fallback_stays_scoring_eligible() -> None:
    candidate = _complete_candidate()
    provider_audit = {
        "openbb": {
            "attempted": 1,
            "success": 1,
            "fallback_used": 1,
            "mock_used": 0,
            "contributed_fields": ["reason"],
            "has_fallback": True,
        }
    }
    provider_outputs = {
        "openbb": {
            "AAPL": {
                "ticker": "AAPL",
                "source": "openbb",
                "reason": "research note only",
            }
        }
    }

    result = evaluate_candidate_data_quality(
        candidate,
        provider_audit=provider_audit,
        provider_outputs=provider_outputs,
    )

    assert result.data_status == "COMPLETE"
    assert result.scoring_eligible is True
    assert result.candidate_fallback is True
    assert result.mock_used is False
    assert result.fallback_scope == "EXPLANATION_ONLY"
    assert result.fallback_severity == "INFO"
    assert "noncritical_provider_degraded" in result.quality_warnings
    assert "explanation_only_degraded" in result.quality_warnings
    assert "critical_market_data_missing" not in result.blocking_reasons


def test_missing_critical_market_data_blocks_scoring() -> None:
    candidate = _complete_candidate()
    candidate.pop("current_price")
    candidate.pop("quote_timestamp")

    result = evaluate_candidate_data_quality(candidate)

    assert result.data_status == "INVALID"
    assert result.scoring_eligible is False
    assert "critical_market_data_missing" in result.blocking_reasons
    assert any(field in result.missing_fields for field in {"quote", "quote_timestamp", "current_price"})


def test_stale_quote_marks_data_stale() -> None:
    candidate = _complete_candidate()
    candidate["quote_age_seconds"] = 1200

    result = evaluate_candidate_data_quality(candidate)

    assert result.data_status == "STALE"
    assert result.scoring_eligible is False
    assert "quote_timestamp" in result.stale_fields
    assert "quote_expired" in result.blocking_reasons


def test_enrich_candidate_quality_attaches_explanations() -> None:
    candidate = _complete_candidate()
    candidate["score_reason"] = "strong liquidity"

    enriched = enrich_candidate_quality(candidate)

    assert enriched["data_quality"]["data_status"] == "COMPLETE"
    assert enriched["score_reason"] == "strong liquidity"
    assert enriched["why_selected"]
    assert enriched["confidence_score"] == 0.0
