from __future__ import annotations

from src.ai_selector.universe_filter import evaluate_universe_candidate, filter_universe_candidates, load_universe_rules
from src.candidate_validation.models import CandidateRecord, ValidationStatus


def _candidate(ticker: str, **kwargs):
    item = {
        "ticker": ticker,
        "current_price": 20.0,
        "average_dollar_volume_20d": 100_000_000,
        "atr_20_percentage": 3.0,
        "market_cap": 10_000_000_000,
        "asset_type": "common_stock",
    }
    item.update(kwargs)
    return item


def test_common_stock_aapl_150_passes():
    result = evaluate_universe_candidate(_candidate("AAPL", current_price=150.0))

    assert result.rejected is False
    assert result.asset_type == "common_stock"


def test_inverse_etf_soxs_20_passes_without_market_cap():
    result = evaluate_universe_candidate(
        _candidate(
            "SOXS",
            current_price=20.0,
            asset_type="inverse_etf",
            market_cap=None,
            average_dollar_volume_20d=30_000_000,
            atr_20_percentage=5.0,
        )
    )

    assert result.rejected is False
    assert result.asset_type == "inverse_etf"


def test_brka_super_high_price_rejected():
    result = evaluate_universe_candidate(_candidate("BRK.A", current_price=600_000.0))

    assert result.rejected is True
    assert "price_out_of_range" in result.rejection_reason


def test_low_dollar_volume_rejected():
    result = evaluate_universe_candidate(_candidate("LOWVOL", average_dollar_volume_20d=1_000_000))

    assert result.rejected is True
    assert "low_dollar_volume" in result.rejection_reason


def test_small_market_cap_common_stock_rejected():
    result = evaluate_universe_candidate(_candidate("SMALL", market_cap=500_000_000))

    assert result.rejected is True
    assert "market_cap_too_small" in result.rejection_reason


def test_high_volatility_junk_stock_rejected():
    result = evaluate_universe_candidate(_candidate("JUNK", atr_20_percentage=18.0))

    assert result.rejected is True
    assert "volatility_too_high" in result.rejection_reason


def test_filter_records_all_rejection_reasons():
    accepted, rejected = filter_universe_candidates(
        [
            _candidate(
                "BAD",
                current_price=1.0,
                average_dollar_volume_20d=100_000,
                market_cap=100_000_000,
                atr_20_percentage=20.0,
            )
        ]
    )

    assert accepted == []
    assert rejected[0]["rejected"] is True
    assert set(rejected[0]["rejection_reason"]) == {
        "price_out_of_range",
        "low_dollar_volume",
        "market_cap_too_small",
        "volatility_too_high",
    }


def test_config_rules_loaded_from_yaml():
    rules = load_universe_rules()

    assert rules["common_stock"].price_min == 5.0
    assert rules["common_stock"].price_max == 200.0
    assert rules["etf"].price_max == 300.0
    assert rules["leveraged_etf"].min_average_dollar_volume == 10_000_000


def test_ai_candidate_default_status_remains_ai_candidate():
    record = CandidateRecord.from_ai_candidate(
        symbol="AAPL.US",
        selected_at="2026-07-14T00:00:00Z",
        source="ai_selector",
        ai_score=80.0,
        ai_reason="universe filter smoke",
        asset_type="common_stock",
        benchmarks=("QQQ.US",),
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )

    assert record.validation_status == ValidationStatus.AI_CANDIDATE.value
    assert record.trading_enabled is False
    assert record.shadow_enabled is False
    assert record.paper_enabled is False
    assert record.live_enabled is False
