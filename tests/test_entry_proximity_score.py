from __future__ import annotations

import pytest

from src.ai_selector.range_score import RangeFitnessScorer


@pytest.mark.parametrize(
    "market_data, expected_quality, expected_good, expected_min_score",
    [
        ({"current_price": 100.5, "support_price": 100.0, "resistance_price": 105.0}, "excellent", True, 95.0),
        ({"current_price": 101.75, "support_price": 100.0, "resistance_price": 105.0}, "good", True, 80.0),
        ({"current_price": 102.75, "support_price": 100.0, "resistance_price": 105.0}, "neutral", False, 55.0),
        ({"current_price": 103.5, "support_price": 100.0, "resistance_price": 105.0}, "poor", False, 30.0),
        ({"current_price": 104.75, "support_price": 100.0, "resistance_price": 105.0}, "very_poor", False, 10.0),
    ],
)
def test_entry_proximity_score_thresholds(market_data, expected_quality, expected_good, expected_min_score):
    scorer = RangeFitnessScorer()
    payload = scorer.calculate("SOFI", market_data)
    entry = payload["entry"]

    assert entry["entry_quality"] == expected_quality
    assert entry["good_for_entry_now"] is expected_good
    assert 0.0 <= entry["entry_proximity_score"] <= 100.0
    assert entry["entry_proximity_score"] >= expected_min_score


def test_entry_proximity_blocks_new_entry_near_resistance():
    scorer = RangeFitnessScorer()
    payload = scorer.calculate(
        "SOFI",
        {
            "current_price": 19.9,
            "support_price": 10.0,
            "resistance_price": 20.0,
        },
    )

    entry = payload["entry"]
    assert entry["entry_quality"] == "very_poor"
    assert entry["good_for_entry_now"] is False
    assert entry["entry_reason"] == "price_near_resistance_not_suitable_for_new_entry"


def test_entry_proximity_demotes_far_from_support():
    scorer = RangeFitnessScorer()
    payload = scorer.calculate(
        "SOFI",
        {
            "current_price": 10.9,
            "support_price": 10.0,
            "resistance_price": 20.0,
        },
    )

    entry = payload["entry"]
    assert entry["dist_to_support"] >= 8.0
    assert entry["entry_quality"] == "poor"
    assert entry["good_for_entry_now"] is False
    assert entry["entry_reason"] == "price_far_from_support_not_suitable_for_new_entry"


def test_entry_proximity_handles_missing_data():
    scorer = RangeFitnessScorer()
    payload = scorer.calculate("SOFI", {"current_price": 18.0})

    entry = payload["entry"]
    assert entry["entry_quality"] == "unknown"
    assert entry["good_for_entry_now"] is False
    assert entry["entry_reason"] == "missing_support_resistance_or_price"
    assert entry["range_position"] is None
