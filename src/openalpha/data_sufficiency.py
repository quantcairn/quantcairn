from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


UTC = timezone.utc


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _nested(candidate: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = candidate.get(key)
    return value if isinstance(value, dict) else {}


def _first_present(candidate: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in candidate and candidate.get(key) is not None:
            return candidate.get(key)
    return None


def _collect_fields(candidate: Mapping[str, Any]) -> list[str]:
    fields: list[str] = []
    for key in (
        "quote_timestamp",
        "quote_age_seconds",
        "current_price",
        "price",
        "last_price",
        "close",
        "premarket_last_price",
        "daily_data_status",
        "freshness_status",
        "benchmark_status",
        "benchmark_alignment_status",
        "average_dollar_volume_20d",
        "avg_10d_volume",
        "ma20",
        "ma50",
        "ma200",
        "atr_20_percentage",
        "risk_score",
        "trend_score",
        "volatility_score",
        "liquidity_score",
        "score_reason",
        "fallback_history_incomplete",
    ):
        if candidate.get(key) is not None:
            fields.append(key)
    trade_market_data = _nested(candidate, "trade_market_data")
    market_data = _nested(candidate, "market_data")
    for source in (trade_market_data, market_data, _nested(candidate, "metrics")):
        for key in source.keys():
            if key not in fields and source.get(key) is not None:
                fields.append(key)
    return fields


@dataclass(frozen=True, slots=True)
class DataSufficiencyResult:
    data_mode: str
    data_freshness: str
    data_status: str
    quote_status: str
    ohlcv_status: str
    history_status: str
    factor_status: str
    benchmark_status: str
    scoring_eligible: bool
    scoring_block_reason: str
    missing_fields: tuple[str, ...]
    quote_timestamp: str | None = None
    quote_age_seconds: float | None = None
    daily_data_status: str = ""
    freshness_status: str = ""
    selection_stage: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_mode": self.data_mode,
            "data_freshness": self.data_freshness,
            "data_status": self.data_status,
            "quote_status": self.quote_status,
            "ohlcv_status": self.ohlcv_status,
            "history_status": self.history_status,
            "factor_status": self.factor_status,
            "benchmark_status": self.benchmark_status,
            "scoring_eligible": self.scoring_eligible,
            "scoring_block_reason": self.scoring_block_reason,
            "missing_fields": list(self.missing_fields),
            "quote_timestamp": self.quote_timestamp,
            "quote_age_seconds": self.quote_age_seconds,
            "daily_data_status": self.daily_data_status,
            "freshness_status": self.freshness_status,
            "selection_stage": self.selection_stage,
            "notes": list(self.notes),
        }


def _max_quote_age_seconds(candidate: Mapping[str, Any]) -> int:
    asset_type = _normalize_text(candidate.get("asset_type")).lower()
    if asset_type in {"leveraged_etf", "inverse_etf"}:
        return 600
    if asset_type in {"index_etf"}:
        return 900
    return 900


def evaluate_data_sufficiency(
    candidate: Mapping[str, Any],
    *,
    strict_quote: bool = True,
    strict_history: bool = True,
    strict_benchmark: bool = True,
    min_history_rows: int = 20,
) -> DataSufficiencyResult:
    payload = dict(candidate or {})
    trade_market_data = _nested(payload, "trade_market_data")
    market_data = _nested(payload, "market_data")
    metrics = _nested(payload, "metrics")

    def present(*keys: str) -> Any:
        value = _first_present(payload, *keys)
        if value is not None:
            return value
        value = _first_present(trade_market_data, *keys)
        if value is not None:
            return value
        value = _first_present(market_data, *keys)
        if value is not None:
            return value
        return _first_present(metrics, *keys)

    data_mode = _normalize_text(
        present("data_mode", "data_source", "market_data_mode", "mode")
    ).lower() or "unknown"
    freshness_status = _normalize_text(present("freshness_status")).upper()
    daily_data_status = _normalize_text(present("daily_data_status")).upper()
    selection_stage = _normalize_text(present("selection_stage")).upper()
    benchmark_status = _normalize_text(
        present("benchmark_status", "benchmark_alignment_status")
    ).upper() or "UNKNOWN"

    quote_timestamp = present("quote_timestamp")
    quote_age_seconds = _safe_float(present("quote_age_seconds"))
    current_price = _safe_float(present("current_price", "price", "last_price", "close", "premarket_last_price", "last_close"))
    quote_status = "MISSING"
    data_freshness = "unknown"
    data_status = "MISSING"
    notes: list[str] = []
    missing_fields: list[str] = []

    if quote_timestamp:
        quote_timestamp = str(quote_timestamp)
        quote_status = "OK"
        data_freshness = "fresh"
        if quote_age_seconds is not None:
            if quote_age_seconds < 0:
                quote_status = "INVALID"
                data_freshness = "invalid"
                data_status = "INVALID"
                missing_fields.append("quote_timestamp")
            elif quote_age_seconds > _max_quote_age_seconds(payload):
                quote_status = "STALE"
                data_freshness = "stale"
                data_status = "STALE"
                notes.append("quote_too_old")
    elif current_price is not None:
        quote_status = "OK" if not strict_quote else "MISSING"
        if not strict_quote:
            data_freshness = "fresh"
    elif strict_quote:
        missing_fields.append("quote")

    if freshness_status == "INVALID" or daily_data_status in {"FUTURE", "MISSING"} or selection_stage == "INVALID":
        data_status = "INVALID"
        data_freshness = "invalid"
    elif freshness_status == "STALE" or daily_data_status == "STALE" or selection_stage == "STALE":
        data_status = "STALE" if data_status != "INVALID" else data_status
        if data_freshness != "invalid":
            data_freshness = "stale"
    elif not data_status or data_status == "MISSING":
        data_status = "VALID"
        if not data_freshness or data_freshness == "unknown":
            data_freshness = "fresh"

    close_history = payload.get("close_history")
    if not isinstance(close_history, list):
        close_history = list((payload.get("series") or {}).get("returns") or [])
    history_rows = _safe_int(payload.get("history_rows"))
    if history_rows is None:
        history_rows = len(close_history) if isinstance(close_history, list) else 0
    fallback_history_incomplete = bool(payload.get("fallback_history_incomplete"))
    explicit_ohlcv_fetch_status = _normalize_text(present("ohlcv_fetch_status")).upper()
    history_missing_windows = list(payload.get("history_missing_windows") or [])
    if (
        fallback_history_incomplete
        and explicit_ohlcv_fetch_status == "COMPLETE"
        and history_rows >= min_history_rows
        and not history_missing_windows
    ):
        fallback_history_incomplete = False

    ohlcv_status = "OK"
    history_status = "OK"
    if history_rows < min_history_rows:
        history_status = "MISSING" if strict_history else "DEGRADED"
        if strict_history:
            missing_fields.append("history")
    if fallback_history_incomplete:
        history_status = "INVALID"
        notes.append("fallback_history_incomplete")

    has_ohlcv = any(
        present(*keys) is not None
        for keys in (
            ("open", "Open"),
            ("high", "High"),
            ("low", "Low"),
            ("close", "Close"),
            ("volume", "Volume"),
            ("ohlcv",),
            ("last_close",),
        )
    )
    if not has_ohlcv and not close_history:
        ohlcv_status = "MISSING"
        if strict_history:
            missing_fields.append("ohlcv")

    factor_fields = [
        "average_dollar_volume_20d",
        "avg_10d_volume",
        "ma20",
        "ma50",
        "ma200",
        "atr_20_percentage",
        "liquidity_score",
        "trend_score",
        "volatility_score",
        "risk_score",
        "strategy_fit_score",
    ]
    factor_present = any(present(field) is not None for field in factor_fields)
    factor_status = "OK" if factor_present else "MISSING"
    if not factor_present:
        missing_fields.append("factors")

    if strict_benchmark and benchmark_status and benchmark_status != "VALID":
        data_status = "INVALID"
        notes.append("benchmark_invalid")
        missing_fields.append("benchmark")

    if strict_quote and quote_status == "MISSING":
        data_status = "INVALID"
        notes.append("quote_missing")

    if strict_history and history_status in {"MISSING", "INVALID"}:
        data_status = "INVALID"
        notes.append("history_missing")

    if factor_status == "MISSING":
        data_status = "INVALID"
        notes.append("missing_factors")

    if data_status == "MISSING":
        data_status = "INVALID" if missing_fields else "VALID"

    if data_status == "STALE" and quote_status == "OK":
        quote_status = "STALE"
    if data_status == "INVALID" and quote_status == "OK" and strict_quote and current_price is None:
        quote_status = "INVALID"

    scoring_eligible = data_status == "VALID" and quote_status == "OK" and history_status == "OK" and factor_status == "OK"
    block_reasons: list[str] = []
    if quote_status == "MISSING":
        block_reasons.append("missing_quote")
    elif quote_status in {"STALE", "INVALID"}:
        block_reasons.append(f"quote_{quote_status.lower()}")
    if history_status in {"MISSING", "INVALID"}:
        block_reasons.append("missing_history")
    elif history_status == "DEGRADED":
        block_reasons.append("history_degraded")
    if ohlcv_status != "OK":
        block_reasons.append("missing_ohlcv")
    if factor_status == "MISSING":
        block_reasons.append("missing_factors")
    if strict_benchmark and benchmark_status != "VALID":
        block_reasons.append("benchmark_invalid")
    if data_status == "STALE":
        block_reasons.append("stale_data")
    if data_status == "INVALID" and not block_reasons:
        block_reasons.append("data_invalid")

    if not scoring_eligible and not block_reasons:
        block_reasons.append("data_sufficiency_failed")

    return DataSufficiencyResult(
        data_mode=data_mode,
        data_freshness=data_freshness,
        data_status=data_status,
        quote_status=quote_status,
        ohlcv_status=ohlcv_status,
        history_status=history_status,
        factor_status=factor_status,
        benchmark_status=benchmark_status,
        scoring_eligible=scoring_eligible,
        scoring_block_reason="; ".join(dict.fromkeys(block_reasons)),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        quote_timestamp=str(quote_timestamp) if quote_timestamp is not None else None,
        quote_age_seconds=quote_age_seconds,
        daily_data_status=daily_data_status,
        freshness_status=freshness_status,
        selection_stage=selection_stage,
        notes=tuple(dict.fromkeys(notes)),
    )
