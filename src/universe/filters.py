"""Managed Universe Filters — pass/fail with reason codes.

Liquidity, price, risk, and composition filters. Each filter returns
(dropped, reason_codes) for auditability.  Composition filter enforces
the leveraged/inverse limit.  Never touches trading state.
"""

from __future__ import annotations

from typing import Any

from src.universe.models import UniverseSymbol, UniverseSnapshot


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def apply_liquidity_filter(
    symbols: list[UniverseSymbol],
    *,
    min_avg_volume: int = 500_000,
) -> tuple[list[UniverseSymbol], list[dict[str, str]]]:
    """Drop symbols with insufficient average volume."""
    kept: list[UniverseSymbol] = []
    dropped: list[dict[str, str]] = []
    for s in symbols:
        if not s.enabled:
            dropped.append({"symbol": s.symbol, "reason": "disabled_by_config"})
            continue
        if s.min_avg_volume < min_avg_volume or s.liquidity_score < 20.0:
            s.disable(f"liquidity_filter:volume={s.min_avg_volume},score={s.liquidity_score}")
            dropped.append({"symbol": s.symbol, "reason": "liquidity_insufficient"})
        else:
            kept.append(s)
    return kept, dropped


def apply_price_filter(
    symbols: list[UniverseSymbol],
    *,
    min_price: float = 4.0,
    max_price: float = 300.0,
) -> tuple[list[UniverseSymbol], list[dict[str, str]]]:
    """Drop symbols outside the price band."""
    kept: list[UniverseSymbol] = []
    dropped: list[dict[str, str]] = []
    for s in symbols:
        if not s.enabled:
            continue
        if s.min_price < min_price:
            s.disable(f"price_too_low:{s.min_price}<{min_price}")
            dropped.append({"symbol": s.symbol, "reason": "price_too_low"})
        elif s.min_price > max_price:
            s.disable(f"price_too_high:{s.min_price}>{max_price}")
            dropped.append({"symbol": s.symbol, "reason": "price_too_high"})
        else:
            kept.append(s)
    return kept, dropped


def apply_risk_filter(
    symbols: list[UniverseSymbol],
    *,
    max_risk_score: float = 70.0,
    max_volatility_score: float = 80.0,
) -> tuple[list[UniverseSymbol], list[dict[str, str]]]:
    """Drop symbols that exceed risk or volatility thresholds."""
    kept: list[UniverseSymbol] = []
    dropped: list[dict[str, str]] = []
    for s in symbols:
        if not s.enabled:
            continue
        reasons: list[str] = []
        if s.risk_score > max_risk_score:
            reasons.append(f"risk_score:{s.risk_score}>{max_risk_score}")
        if s.volatility_score > max_volatility_score:
            reasons.append(f"volatility_score:{s.volatility_score}>{max_volatility_score}")
        if reasons:
            s.disable("; ".join(reasons))
            dropped.append({"symbol": s.symbol, "reason": "; ".join(reasons)})
        else:
            kept.append(s)
    return kept, dropped


def apply_composition_filter(
    symbols: list[UniverseSymbol],
    *,
    max_leveraged_inverse: int = 1,
) -> tuple[list[UniverseSymbol], list[dict[str, str]]]:
    """Limit the number of leveraged/inverse ETF symbols in the universe.

    The first *max_leveraged_inverse* leveraged/inverse symbols (sorted by
    liquidity descending) are kept; remaining ones are disabled.

    Must be applied LAST — after liquidity/price/risk filters have narrowed
    the field.
    """
    kept: list[UniverseSymbol] = []
    dropped: list[dict[str, str]] = []
    leveraged: list[UniverseSymbol] = []
    non_leveraged: list[UniverseSymbol] = []

    for s in symbols:
        if not s.enabled:
            continue
        if s.is_leveraged or s.is_inverse:
            leveraged.append(s)
        else:
            non_leveraged.append(s)

    # Sort leveraged by liquidity descending — best first
    leveraged.sort(key=lambda s: -s.liquidity_score)

    for i, s in enumerate(leveraged):
        if i < max_leveraged_inverse:
            kept.append(s)
        else:
            s.disable(f"composition_limit:max_leveraged_inverse={max_leveraged_inverse}")
            dropped.append({"symbol": s.symbol, "reason": "leveraged_inverse_limit_exceeded"})

    kept.extend(non_leveraged)
    return kept, dropped


def run_all_filters(
    symbols: list[UniverseSymbol],
    *,
    min_avg_volume: int = 500_000,
    min_price: float = 4.0,
    max_price: float = 300.0,
    max_risk_score: float = 70.0,
    max_volatility_score: float = 80.0,
    max_leveraged_inverse: int = 1,
) -> UniverseSnapshot:
    """Apply all filters in sequence and return a populated snapshot.

    Filter order: Liquidity → Price → Risk → Composition (leveraged limit)
    """
    results: dict[str, Any] = {}
    snapshot = UniverseSnapshot(max_leveraged_inverse=max_leveraged_inverse)

    result = list(symbols)

    # 1. Liquidity
    result, dropped = apply_liquidity_filter(result, min_avg_volume=min_avg_volume)
    results["liquidity_dropped"] = len(dropped)
    results["liquidity_reasons"] = [d["reason"] for d in dropped]

    # 2. Price
    result, dropped = apply_price_filter(result, min_price=min_price, max_price=max_price)
    results["price_dropped"] = len(dropped)
    results["price_reasons"] = [d["reason"] for d in dropped]

    # 3. Risk
    result, dropped = apply_risk_filter(result, max_risk_score=max_risk_score, max_volatility_score=max_volatility_score)
    results["risk_dropped"] = len(dropped)
    results["risk_reasons"] = [d["reason"] for d in dropped]

    # 4. Composition
    result, dropped = apply_composition_filter(result, max_leveraged_inverse=max_leveraged_inverse)
    results["composition_dropped"] = len(dropped)
    results["composition_reasons"] = [d["reason"] for d in dropped]

    snapshot.symbols = list(symbols)  # All symbols (enabled + disabled)
    snapshot.total_symbols = len(symbols)
    snapshot.enabled_symbols = sum(1 for s in symbols if s.enabled)
    snapshot.filter_results = results
    snapshot.compute_composition()

    return snapshot
