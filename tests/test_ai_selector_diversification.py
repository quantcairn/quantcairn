import pytest

from src.openalpha.selector import AIStrategySelector


@pytest.fixture(autouse=True)
def _offline_preflight(monkeypatch):
    """Selector diversification tests use synthetic candidates, not Yahoo."""
    from src.openalpha.preflight import PreflightReport

    monkeypatch.setattr(
        "src.openalpha.preflight.run_preflight",
        lambda **kwargs: PreflightReport(
            selection_run_id=str(kwargs.get("selection_run_id") or "offline-test"),
            run_mode="FULL",
            data_mode="UNAVAILABLE",
        ),
    )
    monkeypatch.setattr(
        "src.openalpha.selector._apply_quality_filters_with_report",
        lambda candidates, **kwargs: (list(candidates), {"timed_out": False, "rows": []}),
    )


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


def test_top5_selection_spreads_across_sectors():
    selector = AIStrategySelector()
    candidates = [
        _candidate("A1", "Semiconductors", 95.0, 1),
        _candidate("B1", "Technology", 94.0, 9),
        _candidate("C1", "Consumer Discretionary", 93.0, 13),
        _candidate("D1", "Healthcare", 92.0, 17),
        _candidate("E1", "Energy", 91.0, 21),
        _candidate("A2", "Semiconductors", 90.0, 1),
    ]

    selected = selector._select_diversified_top_k(candidates, 5)

    assert len(selected) == 5
    sectors = [item["sector"] for item in selected]
    assert len(set(sectors)) == 5
    assert selected[0]["ticker"] == "A1"
    assert selected[-1]["ticker"] == "E1"


def test_selector_falls_back_when_live_scoring_returns_too_few(monkeypatch):
    selector = AIStrategySelector()
    monkeypatch.setenv("OPENALPHA_LIVE_DATA", "1")
    monkeypatch.setenv("OPENALPHA_UNIVERSE", "sample")
    monkeypatch.setenv("OPENALPHA_TOP_K", "3")
    selector.selection_size = 3
    monkeypatch.setattr("src.openalpha.config_writer.write_top_configs", lambda top_items: None)
    def _passthrough_quality_filters(candidates, **_kwargs):
        candidate_list = list(candidates)
        return candidate_list, {
            "generated_at": "2026-08-03T00:00:00",
            "total_candidates_before_filters": len(candidate_list),
            "removed_by_volume_filter": 0,
            "removed_by_spread_filter": 0,
            "removed_by_volatility_filter": 0,
            "removed_due_to_missing_data": 0,
            "final_selected_symbols": [item.get("ticker") for item in candidate_list],
            "existing_real_positions_preserved": [],
            "timed_out": False,
            "rows": [],
        }

    monkeypatch.setattr("src.openalpha.selector._apply_quality_filters_with_report", _passthrough_quality_filters)

    selector.universe._load_local_snapshot = lambda: ["AAA", "BBB", "CCC"]
    selector.news.collect_for_symbols = lambda symbols: {symbol: [] for symbol in symbols}

    live_item = _candidate("AAA", "Technology", 90.0, 1)
    fallback_items = [
        _candidate("AAA", "Technology", 90.0, 1),
        _candidate("BBB", "Energy", 88.0, 2),
        _candidate("CCC", "Financials", 87.0, 3),
    ]

    calls = []

    def fake_score(symbols, news_map, live_enabled):
        calls.append(live_enabled)
        return [live_item] if live_enabled else fallback_items

    selector._score_with_live_flag = fake_score

    result = selector.run_selection()

    assert calls == [True, False]
    assert len(result["top5"]) == 3
    assert result["settings"]["fallback_used"] is True
    assert result["settings"]["data_mode"] == "mixed"


def test_selector_can_defer_config_writes_until_positions_are_verified(monkeypatch):
    selector = AIStrategySelector()
    selector.selection_size = 1
    selector.universe._load_local_snapshot = lambda: ["AAA"]
    selector.news.collect_for_symbols = lambda symbols: {symbol: [] for symbol in symbols}
    selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
        _candidate("AAA", "Technology", 90.0, 1)
    ]
    writes = []
    monkeypatch.setattr(
        "src.openalpha.config_writer.write_top_configs",
        lambda top_items: writes.append(top_items),
    )

    result = selector.run_selection(write_configs=False)

    assert result["top5"][0]["ticker"] == "AAA"
    assert writes == []
