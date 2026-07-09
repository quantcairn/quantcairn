from __future__ import annotations

from src.ai_selector.composition_filter import CompositionFilter
from src.portfolio.risk_allocator import RiskAllocator


def _candidate(ticker: str, score: float, leveraged: bool | None = None) -> dict:
    payload = {
        "ticker": ticker,
        "final_score": score,
        "score": score,
        "range_score": score,
        "confidence": 0.8,
        "reason": f"stub:{ticker}",
        "source": "stub",
    }
    if leveraged is not None:
        payload["leveraged_etf"] = leveraged
    return payload


def test_top3_allows_at_most_one_leveraged_etf():
    result = CompositionFilter().filter_top_n(
        [
            _candidate("SOXS", 99.0),
            _candidate("DRIP", 98.0),
            _candidate("AAPL", 97.0),
            _candidate("MSFT", 96.0),
        ],
        top_n=3,
    )

    accepted = result["accepted"]
    rejected = result["rejected"]

    assert len(accepted) == 3
    assert sum(1 for item in accepted if item.get("leveraged_etf")) == 1
    assert any(item["ticker"] == "DRIP" for item in rejected)
    assert rejected[0]["reason"] == "leveraged_etf_limit_exceeded"


def test_non_leveraged_stocks_fill_remaining_slots():
    result = CompositionFilter().filter_top_n(
        [
            _candidate("LABD", 99.0),
            _candidate("SOXS", 98.0),
            _candidate("AAPL", 97.0),
            _candidate("MSFT", 96.0),
        ],
        top_n=3,
    )

    assert [item["ticker"] for item in result["accepted"]] == ["LABD", "AAPL", "MSFT"]
    assert sum(1 for item in result["accepted"] if item.get("leveraged_etf")) == 1
    assert any(item["ticker"] == "SOXS" for item in result["rejected"])


def test_reject_reason_and_warnings_are_present_when_top3_not_filled():
    result = CompositionFilter().filter_top_n(
        [
            _candidate("AAPL", 99.0),
            _candidate("MSFT", 98.0),
        ],
        top_n=3,
    )

    assert [item["ticker"] for item in result["accepted"]] == ["AAPL", "MSFT"]
    assert result["warnings"]
    assert result["warnings"][0].startswith("top_n_not_filled:")
    assert result["rejected"] == []


def test_risk_allocator_uses_filtered_accepted_list():
    result = CompositionFilter().filter_top_n(
        [
            _candidate("SOXS", 99.0),
            _candidate("DRIP", 98.0),
            _candidate("AAPL", 97.0),
            _candidate("MSFT", 96.0),
        ],
        top_n=3,
    )

    allocations = RiskAllocator().allocate_positions(
        result["accepted"],
        total_capital=10_000.0,
        reserve_cash=100.0,
    )

    assert "DRIP" not in allocations
    assert set(allocations).issubset({"SOXS", "AAPL", "MSFT"})
