"""Tests for src.ai_selector.tradable_filter."""
from src.openalpha.tradable_filter import filter_candidates


def _cand(ticker, **kw):
    item = {"ticker": ticker}
    item.update(kw)
    return item


def test_empty_candidates():
    assert filter_candidates([]) == []


def test_passes_good_candidate():
    result = filter_candidates([
        _cand("SOXS", current_price=5.0, range_low=4.5, range_high=6.0,
              bid=4.99, ask=5.01, avg_volume=5_000_000, atr_pct=4.0,
              day_change_pct=1.0)
    ])
    assert len(result) == 1
    assert result[0]["tradable_filter_reason"] == "passed"


def test_rejects_event_regime():
    result = filter_candidates([
        _cand("SOXS", regime="EVENT")
    ])
    assert len(result) == 0


def test_rejects_wide_spread():
    result = filter_candidates([
        _cand("SOXS", bid=5.0, ask=6.0, current_price=5.5)
    ])
    assert len(result) == 0  # spread = (6-5)/5 = 20% > 0.5%


def test_rejects_large_premarket_gap():
    result = filter_candidates([
        _cand("SOXS", premarket_change_pct=6.0, current_price=5.0)
    ])
    assert len(result) == 0


def test_rejects_large_day_change():
    result = filter_candidates([
        _cand("SOXS", day_change_pct=-9.0, current_price=5.0)
    ])
    assert len(result) == 0


def test_rejects_low_volume():
    result = filter_candidates([
        _cand("SOXS", avg_volume=500_000, current_price=5.0)
    ])
    assert len(result) == 0


def test_rejects_high_atr():
    result = filter_candidates([
        _cand("SOXS", atr_pct=15.0, current_price=5.0)
    ])
    assert len(result) == 0


def test_rejects_far_from_support():
    result = filter_candidates([
        _cand("SOXS", current_price=10.0, range_low=5.0, range_high=15.0)
    ])
    assert len(result) == 0  # 100% above support > 15%


def test_market_data_overrides():
    md = {"SOXS": {"bid": 5.0, "ask": 6.0}}
    result = filter_candidates([
        _cand("SOXS", current_price=5.5)
    ], market_data=md)
    assert len(result) == 0  # spread = 20%


def test_mixed_pool():
    candidates = [
        _cand("GOOD1", current_price=10.0, range_low=9.5, range_high=11.0,
              bid=9.99, ask=10.01, avg_volume=2_000_000, atr_pct=3.0,
              day_change_pct=0.5),
        _cand("BAD1", current_price=10.0, range_low=5.0, range_high=15.0,
              bid=9.0, ask=11.0, avg_volume=2_000_000),
        _cand("BAD2", avg_volume=100_000),
    ]
    result = filter_candidates(candidates)
    assert len(result) == 1
    assert result[0]["ticker"] == "GOOD1"


def run_test_direct():
    test_empty_candidates()
    test_passes_good_candidate()
    test_rejects_event_regime()
    test_rejects_wide_spread()
    test_rejects_large_premarket_gap()
    test_rejects_large_day_change()
    test_rejects_low_volume()
    test_rejects_high_atr()
    test_rejects_far_from_support()
    test_market_data_overrides()
    test_mixed_pool()
