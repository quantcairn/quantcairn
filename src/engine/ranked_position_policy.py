"""Rank-based Paper allocation policy.

This module is pure calculation. It does not read runtime state, call brokers,
or submit orders.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from src.config.loader import PositionPolicyConfig
from src.risk.instrument_profile import is_leveraged_etf


LEVERAGED_INVERSE_TYPES = {"leveraged_etf", "inverse_etf", "leveraged_inverse_etf"}
ETF_TYPES = {"etf", "index_etf", "standard_etf"}


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return result


def _position_notional(position: Any) -> float:
    item = dict(position or {}) if isinstance(position, dict) else {}
    market_value = _float(item.get("market_value"), 0.0)
    if market_value > 0:
        return market_value
    quantity = _float(item.get("quantity"), 0.0)
    current_price = _float(item.get("current_price"), 0.0)
    avg_cost = _float(item.get("avg_cost", item.get("avg_entry_price")), 0.0)
    price = current_price if current_price > 0 else avg_cost
    return max(0.0, quantity) * max(0.0, price)


def _is_leveraged_inverse(candidate: dict[str, Any]) -> bool:
    asset_type = str(candidate.get("asset_type") or candidate.get("symbol_class") or "").strip().lower()
    if asset_type in LEVERAGED_INVERSE_TYPES:
        return True
    if bool(candidate.get("leveraged_etf")) or bool(candidate.get("inverse_etf")):
        return True
    return is_leveraged_etf(_symbol(candidate.get("symbol") or candidate.get("ticker")))


def _asset_type(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("asset_type") or candidate.get("symbol_class") or "").strip().lower()
    if _is_leveraged_inverse(candidate):
        return "leveraged_inverse_etf"
    if raw in ETF_TYPES:
        return "etf"
    return raw or "common_stock"


def _asset_cap(asset_type: str, policy: PositionPolicyConfig) -> tuple[float, str]:
    if asset_type == "leveraged_inverse_etf":
        return policy.max_leveraged_inverse_position_pct, "leveraged_inverse_position_limit"
    if asset_type == "etf":
        return policy.max_standard_etf_position_pct, "standard_etf_position_limit"
    return policy.max_common_stock_position_pct, "common_stock_position_limit"


def _current_positions_by_symbol(current_positions: Any) -> dict[str, dict[str, Any]]:
    if isinstance(current_positions, dict):
        items = current_positions.items()
    else:
        items = [
            (_symbol(getattr(pos, "ticker", "")), {
                "quantity": getattr(pos, "quantity", 0),
                "market_value": getattr(pos, "market_value", 0.0),
                "current_price": getattr(pos, "current_price", 0.0),
                "avg_entry_price": getattr(pos, "avg_entry_price", 0.0),
            })
            for pos in (current_positions or [])
        ]
    result: dict[str, dict[str, Any]] = {}
    for key, value in items:
        symbol = _symbol(key)
        if symbol:
            result[symbol] = dict(value or {}) if isinstance(value, dict) else {}
    return result


def _candidate_price(candidate: dict[str, Any]) -> float:
    for key in ("current_price", "price", "last_price", "close"):
        value = _float(candidate.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def _is_formal_ready(candidate: dict[str, Any]) -> bool:
    if candidate.get("scoring_eligible") is False:
        return False
    data_status = str(candidate.get("data_status") or "COMPLETE").strip().upper()
    if data_status not in {"COMPLETE", "DATA_VALID"}:
        return False
    if str(candidate.get("score_reason") or "").strip().lower() == "stub":
        return False
    if candidate.get("candidate_score") is None and candidate.get("final_score") is None and candidate.get("score") is None:
        return False
    return True


def position_policy_to_dict(policy: PositionPolicyConfig) -> dict[str, Any]:
    payload = asdict(policy)
    payload["rank_target_weights"] = {str(k): float(v) for k, v in policy.rank_target_weights.items()}
    return payload


def calculate_ranked_target_allocations(
    candidates: list[dict[str, Any]],
    account_equity: float,
    current_positions: Any,
    policy: PositionPolicyConfig | None = None,
    *,
    current_cash: float | None = None,
    selection_mode: str = "ACTIVE",
    result_quality: str = "COMPLETE",
    research_admission: str = "RESEARCH_READY",
) -> dict[str, Any]:
    policy = policy or PositionPolicyConfig()
    equity = _float(account_equity, 0.0)
    cash = _float(current_cash, equity) if current_cash is not None else equity
    positions = _current_positions_by_symbol(current_positions)
    current_gross_notional = sum(_position_notional(item) for item in positions.values())
    current_gross_weight = (current_gross_notional / equity) if equity > 0 else 0.0
    current_position_count = sum(1 for item in positions.values() if _position_notional(item) > 0)
    current_leveraged_count = sum(1 for symbol, item in positions.items() if _position_notional(item) > 0 and is_leveraged_etf(symbol))

    mode = str(selection_mode or "").strip().upper()
    quality = str(result_quality or "").strip().upper()
    admission = str(research_admission or "").strip().upper()
    preflight_reason = ""
    if mode != "ACTIVE":
        preflight_reason = "selection_not_active"
    elif quality != "COMPLETE":
        preflight_reason = "result_quality_not_complete"
    elif admission != "RESEARCH_READY":
        preflight_reason = "research_admission_not_ready"
    elif equity <= 0:
        preflight_reason = "invalid_account_equity"

    rows: list[dict[str, Any]] = []
    leveraged_selected = current_leveraged_count
    new_positions_selected = 0
    formal_candidates = [dict(item or {}) for item in (candidates or [])[: policy.requested_top_n]]

    for index, candidate in enumerate(formal_candidates, start=1):
        symbol = _symbol(candidate.get("symbol") or candidate.get("ticker"))
        asset_type = _asset_type(candidate)
        raw_weight = float(policy.rank_target_weights.get(index, 0.0) or 0.0)
        asset_cap, cap_reason = _asset_cap(asset_type, policy)
        capped_weight = min(raw_weight, asset_cap)
        current_notional = _position_notional(positions.get(symbol))
        status = "ELIGIBLE"
        reasons: list[str] = []

        if preflight_reason:
            status = "BLOCKED"
            reasons.append(preflight_reason)
            capped_weight = 0.0
        elif not symbol:
            status = "BLOCKED"
            reasons.append("symbol_not_in_formal_top")
            capped_weight = 0.0
        elif not _is_formal_ready(candidate):
            status = "BLOCKED"
            reasons.append("symbol_not_in_formal_top")
            capped_weight = 0.0
        elif index > policy.requested_top_n:
            status = "BLOCKED"
            reasons.append("top_n_limit")
            capped_weight = 0.0
        elif symbol in positions and current_notional > 0 and not policy.allow_position_increase:
            status = "BLOCKED"
            reasons.append("position_already_exists")
            capped_weight = 0.0
        elif current_position_count + new_positions_selected >= policy.max_open_positions:
            status = "BLOCKED"
            reasons.append("max_open_positions_reached")
            capped_weight = 0.0
        elif asset_type == "leveraged_inverse_etf":
            if leveraged_selected >= policy.max_leveraged_inverse_positions:
                status = "BLOCKED"
                reasons.append("leveraged_inverse_count_limit")
                capped_weight = 0.0
            else:
                leveraged_selected += 1

        if status == "ELIGIBLE" and capped_weight < raw_weight:
            reasons.append(cap_reason)

        price = _candidate_price(candidate)
        target_notional = equity * capped_weight if equity > 0 else 0.0
        available_increment = max(0.0, target_notional - current_notional)

        projected_gross = current_gross_notional + available_increment
        if status == "ELIGIBLE" and equity > 0 and projected_gross / equity > policy.max_gross_exposure_pct:
            remaining = max(0.0, equity * policy.max_gross_exposure_pct - current_gross_notional)
            if remaining <= 0:
                status = "BLOCKED"
                reasons.append("gross_exposure_limit")
                available_increment = 0.0
                target_notional = current_notional
                capped_weight = current_notional / equity if equity > 0 else 0.0
            else:
                available_increment = min(available_increment, remaining)
                target_notional = current_notional + available_increment
                capped_weight = target_notional / equity
                reasons.append("gross_exposure_limit")
        if status == "ELIGIBLE" and cash - available_increment < equity * policy.minimum_cash_reserve_pct:
            remaining = max(0.0, cash - equity * policy.minimum_cash_reserve_pct)
            if remaining <= 0:
                status = "BLOCKED"
                reasons.append("cash_reserve_limit")
                available_increment = 0.0
                target_notional = current_notional
                capped_weight = current_notional / equity if equity > 0 else 0.0
            else:
                available_increment = min(available_increment, remaining)
                target_notional = current_notional + available_increment
                capped_weight = target_notional / equity
                reasons.append("cash_reserve_limit")

        if status == "ELIGIBLE" and price <= 0:
            status = "BLOCKED"
            reasons.append("invalid_price")
            available_increment = 0.0

        target_shares = int(available_increment / price) if status == "ELIGIBLE" and price > 0 else 0
        if status == "ELIGIBLE" and target_shares <= 0:
            status = "BLOCKED"
            reasons.append("insufficient_buying_power")
            available_increment = 0.0
        if status == "ELIGIBLE":
            new_positions_selected += 1

        rows.append(
            {
                "symbol": symbol,
                "rank": index,
                "asset_type": asset_type,
                "raw_target_weight": round(raw_weight, 6),
                "capped_target_weight": round(max(0.0, capped_weight), 6),
                "target_notional": round(max(0.0, target_notional), 2),
                "current_notional": round(max(0.0, current_notional), 2),
                "available_increment_notional": round(max(0.0, available_increment), 2),
                "target_shares": max(0, target_shares),
                "allocation_status": status,
                "allocation_reason": reasons[0] if reasons else "rank_target_allocation",
                "allocation_reasons": reasons or ["rank_target_allocation"],
            }
        )

    eligible_increment = sum(float(row["available_increment_notional"]) for row in rows if row["allocation_status"] == "ELIGIBLE")
    gross_target_exposure = (current_gross_notional + eligible_increment) / equity if equity > 0 else 0.0
    return {
        "position_policy": position_policy_to_dict(policy),
        "target_allocations": rows,
        "gross_target_exposure": round(gross_target_exposure, 6),
        "cash_reserve_target": round(policy.minimum_cash_reserve_pct, 6),
        "allocation_warnings": sorted({
            reason
            for row in rows
            for reason in row.get("allocation_reasons", [])
            if reason != "rank_target_allocation"
        }),
        "current_gross_exposure": round(current_gross_weight, 6),
    }
