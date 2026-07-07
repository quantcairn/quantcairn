from scripts.run_ai_selector import _pin_live_positions, _split_selected_and_protected_positions


def _pick(ticker: str, score: float) -> dict:
    return {
        "ticker": ticker,
        "score": score,
        "range_low": 10.0,
        "range_high": 12.0,
        "risk": {"stop_loss_pct": 1.5},
        "size": 10,
    }


def test_live_equity_positions_are_pinned_into_top5():
    selected = [_pick(symbol, 100 - idx) for idx, symbol in enumerate(
        ["WULF", "QBTS", "SOFI", "PLTR", "QCOM"]
    )]
    positions = [
        {"ticker": "SOFI", "quantity": 30, "current_price": 18.44},
        {"ticker": "SOXS", "quantity": 132, "current_price": 3.86},
    ]

    result = _pin_live_positions(selected, positions, limit=5)

    assert [item["ticker"] for item in result[:2]] == ["SOFI", "SOXS"]
    assert len(result) == 5
    assert all(item.get("pinned_live_position") for item in result[:2])
    assert result[0]["existing_position"] is True
    assert result[0]["reduce_only"] is False
    assert result[1]["existing_position"] is True
    assert result[1]["reduce_only"] is True
    assert "QCOM" not in [item["ticker"] for item in result]


def test_option_like_positions_do_not_consume_top_slots():
    selected = [_pick("SOFI", 90.0)]
    positions = [
        {"ticker": "SPCX260717C265000", "quantity": 2, "current_price": 0.6},
    ]

    result = _pin_live_positions(selected, positions, limit=5)

    assert [item["ticker"] for item in result] == ["SOFI"]


def test_live_equity_positions_can_be_monitored_without_consuming_top_slots():
    selected = [_pick(symbol, 100 - idx) for idx, symbol in enumerate(
        ["NVDA", "SOXL", "SOXS", "LABU"]
    )]
    positions = [
        {"ticker": "NVDA", "quantity": 4, "current_price": 196.5},
    ]

    tradable, protected = _split_selected_and_protected_positions(selected, positions, limit=3)

    assert [item["ticker"] for item in tradable] == ["SOXL", "SOXS", "LABU"]
    assert [item["ticker"] for item in protected] == ["NVDA"]
    assert protected[0]["protected_position"] is True
