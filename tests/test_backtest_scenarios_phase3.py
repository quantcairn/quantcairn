from __future__ import annotations

from src.backtest.scenarios import available_scenarios, generate_scenario


def test_available_scenarios_cover_all_requested_cases():
    names = available_scenarios()
    assert len(names) == 12
    assert "stable_range" in names
    assert "regime_transition" in names


def test_scenarios_are_reproducible_and_serializable():
    left = generate_scenario("stable_range", seed=123)
    right = generate_scenario("stable_range", seed=123)

    assert left.to_dict() == right.to_dict()
    assert len(left.symbol_bars) == len(left.benchmark_bars)
    assert left.expected_regime == "RANGE"
    assert left.to_dict()["metadata"]["seed"] == 123


def test_regime_transition_marks_expected_behavior():
    scenario = generate_scenario("regime_transition")
    payload = scenario.to_dict()
    assert payload["expected_regime"] == "RANGE"
    assert payload["expected_strategy_behavior"] == "transition_sensitive"
    assert payload["symbol_bars"][0]["symbol"] == "SOXS.US"
