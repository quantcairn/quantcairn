"""
Tradable filter: runtime gate that rejects AI-selected candidates when
current market conditions make them unsuitable for entry.

Applied *after* AI selection produces a candidate list, *before* the
candidate is written into a TOP config and submitted to an engine.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---- Public API ------------------------------------------------------------


def filter_candidates(
    candidates: list[dict[str, Any]],
    market_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the subset of *candidates* that pass all tradable checks.

    Parameters
    ----------
    candidates :
        Each dict must have at least a ``ticker`` key.  Optional keys that
        may be populated by the AI selector pipeline: ``range_low``,
        ``range_high``, ``atr_pct``, ``current_price``, ``bid``, ``ask``,
        ``premarket_change_pct``, ``day_change_pct``, ``avg_volume``.
    market_data :
        Per-symbol market snapshot keyed by ticker.  If not provided the
        filter falls back to the per-candidate fields.

    Returns
    -------
    List of candidates that passed every applicable check.  Passing
    candidates have a ``tradable_filter_reason`` key set to ``"passed"``.
    Rejected candidates are omitted entirely (the caller may inspect the
    original *candidates* for audit purposes).
    """
    md = dict(market_data or {})
    result: list[dict[str, Any]] = []

    for item in candidates:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue

        ctx = dict(md.get(ticker, {}))
        ctx.setdefault("range_low", item.get("range_low"))
        ctx.setdefault("range_high", item.get("range_high"))
        ctx.setdefault("current_price", item.get("current_price"))
        ctx.setdefault("atr_pct", item.get("atr_pct"))
        ctx.setdefault("bid", item.get("bid"))
        ctx.setdefault("ask", item.get("ask"))
        ctx.setdefault("premarket_change_pct", item.get("premarket_change_pct"))
        ctx.setdefault("day_change_pct", item.get("day_change_pct"))
        ctx.setdefault("avg_volume", item.get("avg_volume"))
        ctx.setdefault("regime", item.get("regime", "NORMAL"))

        reason = _check(ticker, ctx)
        if reason is not None:
            logger.info("TradableFilter rejected %s: %s", ticker, reason)
            continue

        item["tradable_filter_reason"] = "passed"
        result.append(item)

    return result


# ---- Individual checks -----------------------------------------------------


def _check(ticker: str, ctx: dict[str, Any]) -> str | None:
    """Return a rejection reason string, or *None* if the candidate passes."""

    # 1. Regime check — EVENT regime blocks all new positions.
    regime = str(ctx.get("regime") or "NORMAL").strip().upper()
    if regime == "EVENT":
        return "regime:EVENT"

    # 2. Bid / ask spread.
    bid = _float(ctx, "bid")
    ask = _float(ctx, "ask")
    price = _float(ctx, "current_price")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        spread_pct = (ask - bid) / bid * 100.0
        if spread_pct > 0.5:
            return f"spread:{spread_pct:.2f}%"

    # 3. Premarket gap.
    premarket = _float(ctx, "premarket_change_pct")
    if premarket is not None and abs(premarket) > 5.0:
        return f"premarket_gap:{premarket:+.1f}%"

    # 4. Day change.
    day_chg = _float(ctx, "day_change_pct")
    if day_chg is not None and abs(day_chg) > 8.0:
        return f"day_change:{day_chg:+.1f}%"

    # 5. Volume.
    vol = _float(ctx, "avg_volume")
    if vol is not None and vol < 1_000_000:
        return f"volume:{vol:.0f}"

    # 6. ATR.
    atr = _float(ctx, "atr_pct")
    if atr is not None and atr > 12.0:
        return f"atr:{atr:.1f}%"

    # 7. Distance from support.
    low = _float(ctx, "range_low")
    if price is not None and low is not None and low > 0:
        pct_from_support = (price - low) / low * 100.0
        if pct_from_support > 15.0:
            return f"far_from_support:{pct_from_support:.1f}%"

    return None  # passed


def _float(ctx: dict[str, Any], key: str) -> float | None:
    """Safely extract an optional float from *ctx*."""
    val = ctx.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
