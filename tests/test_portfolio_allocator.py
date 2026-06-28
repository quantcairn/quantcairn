from __future__ import annotations

import math

from src.portfolio.allocator import PortfolioAllocator


def test_allocator_normalizes_risk_adjusted_weights():
    allocator = PortfolioAllocator()
    allocation = allocator.allocate(
        [
            {"ticker": "AAPL", "score": 80, "volatility": 0.02, "regime": "TREND"},
            {"ticker": "MSFT", "score": 20, "volatility": 0.04, "regime": "TREND"},
        ]
    )

    assert set(allocation) == {"AAPL", "MSFT"}
    total = sum(item["position_size"] for item in allocation.values())
    assert math.isclose(total, 1.0, rel_tol=1e-9)
    assert math.isclose(allocation["AAPL"]["position_size"], 8 / 9, rel_tol=1e-9)
    assert math.isclose(allocation["MSFT"]["position_size"], 1 / 9, rel_tol=1e-9)
    assert allocation["AAPL"]["leverage"] == 1.0


def test_allocator_drops_invalid_signals():
    allocator = PortfolioAllocator()
    allocation = allocator.allocate(
        [
            {"ticker": "", "score": 80, "volatility": 0.02, "regime": "TREND"},
            {"ticker": "TSLA", "score": 0, "volatility": 0.02, "regime": "TREND"},
        ]
    )
    assert allocation == {}
