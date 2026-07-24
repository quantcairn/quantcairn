from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.shadow.universe import symbol_class_for


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "universe.yaml"

DEFAULT_UNIVERSE_RULES: dict[str, dict[str, float | None]] = {
    "common_stock": {
        "price_min": 5.0,
        "price_max": 200.0,
        "min_market_cap": 2_000_000_000.0,
        "min_average_dollar_volume": 50_000_000.0,
        "atr_20_pct_min": 1.0,
        "atr_20_pct_max": 8.0,
    },
    "etf": {
        "price_min": 5.0,
        "price_max": 300.0,
        "min_market_cap": None,
        "min_average_dollar_volume": 20_000_000.0,
        "atr_20_pct_min": 0.5,
        "atr_20_pct_max": 6.0,
    },
    "leveraged_etf": {
        "price_min": 5.0,
        "price_max": 100.0,
        "min_market_cap": None,
        "min_average_dollar_volume": 10_000_000.0,
        "atr_20_pct_min": 1.0,
        "atr_20_pct_max": 10.0,
    },
    "inverse_etf": {
        "price_min": 5.0,
        "price_max": 100.0,
        "min_market_cap": None,
        "min_average_dollar_volume": 10_000_000.0,
        "atr_20_pct_min": 1.0,
        "atr_20_pct_max": 10.0,
    },
}


@dataclass(frozen=True, slots=True)
class UniverseRule:
    asset_type: str
    price_min: float
    price_max: float
    min_average_dollar_volume: float
    atr_20_pct_min: float
    atr_20_pct_max: float
    min_market_cap: float | None = None


@dataclass(frozen=True, slots=True)
class UniverseEvaluation:
    symbol: str
    asset_type: str
    rejected: bool
    rejection_reason: tuple[str, ...]
    price: float | None
    market_cap: float | None
    average_dollar_volume_20d: float | None
    atr_20_percentage: float | None
    rule: UniverseRule

    def to_rejected_item(self, source: str = "") -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ticker": self.symbol,
            "asset_type": self.asset_type,
            "rejected": True,
            "rejection_reason": list(self.rejection_reason),
            "reason": ",".join(self.rejection_reason),
            "price": self.price,
            "market_cap": self.market_cap,
            "average_dollar_volume_20d": self.average_dollar_volume_20d,
            "atr_20_percentage": self.atr_20_percentage,
            "allowed_price_range": {
                "min": self.rule.price_min,
                "max": self.rule.price_max,
            },
            "source": source,
        }


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def normalize_asset_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"index_etf", "etf", "fund"}:
        return "etf"
    if text in {"leveraged_etf", "inverse_etf", "common_stock"}:
        return text
    return "common_stock"


def infer_asset_type(symbol: str, candidate: dict[str, Any] | None = None) -> str:
    candidate = candidate or {}
    for key in ("asset_type", "symbol_class", "security_type"):
        if candidate.get(key):
            return normalize_asset_type(candidate.get(key))
    return normalize_asset_type(symbol_class_for(f"{_normalize_symbol(symbol)}.US"))


def load_universe_rules(path: Path | str | None = None) -> dict[str, UniverseRule]:
    rules = {key: dict(value) for key, value in DEFAULT_UNIVERSE_RULES.items()}
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            raw_universe = raw.get("universe") if isinstance(raw, dict) else {}
            if isinstance(raw_universe, dict):
                for asset_type, values in raw_universe.items():
                    normalized = normalize_asset_type(asset_type)
                    if normalized not in rules or not isinstance(values, dict):
                        continue
                    rules[normalized].update(values)
        except Exception:
            # Fail-safe defaults keep filtering active if the optional config is unreadable.
            pass

    parsed: dict[str, UniverseRule] = {}
    for asset_type, values in rules.items():
        parsed[asset_type] = UniverseRule(
            asset_type=asset_type,
            price_min=float(values.get("price_min") or DEFAULT_UNIVERSE_RULES[asset_type]["price_min"]),
            price_max=float(values.get("price_max") or DEFAULT_UNIVERSE_RULES[asset_type]["price_max"]),
            min_average_dollar_volume=float(values.get("min_average_dollar_volume") or DEFAULT_UNIVERSE_RULES[asset_type]["min_average_dollar_volume"]),
            atr_20_pct_min=float(values.get("atr_20_pct_min") or DEFAULT_UNIVERSE_RULES[asset_type]["atr_20_pct_min"]),
            atr_20_pct_max=float(values.get("atr_20_pct_max") or DEFAULT_UNIVERSE_RULES[asset_type]["atr_20_pct_max"]),
            min_market_cap=_safe_float(values.get("min_market_cap")),
        )
    return parsed


def _first_float(candidate: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = candidate.get(key)
        if isinstance(value, dict):
            continue
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _candidate_metrics(candidate: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
    merged = dict(metrics)
    merged.update(candidate)
    price = _first_float(
        merged,
        (
            "current_price",
            "price",
            "last_close",
            "close",
            "price_midpoint",
            "price_midpoint_hint",
        ),
    )
    if price is None:
        low = _safe_float(merged.get("range_low"))
        high = _safe_float(merged.get("range_high"))
        if low is not None and high is not None and high > low > 0:
            price = (low + high) / 2.0
    market_cap = _first_float(merged, ("market_cap", "marketCapitalization", "market_value"))
    avg_dollar_volume = _first_float(
        merged,
        (
            "average_dollar_volume_20d",
            "avg_dollar_volume_20d",
            "dollar_volume_20d",
            "liquidity_score",
        ),
    )
    if avg_dollar_volume is None and price is not None:
        avg_volume = _first_float(merged, ("avg_volume_20d", "avg_20d_volume", "avg_volume", "avg_10d_volume", "avg_daily_volume_hint", "volume"))
        if avg_volume is not None:
            avg_dollar_volume = avg_volume * price
    atr_pct = _first_float(
        merged,
        (
            "atr_20_percentage",
            "atr20_percentage",
            "atr_pct",
            "atr_20_pct",
        ),
    )
    return price, market_cap, avg_dollar_volume, atr_pct


def evaluate_universe_candidate(
    candidate: dict[str, Any],
    *,
    rules: dict[str, UniverseRule] | None = None,
    skip_atr_validation: bool = False,
) -> UniverseEvaluation:
    symbol = _normalize_symbol(candidate.get("ticker") or candidate.get("symbol"))
    asset_type = infer_asset_type(symbol, candidate)
    active_rules = rules or load_universe_rules()
    rule = active_rules.get(asset_type) or active_rules["common_stock"]
    price, market_cap, avg_dollar_volume, atr_pct = _candidate_metrics(candidate)

    reasons: list[str] = []
    if price is None:
        reasons.append("price_missing")
    elif price < rule.price_min or price > rule.price_max:
        reasons.append("price_out_of_range")

    if avg_dollar_volume is None:
        reasons.append("dollar_volume_missing")
    elif avg_dollar_volume < rule.min_average_dollar_volume:
        reasons.append("low_dollar_volume")

    if rule.min_market_cap is not None:
        if market_cap is None:
            reasons.append("market_cap_missing")
        elif market_cap < rule.min_market_cap:
            reasons.append("market_cap_too_small")

    if not skip_atr_validation:
        if atr_pct is None:
            reasons.append("volatility_missing")
        elif atr_pct < rule.atr_20_pct_min:
            reasons.append("volatility_too_low")
        elif atr_pct > rule.atr_20_pct_max:
            reasons.append("volatility_too_high")

    return UniverseEvaluation(
        symbol=symbol,
        asset_type=asset_type,
        rejected=bool(reasons),
        rejection_reason=tuple(reasons),
        price=round(price, 4) if price is not None else None,
        market_cap=round(market_cap, 4) if market_cap is not None else None,
        average_dollar_volume_20d=round(avg_dollar_volume, 4) if avg_dollar_volume is not None else None,
        atr_20_percentage=round(atr_pct, 4) if atr_pct is not None else None,
        rule=rule,
    )


def filter_universe_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    rules: dict[str, UniverseRule] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    active_rules = rules or load_universe_rules()
    for raw in candidates or []:
        item = dict(raw)
        evaluation = evaluate_universe_candidate(item, rules=active_rules)
        if evaluation.rejected:
            rejected.append(evaluation.to_rejected_item(source=str(item.get("source") or "")))
            continue
        item["asset_type"] = evaluation.asset_type
        item["current_price"] = evaluation.price
        item["average_dollar_volume_20d"] = evaluation.average_dollar_volume_20d
        item["atr_20_percentage"] = evaluation.atr_20_percentage
        if evaluation.market_cap is not None:
            item["market_cap"] = evaluation.market_cap
        accepted.append(item)
    return accepted, rejected
