from src.ai_selector.selector import AIStrategySelector


def _candidate(ticker, sector, score, corr_seed):
    returns = [0.01 * (corr_seed + i) for i in range(30)]
    return {
        "ticker": ticker,
        "sector": sector,
        "score": score,
        "base_score": score,
        "volatility_score": 75.0,
        "volume_score": 72.0,
        "trend_fit_score": 68.0,
        "repeatability_score": 66.0,
        "drawdown_safety_score": 64.0,
        "correlation_penalty": 0.0,
        "range_low": 100.0,
        "range_high": 110.0,
        "suggested_range": "$100.00 - $110.00",
        "risk": {"stop_loss_pct": 1.5},
        "series": {"returns": returns},
    }


def test_top3_selection_spreads_across_sectors():
    selector = AIStrategySelector()
    candidates = [
        _candidate("A1", "Semiconductors", 91.0, 1),
        _candidate("A2", "Semiconductors", 90.0, 1),
        _candidate("B1", "Technology", 89.0, 9),
        _candidate("C1", "Consumer Discretionary", 88.0, 13),
    ]

    selected = selector._select_diversified_top3(candidates)

    assert len(selected) == 3
    sectors = [item["sector"] for item in selected]
    assert len(set(sectors)) == 3
    assert selected[0]["ticker"] == "A1"
    assert selected[1]["ticker"] in {"B1", "C1"}
