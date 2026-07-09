from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper()


class TradeEligibilityFilter:
    def filter(self, candidates: list, market_data: dict) -> dict:
        market_data = dict(market_data or {})
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        fallback_used = False

        for raw in candidates or []:
            item = dict(raw)
            ticker = _normalize_ticker(item.get("ticker"))
            if not ticker:
                continue
            ctx = self._context_for(ticker, item, market_data)
            reason = self._reject_reason(ctx)
            if reason:
                rejected.append({"ticker": ticker, "reason": reason})
                item["trade_filter_passed"] = False
                item["reject_reason"] = reason
                item["fallback_used"] = False
            else:
                item["trade_filter_passed"] = True
                item["reject_reason"] = ""
                item["fallback_used"] = False
                accepted.append(item)

        if len(accepted) < 3:
            accepted_tickers = {_normalize_ticker(item.get("ticker")) for item in accepted}
            fallback_pool = sorted(
                [dict(item) for item in candidates or [] if _normalize_ticker(item.get("ticker")) not in accepted_tickers],
                key=lambda row: (
                    -float(row.get("final_score") or row.get("score") or 0.0),
                    _normalize_ticker(row.get("ticker")),
                ),
            )
            for item in fallback_pool:
                if len(accepted) >= 3:
                    break
                ticker = _normalize_ticker(item.get("ticker"))
                ctx = self._context_for(ticker, item, market_data)
                item["trade_filter_passed"] = False
                item["reject_reason"] = self._reject_reason(ctx) or "fallback_pool"
                item["fallback_used"] = True
                accepted.append(item)
                fallback_used = True

        accepted.sort(
            key=lambda row: (
                -float(row.get("final_score") or row.get("score") or 0.0),
                _normalize_ticker(row.get("ticker")),
            )
        )
        return {
            "accepted": accepted,
            "rejected": rejected,
            "fallback_used": fallback_used,
        }

    def _context_for(self, ticker: str, candidate: dict[str, Any], market_data: dict[str, Any]) -> dict[str, Any]:
        ctx = {}
        raw_md = market_data.get(ticker, {})
        if isinstance(raw_md, dict):
            ctx.update(raw_md)
        elif raw_md is not None:
            ctx["value"] = raw_md
        for key in (
            "earnings_within_days",
            "price_change_5d",
            "avg_volume",
            "bid_ask_spread_pct",
            "regime",
            "data_age_seconds",
        ):
            if key not in ctx or ctx.get(key) is None:
                ctx[key] = candidate.get(key)
        if ctx.get("avg_volume") is None:
            ctx["avg_volume"] = candidate.get("avg_10d_volume") or candidate.get("volume")
        if ctx.get("bid_ask_spread_pct") is None:
            ctx["bid_ask_spread_pct"] = candidate.get("spread_pct") or candidate.get("spread_pct_live")
        if ctx.get("price_change_5d") is None:
            ctx["price_change_5d"] = candidate.get("price_change_5d") or candidate.get("day_change_pct")
        if ctx.get("regime") is None:
            ctx["regime"] = candidate.get("regime") or "NORMAL"
        if ctx.get("data_age_seconds") is None:
            ctx["data_age_seconds"] = candidate.get("data_age_seconds") or 0
        if ctx.get("earnings_within_days") is None:
            ctx["earnings_within_days"] = candidate.get("earnings_within_days")
        return ctx

    def _reject_reason(self, ctx: dict[str, Any]) -> str | None:
        earnings_days = _safe_float(ctx.get("earnings_within_days"))
        if earnings_days is not None and earnings_days <= 3:
            return "earnings_too_close"

        price_change_5d = _safe_float(ctx.get("price_change_5d"))
        if price_change_5d is not None and abs(price_change_5d) > 15:
            return "extreme_5d_move"

        avg_volume = _safe_float(ctx.get("avg_volume"))
        if avg_volume is not None and avg_volume < 5_000_000:
            return "low_volume"

        spread_pct = _safe_float(ctx.get("bid_ask_spread_pct"))
        if spread_pct is not None and spread_pct > 0.2:
            return "spread_too_wide"

        regime = str(ctx.get("regime") or "NORMAL").strip().upper()
        if regime == "EVENT":
            return "event_regime"

        data_age_seconds = _safe_float(ctx.get("data_age_seconds"))
        if data_age_seconds is not None and data_age_seconds > 120:
            return "stale_data"

        return None
