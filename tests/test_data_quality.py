from __future__ import annotations

from src.ai_selector.data_quality import (
    enrich_candidate_quality,
    evaluate_candidate_data_quality,
    formal_selection_ineligibility_reasons,
    is_formal_selection_eligible,
)


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


def test_enrich_candidate_quality_uses_complete_fetch_diagnostics_over_stale_missing_status() -> None:
    candidate = _complete_candidate()
    precheck = {
        "data_status": "VALID",
        "quote_status": "OK",
        "ohlcv_status": "MISSING",
        "history_status": "DEGRADED",
        "benchmark_status": "VALID",
        "scoring_eligible": False,
        "scoring_block_reason": "history_degraded; missing_ohlcv",
    }
    candidate.update(
        {
            "quote_status": "MISSING",
            "ohlcv_status": "MISSING",
            "history_status": "MISSING",
            "data_sufficiency": precheck,
            "quote_fetch_status": "COMPLETE",
            "ohlcv_fetch_status": "COMPLETE",
            "history_rows": 220,
            "close_history": [150.0] * 220,
            "history_available_bars": 220,
            "history_required_bars": 200,
            "history_missing_windows": [],
            "fallback_history_incomplete": True,
        }
    )

    enriched = enrich_candidate_quality(candidate)

    assert enriched["quote_status"] == "COMPLETE"
    assert enriched["ohlcv_status"] == "COMPLETE"
    assert enriched["history_status"] == "COMPLETE"
    assert enriched["data_status"] == "COMPLETE"
    assert enriched["scoring_eligible"] is True
    assert enriched["formal_scoring_eligibility"] is True
    assert enriched["market_data_sufficiency"] == "COMPLETE"
    assert enriched["record_completeness"] == "COMPLETE"
    assert enriched["precheck_data_sufficiency"] == precheck
    assert enriched["final_data_quality"]["data_status"] == "COMPLETE"
    assert enriched["quality_state_conflict"] is True
    assert set(enriched["quality_state_conflict_fields"]) >= {"ohlcv_status", "history_status", "scoring_eligible"}
    assert "missing_history" not in enriched["blocking_reasons"]


def test_history_missing_windows_fail_closed_even_when_ohlcv_fetch_succeeds() -> None:
    candidate = _complete_candidate()
    candidate.update(
        {
            "quote_fetch_status": "COMPLETE",
            "ohlcv_fetch_status": "COMPLETE",
            "history_rows": 30,
            "close_history": [150.0] * 30,
            "history_available_bars": 30,
            "history_required_bars": 200,
            "history_missing_windows": ["ma50", "ma200"],
        }
    )

    enriched = enrich_candidate_quality(candidate)

    assert enriched["ohlcv_status"] == "COMPLETE"
    assert enriched["history_status"] == "MISSING"
    assert enriched["data_status"] == "INVALID"
    assert enriched["scoring_eligible"] is False
    assert enriched["formal_scoring_eligibility"] is False
    assert enriched["market_data_sufficiency"] == "FAILED"
    assert "missing_history" in enriched["blocking_reasons"]
    assert "history_window_missing:ma50" in enriched["blocking_reasons"]
    assert "history_window_missing:ma200" in enriched["blocking_reasons"]
    assert is_formal_selection_eligible(enriched) is False


def test_formal_selection_helpers_reject_explicit_invalid_data() -> None:
    candidate = _complete_candidate()
    candidate.update(
        {
            "data_status": "INVALID",
            "scoring_eligible": False,
            "formal_scoring_eligibility": False,
            "quote_status": "MISSING",
            "ohlcv_status": "MISSING",
            "history_status": "MISSING",
            "benchmark_status": "INVALID",
            "fallback_scope": "CRITICAL_MARKET_DATA",
            "fallback_severity": "CRITICAL",
        }
    )

    reasons = formal_selection_ineligibility_reasons(candidate)

    assert is_formal_selection_eligible(candidate) is False
    assert "data_status_invalid" in reasons
    assert "scoring_eligible_false" in reasons
    assert "formal_scoring_eligibility_false" in reasons
    assert "quote_status_missing" in reasons
    assert "ohlcv_status_missing" in reasons
    assert "history_status_missing" in reasons
    assert "benchmark_status_invalid" in reasons
    assert "critical_market_data_fallback" in reasons
