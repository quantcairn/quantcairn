from __future__ import annotations

from types import SimpleNamespace

from src.openalpha.market_regime import analyze_market_regime
from src.openalpha.strategy_selection import build_strategy_research
from src.candidate_validation.outcome_dataset import CandidateOutcomeSample


def _selection_report(*rows):
    return {
        "selection_stage": "FINALIZED",
        "selection_date": "2026-07-15",
        "fallback_used": False,
        "top10": list(rows),
    }


def _row(
    *,
    candidate_id: str,
    symbol: str,
    asset_type: str,
    strategy_family: str,
    candidate_score: float,
    liquidity_score: float,
    trend_score: float,
    volatility_score: float,
    risk_score: float,
    strategy_fit_score: float,
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    adx: float,
    relative_strength_vs_spy: float,
    average_dollar_volume_20d: float,
    spread_pct: float,
    gap_pct: float,
    data_status: str = "VALID",
    freshness_status: str = "SAFE",
    benchmark_status: str = "VALID",
    scoring_eligible: bool = True,
    trade_filter_passed: bool = True,
    benchmark_symbols: tuple[str, ...] = ("QQQ.US", "SPY.US"),
):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "asset_type": asset_type,
        "strategy_family": strategy_family,
        "candidate_score": candidate_score,
        "liquidity_score": liquidity_score,
        "trend_score": trend_score,
        "volatility_score": volatility_score,
        "risk_score": risk_score,
        "strategy_fit_score": strategy_fit_score,
        "data_status": data_status,
        "freshness_status": freshness_status,
        "benchmark_status": benchmark_status,
        "scoring_eligible": scoring_eligible,
        "trade_filter_passed": trade_filter_passed,
        "trade_market_data": {
            "current_price": current_price,
            "ma20": ma20,
            "ma50": ma50,
            "ma200": ma200,
            "adx": adx,
            "relative_strength_vs_SPY": relative_strength_vs_spy,
            "average_dollar_volume_20d": average_dollar_volume_20d,
            "spread_pct": spread_pct,
            "gap_pct": gap_pct,
            "benchmark_change_pct": {"QQQ.US": 1.5, "SPY.US": 1.2},
            "benchmark_symbols": list(benchmark_symbols),
            "selection_stage": "FINALIZED",
            "data_status": data_status,
            "freshness_status": freshness_status,
            "benchmark_status": benchmark_status,
        },
    }


def test_market_regime_identifies_uptrend_from_structured_indicators():
    report = _selection_report(
        _row(
            candidate_id="cand-1",
            symbol="AAPL.US",
            asset_type="common_stock",
            strategy_family="trend_following",
            candidate_score=88.0,
            liquidity_score=94.0,
            trend_score=91.0,
            volatility_score=76.0,
            risk_score=83.0,
            strategy_fit_score=92.0,
            current_price=200.0,
            ma20=195.0,
            ma50=190.0,
            ma200=180.0,
            adx=26.0,
            relative_strength_vs_spy=12.0,
            average_dollar_volume_20d=120_000_000.0,
            spread_pct=0.08,
            gap_pct=1.2,
        )
    )

    regime = analyze_market_regime(report)

    assert regime["regime"] == "STRONG_UPTREND"
    assert regime["actionable"] is True
    assert regime["selection_outcome"] == "ACTIONABLE_RESEARCH_CANDIDATE"
    assert regime["confidence"] >= 0.68
    assert regime["coverage"]["benchmark_symbols"] == ["QQQ.US", "SPY.US"]
    assert "trend_alignment_bullish" in regime["regime_reasons"]


def test_strategy_selection_returns_no_actionable_candidate_when_data_is_insufficient():
    report = _selection_report(
        _row(
            candidate_id="cand-2",
            symbol="SOXS.US",
            asset_type="inverse_etf",
            strategy_family="inverse_range",
            candidate_score=62.0,
            liquidity_score=58.0,
            trend_score=44.0,
            volatility_score=71.0,
            risk_score=55.0,
            strategy_fit_score=60.0,
            current_price=19.0,
            ma20=18.5,
            ma50=18.0,
            ma200=17.5,
            adx=17.0,
            relative_strength_vs_spy=3.0,
            average_dollar_volume_20d=18_000_000.0,
            spread_pct=0.18,
            gap_pct=0.8,
            data_status="INVALID",
            freshness_status="INVALID",
            benchmark_status="INVALID",
            scoring_eligible=False,
            trade_filter_passed=False,
            benchmark_symbols=("SOXX.US", "SMH.US"),
        )
    )

    strategy = build_strategy_research(
        report,
        dataset_samples=[],
        performance_snapshot={"score_bucket_distribution": []},
        model_evaluation={"calibration_curve": []},
    )

    assert strategy["selection_outcome"] == "NO_ACTIONABLE_RESEARCH_CANDIDATE"
    assert strategy["final_selected_count"] == 0
    assert strategy["actionable_candidate_status"] == "NO_ACTIONABLE_RESEARCH_CANDIDATE"
    assert strategy["candidate_strategy_matrix"][0]["selected"] is False
    assert strategy["candidate_strategy_matrix"][0]["blocked_reason"] in {
        "eligibility_blocked",
        "regime_not_allowed",
        "benchmark_invalid",
        "freshness_invalid",
    }


def test_strategy_selection_applies_composition_constraints_and_is_deterministic():
    report = _selection_report(
        _row(
            candidate_id="cand-3",
            symbol="SOXS.US",
            asset_type="inverse_etf",
            strategy_family="inverse_range",
            candidate_score=90.0,
            liquidity_score=96.0,
            trend_score=87.0,
            volatility_score=82.0,
            risk_score=88.0,
            strategy_fit_score=95.0,
            current_price=20.0,
            ma20=19.5,
            ma50=19.1,
            ma200=18.9,
            adx=24.0,
            relative_strength_vs_spy=8.0,
            average_dollar_volume_20d=180_000_000.0,
            spread_pct=0.04,
            gap_pct=0.6,
            benchmark_symbols=("SOXX.US", "SMH.US"),
        ),
        _row(
            candidate_id="cand-4",
            symbol="SOXL.US",
            asset_type="leveraged_etf",
            strategy_family="leveraged_range",
            candidate_score=89.0,
            liquidity_score=95.0,
            trend_score=84.0,
            volatility_score=81.0,
            risk_score=84.0,
            strategy_fit_score=94.0,
            current_price=30.0,
            ma20=29.0,
            ma50=28.5,
            ma200=27.8,
            adx=23.0,
            relative_strength_vs_spy=7.0,
            average_dollar_volume_20d=110_000_000.0,
            spread_pct=0.05,
            gap_pct=0.7,
            benchmark_symbols=("SOXX.US", "SMH.US"),
        ),
    )
    samples = [
        CandidateOutcomeSample(
            candidate_id="h1",
            symbol="SOXS.US",
            selection_date="2026-07-01T00:00:00+00:00",
            data_as_of="2026-07-01T00:00:00+00:00",
            scoring_version="v1",
            liquidity_score=90.0,
            trend_score=80.0,
            volatility_score=78.0,
            risk_score=80.0,
            strategy_fit_score=88.0,
            candidate_score=86.0,
            recommended_strategy="inverse_range",
            data_quality_score=92.0,
            validation_result="DATA_VALID",
            backtest_result="BACKTEST_COMPLETE",
            walk_forward_result="WALK_FORWARD_COMPLETE",
            shadow_result="SHADOW_COMPLETE",
            paper_result="PAPER_ELIGIBLE",
            outcome_window="2026-06-01 -> 2026-06-30",
            label_data_valid=True,
            label_backtest_pass=True,
            label_walk_forward_pass=True,
            label_positive_return=True,
            label_max_drawdown_ok=True,
            outcome_return=0.12,
            outcome_max_drawdown=0.04,
        ),
        CandidateOutcomeSample(
            candidate_id="h2",
            symbol="SOXL.US",
            selection_date="2026-07-02T00:00:00+00:00",
            data_as_of="2026-07-02T00:00:00+00:00",
            scoring_version="v1",
            liquidity_score=88.0,
            trend_score=79.0,
            volatility_score=81.0,
            risk_score=79.0,
            strategy_fit_score=87.0,
            candidate_score=84.0,
            recommended_strategy="leveraged_range",
            data_quality_score=90.0,
            validation_result="DATA_VALID",
            backtest_result="BACKTEST_COMPLETE",
            walk_forward_result="WALK_FORWARD_COMPLETE",
            shadow_result="SHADOW_COMPLETE",
            paper_result="PAPER_ELIGIBLE",
            outcome_window="2026-06-01 -> 2026-06-30",
            label_data_valid=True,
            label_backtest_pass=True,
            label_walk_forward_pass=True,
            label_positive_return=True,
            label_max_drawdown_ok=True,
            outcome_return=0.10,
            outcome_max_drawdown=0.05,
        ),
    ]

    baseline = build_strategy_research(
        report,
        dataset_samples=samples,
        performance_snapshot={
            "score_bucket_distribution": [
                {"score_bucket": "80-90", "backtest_complete_rate": 100.0, "walk_forward_complete_rate": 100.0},
            ]
        },
        model_evaluation={
            "calibration_curve": [
                {"lower_bound": 80.0, "upper_bound": 90.0, "probability": 0.78},
            ]
        },
    )
    repeated = build_strategy_research(
        report,
        dataset_samples=samples,
        performance_snapshot={
            "score_bucket_distribution": [
                {"score_bucket": "80-90", "backtest_complete_rate": 100.0, "walk_forward_complete_rate": 100.0},
            ]
        },
        model_evaluation={
            "calibration_curve": [
                {"lower_bound": 80.0, "upper_bound": 90.0, "probability": 0.78},
            ]
        },
    )

    def _normalize(payload: dict[str, object]) -> dict[str, object]:
        result = dict(payload)
        result.pop("generated_at", None)
        market_regime = dict(result.get("market_regime") or {})
        market_regime.pop("generated_at", None)
        market_regime.pop("now_et", None)
        result["market_regime"] = market_regime
        return result

    assert _normalize(baseline) == _normalize(repeated)
    assert baseline["strategy_candidates"][0]["expected_edge"]["probability_of_success"] >= 0.7
    assert baseline["final_selected_count"] == 1
    assert baseline["portfolio_composition"]["selected_count"] == 1
    assert baseline["portfolio_composition"]["blocked_count"] >= 0
    assert all("candidate_id" in row for row in baseline["blocked_candidates"])
