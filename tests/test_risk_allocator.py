from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from src.openalpha.config_writer import write_top_configs
from src.portfolio.risk_allocator import RiskAllocator


def _signal(ticker: str, final_score: float, atr_pct: float | None = 0.05, price: float = 10.0, regime: str = "RANGE") -> dict:
    payload = {
        "ticker": ticker,
        "final_score": final_score,
        "score": final_score,
        "atr_pct": atr_pct,
        "price": price,
        "regime": regime,
    }
    return payload


def _make_signals(count: int = 30) -> list[dict]:
    signals = []
    for i in range(count):
        signals.append(_signal(f"T{i:02d}", 50.0, 0.05, 10.0))
    return signals


def test_higher_volatility_gets_smaller_allocation():
    allocator = RiskAllocator()
    signals = _make_signals()
    signals[0] = _signal("LOWVOL", 80.0, 0.02, 10.0)
    signals[1] = _signal("HIGHVOL", 80.0, 0.10, 10.0)

    allocations = allocator.allocate_positions(signals, total_capital=10_000.0, reserve_cash=100.0)

    assert allocations["LOWVOL"]["capital"] > allocations["HIGHVOL"]["capital"]
    assert allocations["LOWVOL"]["shares"] > allocations["HIGHVOL"]["shares"]


def test_higher_score_gets_larger_allocation():
    allocator = RiskAllocator()
    signals = _make_signals()
    signals[0] = _signal("HIGHSCORE", 90.0, 0.05, 10.0)
    signals[1] = _signal("LOWSCORE", 60.0, 0.05, 10.0)

    allocations = allocator.allocate_positions(signals, total_capital=10_000.0, reserve_cash=100.0)

    assert allocations["HIGHSCORE"]["capital"] > allocations["LOWSCORE"]["capital"]
    assert allocations["HIGHSCORE"]["weight"] > allocations["LOWSCORE"]["weight"]


def test_event_regime_gets_zero_allocation():
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(
        [_signal("EVENT", 90.0, 0.05, 10.0, regime="EVENT")],
        total_capital=10_000.0,
        reserve_cash=100.0,
    )

    assert allocations["EVENT"]["capital"] == 0.0
    assert allocations["EVENT"]["shares"] == 0
    assert allocations["EVENT"]["reason"] == "event_regime"


def test_total_allocation_does_not_exceed_available_capital():
    allocator = RiskAllocator()
    signals = _make_signals()
    allocations = allocator.allocate_positions(signals, total_capital=10_000.0, reserve_cash=100.0)

    total_capital = sum(item["capital"] for item in allocations.values())
    assert total_capital <= 9_900.0


def test_single_ticket_does_not_exceed_fifteen_percent_cap():
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(
        [_signal("SOXS", 95.0, 0.03, 10.0)],
        total_capital=10_000.0,
        reserve_cash=100.0,
    )

    assert allocations["SOXS"]["capital"] <= 1_485.0


def test_reserve_cash_is_preserved():
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(
        [_signal("SOXS", 90.0, 0.05, 10.0)],
        total_capital=120.0,
        reserve_cash=100.0,
    )

    assert allocations["SOXS"]["capital"] == 0.0
    assert allocations["SOXS"]["shares"] == 0


def test_price_non_positive_results_in_zero_shares():
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(
        [_signal("SOXS", 90.0, 0.05, 0.0)],
        total_capital=10_000.0,
        reserve_cash=100.0,
    )

    assert allocations["SOXS"]["shares"] == 0
    assert allocations["SOXS"]["capital"] == 0.0


def test_missing_atr_uses_default_value():
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(
        [_signal("SOXS", 90.0, None, 10.0)],
        total_capital=10_000.0,
        reserve_cash=100.0,
    )

    assert allocations["SOXS"]["atr_pct"] == 0.05


def test_output_fields_are_complete():
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(
        [_signal("SOXS", 90.0, 0.08, 10.0)],
        total_capital=10_000.0,
        reserve_cash=100.0,
    )

    payload = allocations["SOXS"]
    assert set(payload) == {"capital", "shares", "weight", "risk_pct", "atr_pct", "reason"}


def test_risk_allocator_is_written_into_top_yaml():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        configs_dir = repo_root / "configs"
        configs_dir.mkdir()
        (configs_dir / "TOP1.yaml").write_text("ticker: OLD\nmode: paper\n", encoding="utf-8")
        from src.openalpha import config_writer

        original_base = config_writer.BASE
        try:
            config_writer.BASE = str(repo_root)
            write_top_configs([
                {
                    "ticker": "SOXS",
                    "range_low": 4.5,
                    "range_high": 5.5,
                    "ai_score": 90.0,
                    "range_score": 80.0,
                    "final_score": 88.0,
                    "confidence": 0.75,
                    "trade_filter_passed": True,
                    "reject_reason": "",
                    "fallback_used": False,
                    "size": 10,
                }
            ])
        finally:
            config_writer.BASE = original_base

        payload = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
        assert "allocation" in payload
        assert set(payload["allocation"]) == {"target_capital", "target_shares", "weight", "atr_pct", "risk_pct", "reason"}
