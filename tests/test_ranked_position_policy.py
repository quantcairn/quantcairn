from src.config.loader import PositionPolicyConfig
from src.engine.ranked_position_policy import calculate_ranked_target_allocations


def _candidate(symbol, *, asset_type="common_stock", price=10.0):
    return {
        "symbol": symbol,
        "ticker": symbol,
        "asset_type": asset_type,
        "current_price": price,
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "candidate_score": 90.0,
        "score_reason": "ranked",
    }


def _weights(result):
    return [row["capped_target_weight"] for row in result["target_allocations"]]


def test_top3_common_stocks_allocate_35_25_15_and_75_percent_total():
    result = calculate_ranked_target_allocations(
        [_candidate("AAPL"), _candidate("SOFI"), _candidate("NVDA")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    assert _weights(result) == [0.35, 0.25, 0.15]
    assert result["gross_target_exposure"] == 0.75
    assert result["cash_reserve_target"] == 0.25


def test_selected_top_n_one_does_not_redistribute_unused_weight():
    result = calculate_ranked_target_allocations(
        [_candidate("AAPL")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    assert _weights(result) == [0.35]
    assert result["gross_target_exposure"] == 0.35


def test_selected_top_n_two_does_not_use_top3_weight():
    result = calculate_ranked_target_allocations(
        [_candidate("AAPL"), _candidate("SOFI")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    assert _weights(result) == [0.35, 0.25]
    assert result["gross_target_exposure"] == 0.60


def test_leveraged_inverse_top1_is_capped_to_15_percent_without_redistribution():
    result = calculate_ranked_target_allocations(
        [_candidate("SOXS", asset_type="inverse_etf"), _candidate("SOFI"), _candidate("AAPL")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    rows = result["target_allocations"]
    assert rows[0]["capped_target_weight"] == 0.15
    assert rows[0]["allocation_reasons"] == ["leveraged_inverse_position_limit"]
    assert [row["capped_target_weight"] for row in rows] == [0.15, 0.25, 0.15]
    assert result["gross_target_exposure"] == 0.55


def test_two_leveraged_inverse_candidates_only_allow_higher_ranked_one():
    result = calculate_ranked_target_allocations(
        [
            _candidate("SOXS", asset_type="inverse_etf"),
            _candidate("DRIP", asset_type="inverse_etf"),
            _candidate("AAPL"),
        ],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    rows = result["target_allocations"]
    assert rows[0]["allocation_status"] == "ELIGIBLE"
    assert rows[1]["allocation_status"] == "BLOCKED"
    assert rows[1]["allocation_reason"] == "leveraged_inverse_count_limit"
    assert rows[2]["allocation_status"] == "ELIGIBLE"


def test_standard_etf_is_capped_at_30_percent():
    result = calculate_ranked_target_allocations(
        [_candidate("SPY", asset_type="etf")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    row = result["target_allocations"][0]
    assert row["capped_target_weight"] == 0.30
    assert row["allocation_reason"] == "standard_etf_position_limit"


def test_existing_positions_count_against_open_position_limit():
    result = calculate_ranked_target_allocations(
        [_candidate("AAPL")],
        account_equity=10_000.0,
        current_positions={
            "SOFI": {"market_value": 1000.0, "quantity": 10},
            "NVDA": {"market_value": 1000.0, "quantity": 10},
            "MSFT": {"market_value": 1000.0, "quantity": 10},
        },
        current_cash=7_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    row = result["target_allocations"][0]
    assert row["allocation_status"] == "BLOCKED"
    assert row["allocation_reason"] == "max_open_positions_reached"


def test_existing_same_symbol_is_not_increased_by_default():
    result = calculate_ranked_target_allocations(
        [_candidate("AAPL")],
        account_equity=10_000.0,
        current_positions={"AAPL": {"market_value": 1000.0, "quantity": 10}},
        current_cash=9_000.0,
        policy=PositionPolicyConfig(paper_position_policy_enabled=True),
    )

    row = result["target_allocations"][0]
    assert row["allocation_status"] == "BLOCKED"
    assert row["allocation_reason"] == "position_already_exists"


def test_invalid_or_degraded_selection_blocks_allocations():
    invalid = calculate_ranked_target_allocations(
        [_candidate("AAPL")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        result_quality="INVALID",
    )
    degraded = calculate_ranked_target_allocations(
        [_candidate("AAPL")],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
    )

    assert invalid["target_allocations"][0]["allocation_reason"] == "result_quality_not_complete"
    assert degraded["target_allocations"][0]["allocation_reason"] == "result_quality_not_complete"


def test_ineligible_candidate_is_not_allocated():
    candidate = _candidate("AAPL")
    candidate["scoring_eligible"] = False
    result = calculate_ranked_target_allocations(
        [candidate],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
    )

    row = result["target_allocations"][0]
    assert row["allocation_status"] == "BLOCKED"
    assert row["allocation_reason"] == "symbol_not_in_formal_top"


def test_selected_top_n_zero_has_no_allocations():
    result = calculate_ranked_target_allocations(
        [],
        account_equity=10_000.0,
        current_positions={},
        current_cash=10_000.0,
    )

    assert result["target_allocations"] == []
    assert result["gross_target_exposure"] == 0.0


def test_config_missing_defaults_to_legacy_disabled_policy():
    policy = PositionPolicyConfig()

    assert policy.mode == "legacy"
    assert policy.paper_position_policy_enabled is False
    assert policy.live_position_policy_enabled is False
    assert policy.rank_target_weights == {1: 0.35, 2: 0.25, 3: 0.15}
