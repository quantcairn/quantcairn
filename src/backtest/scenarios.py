from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sin, cos
import random
from typing import Any, Callable, Iterable

from .models import Bar


@dataclass(slots=True)
class ScenarioData:
    name: str
    symbol: str
    benchmark_symbol: str
    symbol_bars: list[Bar]
    benchmark_bars: list[Bar]
    expected_regime: str
    expected_strategy_behavior: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "symbol": self.symbol,
            "benchmark_symbol": self.benchmark_symbol,
            "symbol_bars": [bar.to_dict() for bar in self.symbol_bars],
            "benchmark_bars": [bar.to_dict() for bar in self.benchmark_bars],
            "expected_regime": self.expected_regime,
            "expected_strategy_behavior": self.expected_strategy_behavior,
            "metadata": dict(self.metadata),
        }


def _bar_series(
    *,
    symbol: str,
    start: datetime,
    count: int,
    generator: Callable[[int], float],
    volume: int,
    source: str,
) -> list[Bar]:
    bars: list[Bar] = []
    for index in range(count):
        ts = start + timedelta(minutes=index)
        close = max(0.01, float(generator(index)))
        high = max(close * 1.01, close + 0.05)
        low = max(0.01, min(close * 0.99, close - 0.05))
        open_ = close * (0.999 if index % 2 == 0 else 1.001)
        bid = round(max(0.01, close * 0.999), 6)
        ask = round(max(0.01, close * 1.001), 6)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=round(open_, 6),
                high=round(high, 6),
                low=round(low, 6),
                close=round(close, 6),
                volume=max(0, int(volume)),
                bid=bid,
                ask=ask,
                source=source,
            )
        )
    return bars


def _benchmark_bars(start: datetime, count: int, base: float, slope: float, seed: int, symbol: str) -> list[Bar]:
    rng = random.Random(seed)
    return _bar_series(
        symbol=symbol,
        start=start,
        count=count,
        volume=250_000,
        source="benchmark",
        generator=lambda idx: base + idx * slope + sin(idx / 6.0) * 0.1 + rng.uniform(-0.03, 0.03),
    )


def _symbol_range(
    start: datetime,
    count: int,
    base: float,
    amplitude: float,
    drift: float,
    volume: int,
    symbol: str,
    seed: int,
    source: str,
    spread_factor: float = 0.001,
) -> list[Bar]:
    rng = random.Random(seed)
    return _bar_series(
        symbol=symbol,
        start=start,
        count=count,
        volume=volume,
        source=source,
        generator=lambda idx: base + sin(idx / 4.0) * amplitude + idx * drift + rng.uniform(-amplitude * 0.05, amplitude * 0.05),
    )


def _scenario_payload(
    name: str,
    symbol: str,
    benchmark_symbol: str,
    *,
    symbol_bars: list[Bar],
    benchmark_bars: list[Bar],
    expected_regime: str,
    expected_behavior: str,
    seed: int,
) -> ScenarioData:
    return ScenarioData(
        name=name,
        symbol=symbol,
        benchmark_symbol=benchmark_symbol,
        symbol_bars=symbol_bars,
        benchmark_bars=benchmark_bars,
        expected_regime=expected_regime,
        expected_strategy_behavior=expected_behavior,
        metadata={"seed": seed, "bars": len(symbol_bars), "benchmark_bars": len(benchmark_bars)},
    )


def generate_scenario(
    name: str,
    *,
    symbol: str = "SOXS.US",
    benchmark_symbol: str = "SMH.US",
    bars: int = 180,
    seed: int = 42,
) -> ScenarioData:
    scenario = str(name or "").strip().lower()
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)

    if scenario == "stable_range":
        symbol_bars = _symbol_range(start, bars, 10.0, 0.18, 0.0, 150_000, symbol, seed, "stable_range")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.0, seed + 1, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="RANGE", expected_behavior="layered_entries", seed=seed)

    if scenario == "narrow_range":
        symbol_bars = _symbol_range(start, bars, 10.0, 0.03, 0.0, 150_000, symbol, seed, "narrow_range")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.0, seed + 2, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="INVALID_RANGE", expected_behavior="no_trade", seed=seed)

    if scenario == "strong_uptrend":
        symbol_bars = _symbol_range(start, bars, 10.0, 0.12, 0.05, 180_000, symbol, seed, "strong_uptrend")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.15, seed + 3, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="STRONG_UPTREND", expected_behavior="buy_blocked", seed=seed)

    if scenario == "strong_downtrend":
        symbol_bars = _symbol_range(start, bars, 14.0, 0.15, -0.05, 180_000, symbol, seed, "strong_downtrend")
        benchmark = _benchmark_bars(start, bars, 100.0, -0.15, seed + 4, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="STRONG_DOWNTREND", expected_behavior="sell_only", seed=seed)

    if scenario == "high_volatility_range":
        symbol_bars = _symbol_range(start, bars, 11.0, 1.2, 0.0, 220_000, symbol, seed, "high_volatility_range")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.0, seed + 5, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="HIGH_VOLATILITY", expected_behavior="risk_off", seed=seed)

    if scenario == "gap_up":
        symbol_bars = []
        rng = random.Random(seed)
        for idx in range(bars):
            ts = start + timedelta(minutes=idx)
            close = 10.0 if idx == 0 else 13.0 + sin(idx / 6.0) * 0.15 + rng.uniform(-0.02, 0.02)
            symbol_bars.append(
                Bar(symbol=symbol, timestamp=ts, open=close, high=close * 1.01, low=close * 0.99, close=close, volume=160_000, bid=close * 0.999, ask=close * 1.001, source="gap_up")
            )
        benchmark = _benchmark_bars(start, bars, 100.0, 0.05, seed + 6, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="RANGE", expected_behavior="observe_after_gap_up", seed=seed)

    if scenario == "gap_down":
        symbol_bars = []
        rng = random.Random(seed)
        for idx in range(bars):
            ts = start + timedelta(minutes=idx)
            close = 10.0 if idx == 0 else 7.0 + sin(idx / 5.0) * 0.12 + rng.uniform(-0.02, 0.02)
            symbol_bars.append(
                Bar(symbol=symbol, timestamp=ts, open=close, high=close * 1.01, low=close * 0.99, close=close, volume=160_000, bid=close * 0.999, ask=close * 1.001, source="gap_down")
            )
        benchmark = _benchmark_bars(start, bars, 100.0, -0.05, seed + 7, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="RANGE", expected_behavior="observe_after_gap_down", seed=seed)

    if scenario == "wide_spread":
        symbol_bars = _symbol_range(start, bars, 10.0, 0.15, 0.0, 80_000, symbol, seed, "wide_spread")
        for idx, bar in enumerate(symbol_bars):
            bar.bid = round(bar.close * 0.97, 6)
            bar.ask = round(bar.close * 1.03, 6)
        benchmark = _benchmark_bars(start, bars, 100.0, 0.0, seed + 8, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="RANGE", expected_behavior="cost_block", seed=seed)

    if scenario == "low_liquidity":
        symbol_bars = _symbol_range(start, bars, 10.0, 0.15, 0.0, 1_000, symbol, seed, "low_liquidity")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.0, seed + 9, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="RANGE", expected_behavior="liquidity_block", seed=seed)

    if scenario == "benchmark_up_soxs_down":
        symbol_bars = _symbol_range(start, bars, 11.0, 0.2, -0.03, 180_000, symbol, seed, "benchmark_up_soxs_down")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.18, seed + 10, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="STRONG_DOWNTREND", expected_behavior="soxs_reduce_only", seed=seed)

    if scenario == "no_trade":
        symbol_bars = _symbol_range(start, bars, 10.0, 0.01, 0.0, 150_000, symbol, seed, "no_trade")
        benchmark = _benchmark_bars(start, bars, 100.0, 0.0, seed + 11, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="INVALID_RANGE", expected_behavior="no_trade", seed=seed)

    if scenario == "regime_transition":
        symbol_bars: list[Bar] = []
        rng = random.Random(seed)
        for idx in range(bars):
            ts = start + timedelta(minutes=idx)
            if idx < bars // 2:
                close = 10.0 + sin(idx / 4.0) * 0.15 + rng.uniform(-0.02, 0.02)
            else:
                close = 10.5 + (idx - bars // 2) * 0.06 + cos(idx / 5.0) * 0.08 + rng.uniform(-0.02, 0.02)
            symbol_bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=ts,
                    open=round(close * 0.999, 6),
                    high=round(close * 1.01, 6),
                    low=round(close * 0.99, 6),
                    close=round(close, 6),
                    volume=170_000,
                    bid=round(close * 0.999, 6),
                    ask=round(close * 1.001, 6),
                    source="regime_transition",
                )
            )
        benchmark = _benchmark_bars(start, bars, 100.0, 0.04, seed + 12, benchmark_symbol)
        return _scenario_payload(scenario, symbol, benchmark_symbol, symbol_bars=symbol_bars, benchmark_bars=benchmark, expected_regime="RANGE", expected_behavior="transition_sensitive", seed=seed)

    raise ValueError(f"Unsupported scenario: {name}")


def available_scenarios() -> list[str]:
    return [
        "stable_range",
        "narrow_range",
        "strong_uptrend",
        "strong_downtrend",
        "high_volatility_range",
        "gap_up",
        "gap_down",
        "wide_spread",
        "low_liquidity",
        "benchmark_up_soxs_down",
        "no_trade",
        "regime_transition",
    ]
