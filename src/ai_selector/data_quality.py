from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from src.ai_selector.data_sufficiency import evaluate_data_sufficiency
from src.ai_selector.universe_filter import evaluate_universe_candidate, infer_asset_type
from src.ai_selector.selection_report import normalize_provider_audit


UTC = timezone.utc

_META_KEYS = {
    "ticker",
    "symbol",
    "source",
    "reason",
    "error_message",
    "error_code",
    "fallback",
    "mock",
    "mock_used",
    "fallback_used",
    "timed_out",
    "fallback_scope",
    "fallback_severity",
    "provider_name",
    "attempted",
    "success",
    "failure",
    "contributors",
    "contributed_fields",
    "affected_candidates",
    "affected_fields",
    "has_real_success",
    "has_timeout",
    "has_fallback",
    "has_mock",
}

_CRITICAL_MARKET_DATA_FIELDS = {
    "current_price",
    "open",
    "high",
    "low",
    "close",
    "ohlcv",
    "quote",
    "bid",
    "ask",
    "spread_pct",
    "current_session",
    "previous_completed_session",
    "daily_data_as_of",
    "benchmark_data_as_of",
    "benchmark_alignment_status",
    "market_cap",
    "average_dollar_volume_20d",
    "avg_dollar_volume_20d",
    "avg_10d_volume",
    "volume",
    "atr_20_percentage",
    "atr_pct",
    "gap_pct",
}

_NON_CRITICAL_FACTOR_FIELDS = {
    "score",
    "ai_score",
    "range_score",
    "final_score",
    "candidate_score",
    "confidence",
    "technical_score",
    "sentiment_score",
    "fundamental_score",
    "valuation_score",
    "earnings_score",
    "growth_score",
    "risk_score",
    "liquidity_score",
    "trend_score",
    "volatility_score",
    "strategy_fit_score",
}

_EXPLANATION_ONLY_FIELDS = {
    "reason",
    "summary",
    "commentary",
    "explanation",
    "narrative",
    "analysis",
    "notes",
    "score_reason",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_ticker(value: Any) -> str:
    return _normalize_text(value).upper().split(".")[0]


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = _normalize_text(value)
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if _normalize_text(item)))


def _field_present(candidate: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in candidate and candidate.get(key) is not None:
            return candidate.get(key)
    return None


def _list_from_mapping(candidate: Mapping[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = candidate.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _candidate_field_value(candidate: Mapping[str, Any], *keys: str) -> Any:
    value = _field_present(candidate, *keys)
    if value is not None:
        return value
    for nested_key in ("market_data", "trade_market_data", "metrics"):
        nested = candidate.get(nested_key)
        if not isinstance(nested, dict):
            continue
        value = _field_present(nested, *keys)
        if value is not None:
            return value
    return None


def _provider_rows_for_candidate(
    provider_audit_summary: Mapping[str, Any] | None,
    provider_outputs: Mapping[str, Any] | None,
    ticker: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    summary = provider_audit_summary if isinstance(provider_audit_summary, Mapping) else {}
    outputs = provider_outputs if isinstance(provider_outputs, Mapping) else {}
    provider_details: dict[str, dict[str, Any]] = {}
    provider_rows: dict[str, dict[str, Any]] = {}

    details = summary.get("details") if isinstance(summary.get("details"), list) else []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        provider_name = _normalize_text(detail.get("provider_name"))
        if provider_name:
            provider_details[provider_name] = dict(detail)

    for provider_name, payload in outputs.items():
        provider_key = _normalize_text(provider_name)
        if not provider_key:
            continue
        if isinstance(payload, dict):
            row = payload.get(ticker) or payload.get(_normalize_ticker(ticker))
            if isinstance(row, dict):
                provider_rows[provider_key] = dict(row)
        elif isinstance(payload, list):
            for row in payload:
                if not isinstance(row, dict):
                    continue
                row_ticker = _normalize_ticker(row.get("ticker") or row.get("symbol"))
                if row_ticker == ticker:
                    provider_rows[provider_key] = dict(row)
                    break

    return provider_details, provider_rows


def _classify_provider_fields(fields: set[str]) -> tuple[str, str]:
    if fields & _CRITICAL_MARKET_DATA_FIELDS:
        return "CRITICAL_MARKET_DATA", "CRITICAL"
    if fields & _NON_CRITICAL_FACTOR_FIELDS:
        return "NON_CRITICAL_FACTOR", "DEGRADED"
    if fields & _EXPLANATION_ONLY_FIELDS:
        return "EXPLANATION_ONLY", "INFO"
    return "", ""


@dataclass(frozen=True, slots=True)
class DataQualityResult:
    data_status: str
    scoring_eligible: bool
    blocking_reasons: tuple[str, ...]
    missing_fields: tuple[str, ...]
    stale_fields: tuple[str, ...]
    critical_fields: tuple[str, ...]
    data_as_of: str | None = None
    quote_as_of: str | None = None
    ohlcv_as_of: str | None = None
    benchmark_as_of: str | None = None
    provider_chain: tuple[str, ...] = ()
    critical_data_sources: tuple[str, ...] = ()
    noncritical_data_sources: tuple[str, ...] = ()
    fallback_scope: str = ""
    fallback_severity: str = ""
    quality_warnings: tuple[str, ...] = ()
    explanation_degraded: bool = False
    critical_data_degraded: bool = False
    noncritical_data_degraded: bool = False
    candidate_fallback: bool = False
    mock_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_status": self.data_status,
            "scoring_eligible": self.scoring_eligible,
            "blocking_reasons": list(self.blocking_reasons),
            "missing_fields": list(self.missing_fields),
            "stale_fields": list(self.stale_fields),
            "critical_fields": list(self.critical_fields),
            "data_as_of": self.data_as_of,
            "quote_as_of": self.quote_as_of,
            "ohlcv_as_of": self.ohlcv_as_of,
            "benchmark_as_of": self.benchmark_as_of,
            "provider_chain": list(self.provider_chain),
            "critical_data_sources": list(self.critical_data_sources),
            "noncritical_data_sources": list(self.noncritical_data_sources),
            "fallback_scope": self.fallback_scope,
            "fallback_severity": self.fallback_severity,
            "quality_warnings": list(self.quality_warnings),
            "explanation_degraded": self.explanation_degraded,
            "critical_data_degraded": self.critical_data_degraded,
            "noncritical_data_degraded": self.noncritical_data_degraded,
            "candidate_fallback": self.candidate_fallback,
            "mock_used": self.mock_used,
        }


def evaluate_candidate_data_quality(
    candidate: Mapping[str, Any],
    *,
    provider_audit: Mapping[str, Any] | None = None,
    provider_outputs: Mapping[str, Any] | None = None,
) -> DataQualityResult:
    payload = dict(candidate or {})
    asset_type = infer_asset_type(str(payload.get("ticker") or payload.get("symbol") or ""), payload)
    sufficiency = evaluate_data_sufficiency(payload, strict_quote=True, strict_history=True, strict_benchmark=True)
    universe_eval = evaluate_universe_candidate(payload)

    provider_summary = normalize_provider_audit(
        dict(provider_audit or {}),
        dict(provider_outputs or {}),
    )
    ticker = _normalize_ticker(payload.get("ticker") or payload.get("symbol"))
    provider_details, provider_rows = _provider_rows_for_candidate(provider_summary, provider_outputs, ticker)
    provider_chain = _unique(
        [
            *list(provider_summary.get("attempted") or []),
            *list(provider_summary.get("successful") or []),
            *list(provider_summary.get("fallback") or []),
            *list(provider_summary.get("mock") or []),
            *list(provider_summary.get("contributors") or []),
        ]
    )

    critical_fields = [
        "quote_timestamp",
        "quote_age_seconds",
        "current_price",
        "average_dollar_volume_20d",
        "atr_20_percentage",
        "ma20",
        "ma50",
        "benchmark_status",
        "benchmark_alignment_status",
    ]
    if asset_type == "common_stock":
        critical_fields.append("market_cap")

    quote_as_of = _parse_timestamp(_candidate_field_value(payload, "quote_timestamp", "premarket_snapshot_at"))
    ohlcv_as_of = _parse_timestamp(_candidate_field_value(payload, "daily_data_as_of", "ohlcv_as_of"))
    benchmark_as_of = _parse_timestamp(_candidate_field_value(payload, "benchmark_data_as_of"))
    data_as_of = max([ts for ts in (quote_as_of, ohlcv_as_of, benchmark_as_of) if ts is not None], default=None)

    missing_fields: list[str] = []
    stale_fields: list[str] = []
    blocking_reasons: list[str] = []
    quality_warnings: list[str] = []

    for field in critical_fields:
        value = _candidate_field_value(payload, field)
        if value is None:
            missing_fields.append(field)

    quote_age_seconds = _safe_float(_candidate_field_value(payload, "quote_age_seconds"))
    freshness_status = _normalize_text(_candidate_field_value(payload, "freshness_status")).upper()
    daily_data_status = _normalize_text(_candidate_field_value(payload, "daily_data_status")).upper()
    benchmark_status = _normalize_text(_candidate_field_value(payload, "benchmark_status", "benchmark_alignment_status")).upper()
    if not sufficiency.scoring_eligible:
        blocking_reasons.append(_normalize_text(sufficiency.scoring_block_reason) or "data_sufficiency_blocked")
    for field in sufficiency.missing_fields:
        if field and field not in missing_fields:
            missing_fields.append(field)
    history_missing_windows = [
        _normalize_text(item)
        for item in list(payload.get("history_missing_windows") or [])
        if _normalize_text(item)
    ]
    if history_missing_windows:
        if "history" not in missing_fields:
            missing_fields.append("history")
        blocking_reasons.append("missing_history")
        blocking_reasons.extend(f"history_window_missing:{window}" for window in history_missing_windows)
    if sufficiency.data_status == "STALE" and "quote_timestamp" not in stale_fields and quote_age_seconds is not None:
        stale_fields.append("quote_timestamp")
    if quote_age_seconds is not None:
        max_quote_age = 600 if asset_type in {"leveraged_etf", "inverse_etf"} else 900
        if quote_age_seconds < 0:
            stale_fields.append("quote_timestamp")
            blocking_reasons.append("quote_timestamp_in_future")
        elif quote_age_seconds > max_quote_age:
            stale_fields.append("quote_timestamp")
            blocking_reasons.append("quote_expired")
    if freshness_status == "INVALID" or daily_data_status == "FUTURE":
        blocking_reasons.append("daily_data_invalid")
    elif freshness_status == "STALE" or daily_data_status == "STALE":
        stale_fields.append("daily_data_as_of")
    if benchmark_status not in {"VALID", "LATEST_COMPLETED_SESSION", "OK"}:
        missing_fields.append("benchmark")
        blocking_reasons.append("benchmark_invalid")

    if universe_eval.rejected:
        reasons = set(universe_eval.rejection_reason)
        if reasons & {"price_missing", "price_out_of_range"}:
            blocking_reasons.append("price_invalid")
        if reasons & {"low_dollar_volume"}:
            blocking_reasons.append("dollar_volume_too_low")
        if reasons & {"market_cap_missing", "market_cap_too_small"}:
            blocking_reasons.append("market_cap_invalid")
        if reasons & {"volatility_missing", "volatility_too_low", "volatility_too_high"}:
            blocking_reasons.append("volatility_invalid")

    critical_source_set: set[str] = set()
    noncritical_source_set: set[str] = set()
    candidate_fallback = bool(
        payload.get("fallback_used")
        or payload.get("quality_backfill")
        or payload.get("fallback_history_incomplete")
    )
    mock_used = bool(payload.get("mock_used"))
    aggregate_scope = ""
    aggregate_severity = ""

    for provider_name, detail in provider_details.items():
        affected_candidates = {str(item or "").strip().upper() for item in list(detail.get("affected_candidates") or [])}
        if ticker and affected_candidates and ticker not in affected_candidates:
            continue
        fields = {str(item or "").strip() for item in list(detail.get("affected_fields") or []) if str(item or "").strip()}
        scope = str(detail.get("fallback_scope") or "").strip().upper()
        severity = str(detail.get("fallback_severity") or "").strip().upper()
        has_timeout = bool(detail.get("has_timeout"))
        has_fallback = bool(detail.get("has_fallback"))
        has_mock = bool(detail.get("has_mock"))
        impact_scope, impact_severity = _classify_provider_fields(fields)

        if scope == "CRITICAL_MARKET_DATA" or impact_scope == "CRITICAL_MARKET_DATA":
            critical_source_set.add(provider_name)
        elif scope in {"NON_CRITICAL_FACTOR", "EXPLANATION_ONLY", "RUN_LEVEL"} or impact_scope != "CRITICAL_MARKET_DATA":
            noncritical_source_set.add(provider_name)

        if has_fallback or has_mock:
            candidate_fallback = True
        if has_mock:
            mock_used = True
        if not aggregate_scope:
            aggregate_scope = scope or impact_scope
            aggregate_severity = severity or impact_severity
        elif aggregate_scope != "CRITICAL_MARKET_DATA" and (scope == "CRITICAL_MARKET_DATA" or impact_scope == "CRITICAL_MARKET_DATA"):
            aggregate_scope = "CRITICAL_MARKET_DATA"
            aggregate_severity = "CRITICAL"
        elif aggregate_scope == "EXPLANATION_ONLY" and scope == "NON_CRITICAL_FACTOR":
            aggregate_scope = "NON_CRITICAL_FACTOR"
            aggregate_severity = "DEGRADED"

    for provider_name, row in provider_rows.items():
        fields = {
            str(key)
            for key, value in row.items()
            if key not in _META_KEYS and value is not None
        }
        impact_scope, impact_severity = _classify_provider_fields(fields)
        if impact_scope == "CRITICAL_MARKET_DATA":
            critical_source_set.add(provider_name)
        else:
            noncritical_source_set.add(provider_name)
        if row.get("fallback") or row.get("fallback_used") or row.get("mock") or row.get("mock_used"):
            candidate_fallback = True
            if row.get("mock") or row.get("mock_used"):
                mock_used = True
        if not aggregate_scope:
            aggregate_scope = impact_scope
            aggregate_severity = impact_severity
        elif aggregate_scope != "CRITICAL_MARKET_DATA" and impact_scope == "CRITICAL_MARKET_DATA":
            aggregate_scope = impact_scope
            aggregate_severity = impact_severity

    if missing_fields:
        blocking_reasons.extend(sorted(set(missing_fields)))
    if stale_fields:
        quality_warnings.append("stale_market_data")

    critical_degraded = bool(set(missing_fields) & {"quote_timestamp", "current_price", "average_dollar_volume_20d", "atr_20_percentage", "ma20", "ma50", "benchmark_status", "benchmark_alignment_status", "market_cap", "history"}) or bool(stale_fields)
    noncritical_degraded = bool(noncritical_source_set and not critical_degraded and candidate_fallback)
    explanation_degraded = bool(noncritical_degraded or any(scope in {"EXPLANATION_ONLY", "NON_CRITICAL_FACTOR", "RUN_LEVEL"} for scope in (aggregate_scope,)))

    if critical_degraded:
        if freshness_status == "STALE" or stale_fields:
            data_status = "STALE"
        else:
            data_status = "INVALID"
        scoring_eligible = False
        if "critical_market_data_missing" not in blocking_reasons:
            if missing_fields:
                blocking_reasons.append("critical_market_data_missing")
        if "critical_market_data_stale" not in blocking_reasons and stale_fields:
            blocking_reasons.append("critical_market_data_stale")
        aggregate_scope = "CRITICAL_MARKET_DATA"
        aggregate_severity = "CRITICAL"
    else:
        data_status = "COMPLETE"
        scoring_eligible = True
        if noncritical_degraded:
            quality_warnings.append("noncritical_provider_degraded")
        if explanation_degraded:
            quality_warnings.append("explanation_only_degraded")
        if candidate_fallback and not noncritical_degraded and not explanation_degraded:
            quality_warnings.append("provider_fallback_used")

    if not blocking_reasons and not scoring_eligible:
        blocking_reasons.append("critical_data_unavailable")

    if not aggregate_scope and candidate_fallback:
        aggregate_scope = "EXPLANATION_ONLY" if not critical_degraded else "CRITICAL_MARKET_DATA"
        aggregate_severity = "INFO" if aggregate_scope == "EXPLANATION_ONLY" else "CRITICAL"

    return DataQualityResult(
        data_status=data_status,
        scoring_eligible=scoring_eligible,
        blocking_reasons=_unique(blocking_reasons),
        missing_fields=_unique(missing_fields),
        stale_fields=_unique(stale_fields),
        critical_fields=tuple(dict.fromkeys(field for field in critical_fields if field)),
        data_as_of=data_as_of.isoformat() if data_as_of is not None else None,
        quote_as_of=quote_as_of.isoformat() if quote_as_of is not None else None,
        ohlcv_as_of=ohlcv_as_of.isoformat() if ohlcv_as_of is not None else None,
        benchmark_as_of=benchmark_as_of.isoformat() if benchmark_as_of is not None else None,
        provider_chain=provider_chain,
        critical_data_sources=_unique(sorted(critical_source_set)),
        noncritical_data_sources=_unique(sorted(noncritical_source_set)),
        fallback_scope=aggregate_scope or "",
        fallback_severity=aggregate_severity or "",
        quality_warnings=_unique(quality_warnings),
        explanation_degraded=explanation_degraded,
        critical_data_degraded=critical_degraded,
        noncritical_data_degraded=noncritical_degraded,
        candidate_fallback=candidate_fallback,
        mock_used=mock_used,
    )


def enrich_candidate_quality(
    candidate: Mapping[str, Any],
    *,
    provider_audit: Mapping[str, Any] | None = None,
    provider_outputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = evaluate_candidate_data_quality(
        candidate,
        provider_audit=provider_audit,
        provider_outputs=provider_outputs,
    )
    payload = dict(candidate or {})
    data_quality = result.to_dict()
    warning_list = list(data_quality.get("quality_warnings") or [])
    if result.blocking_reasons:
        warning_list.extend(f"blocked:{reason}" for reason in result.blocking_reasons)
    warning_list.extend(
        f"missing:{field}" for field in result.missing_fields if field not in warning_list
    )
    warning_list = list(dict.fromkeys(warning_list))
    data_quality_score = 100.0
    if result.data_status == "INVALID":
        data_quality_score = 0.0
    elif result.data_status == "STALE":
        data_quality_score = 35.0
    elif warning_list:
        data_quality_score = 85.0 if result.scoring_eligible else 50.0

    payload["data_quality"] = data_quality
    payload["data_quality_score"] = round(data_quality_score, 2)
    payload["quality_warnings"] = warning_list
    payload["quality_warnings_structured"] = list(warning_list)
    payload["data_status"] = result.data_status
    payload["scoring_eligible"] = result.scoring_eligible
    quote_status = str(payload.get("quote_status") or "").upper()
    ohlcv_status = str(payload.get("ohlcv_status") or "").upper()
    history_status = str(payload.get("history_status") or "").upper()
    quote_fetch_status = str(payload.get("quote_fetch_status") or "").upper()
    ohlcv_fetch_status = str(payload.get("ohlcv_fetch_status") or "").upper()
    if quote_fetch_status == "COMPLETE" and result.quote_as_of and payload.get("current_price") is not None:
        quote_status = "COMPLETE"
    elif not quote_status:
        quote_status = "OK" if payload.get("quote_timestamp") else "MISSING"
    if ohlcv_fetch_status == "COMPLETE" and (payload.get("close_history") or payload.get("daily_data_as_of")):
        ohlcv_status = "COMPLETE"
    elif not ohlcv_status:
        ohlcv_status = "OK" if payload.get("close_history") or payload.get("daily_data_as_of") else "MISSING"
    history_missing_windows = [
        str(item)
        for item in list(payload.get("history_missing_windows") or [])
        if str(item or "").strip()
    ]
    if ohlcv_fetch_status == "COMPLETE" and payload.get("close_history") and not history_missing_windows:
        history_status = "COMPLETE"
    elif history_missing_windows:
        history_status = "MISSING"
    elif not history_status:
        history_status = "OK" if payload.get("close_history") else "MISSING"
    payload["quote_status"] = quote_status
    payload["ohlcv_status"] = ohlcv_status
    payload["history_status"] = history_status
    payload["factor_status"] = str(
        payload.get("factor_status")
        or (
            "OK"
            if payload.get("average_dollar_volume_20d") is not None
            and payload.get("atr_20_percentage") is not None
            and payload.get("ma20") is not None
            and payload.get("ma50") is not None
            else "MISSING"
        )
    ).upper()
    payload["benchmark_status"] = str(payload.get("benchmark_status") or payload.get("benchmark_alignment_status") or "").upper()
    payload["quote_timestamp"] = payload.get("quote_timestamp")
    payload["quote_age_seconds"] = payload.get("quote_age_seconds")
    payload["daily_data_status"] = str(payload.get("daily_data_status") or "").upper()
    payload["freshness_status"] = str(payload.get("freshness_status") or "").upper()
    payload["selection_stage"] = str(payload.get("selection_stage") or "").upper()
    payload["blocking_reasons"] = list(result.blocking_reasons)
    payload["missing_fields"] = list(result.missing_fields)
    payload["stale_fields"] = list(result.stale_fields)
    payload["critical_fields"] = list(result.critical_fields)
    payload["provider_chain"] = list(result.provider_chain)
    payload["critical_data_sources"] = list(result.critical_data_sources)
    payload["noncritical_data_sources"] = list(result.noncritical_data_sources)
    payload["fallback_scope"] = result.fallback_scope
    payload["fallback_severity"] = result.fallback_severity
    payload["candidate_fallback"] = bool(result.candidate_fallback)
    payload["mock_used"] = bool(result.mock_used)
    payload["score_reason"] = str(payload.get("score_reason") or payload.get("reason") or "")
    payload["confidence_score"] = float(payload.get("confidence") or payload.get("confidence_score") or 0.0)
    payload["why_selected"] = str(
        payload.get("why_selected")
        or (
            "关键数据完整且排名靠前"
            if result.scoring_eligible
            else "关键数据不足，候选仅供研究排障"
        )
    )
    return payload


_FORMAL_INELIGIBLE_STATUSES = {"INVALID", "STALE", "MISSING"}
_FORMAL_ELIGIBLE_STATUSES = {"COMPLETE", "VALID", "OK", "LATEST_COMPLETED_SESSION"}
_FORMAL_CRITICAL_BLOCK_REASONS = {
    "critical_market_data_missing",
    "critical_market_data_stale",
    "critical_data_unavailable",
    "quote_missing",
    "quote_invalid",
    "quote_stale",
    "quote_expired",
    "quote_timestamp_in_future",
    "missing_quote",
    "missing_ohlcv",
    "missing_history",
    "benchmark_invalid",
    "daily_data_invalid",
    "stale_data",
}


def formal_selection_ineligibility_reasons(candidate: Mapping[str, Any]) -> list[str]:
    payload = dict(candidate or {})
    data_quality = payload.get("data_quality") if isinstance(payload.get("data_quality"), Mapping) else {}

    def present(*keys: str) -> Any:
        value = _candidate_field_value(payload, *keys)
        if value is not None:
            return value
        if isinstance(data_quality, Mapping):
            return _candidate_field_value(data_quality, *keys)
        return None

    reasons: list[str] = []

    data_status = _normalize_text(present("data_status")).upper()
    if data_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"data_status_{data_status.lower()}")

    scoring_eligible = present("scoring_eligible")
    if scoring_eligible is False:
        reasons.append("scoring_eligible_false")

    quote_status = _normalize_text(present("quote_status")).upper()
    if quote_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"quote_status_{quote_status.lower()}")

    ohlcv_status = _normalize_text(present("ohlcv_status")).upper()
    if ohlcv_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"ohlcv_status_{ohlcv_status.lower()}")

    history_status = _normalize_text(present("history_status")).upper()
    if history_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"history_status_{history_status.lower()}")

    benchmark_status = _normalize_text(present("benchmark_status", "benchmark_alignment_status")).upper()
    if benchmark_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"benchmark_status_{benchmark_status.lower()}")

    freshness_status = _normalize_text(present("freshness_status")).upper()
    if freshness_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"freshness_status_{freshness_status.lower()}")

    daily_data_status = _normalize_text(present("daily_data_status")).upper()
    if daily_data_status in _FORMAL_INELIGIBLE_STATUSES:
        reasons.append(f"daily_data_status_{daily_data_status.lower()}")

    fallback_scope = _normalize_text(present("fallback_scope")).upper()
    fallback_severity = _normalize_text(present("fallback_severity")).upper()
    if fallback_scope == "CRITICAL_MARKET_DATA":
        reasons.append("critical_market_data_fallback")
    if fallback_severity == "CRITICAL":
        reasons.append("critical_fallback_severity")

    blocking_reasons = present("blocking_reasons") or []
    if isinstance(blocking_reasons, (str, bytes)):
        blocking_reasons = [blocking_reasons]
    for reason in list(blocking_reasons or []):
        text = _normalize_text(reason).lower()
        if not text:
            continue
        if text in _FORMAL_CRITICAL_BLOCK_REASONS:
            reasons.append(text)

    missing_fields = present("missing_fields") or []
    if isinstance(missing_fields, (str, bytes)):
        missing_fields = [missing_fields]
    critical_missing = {
        "quote",
        "quote_timestamp",
        "current_price",
        "ohlcv",
        "history",
        "benchmark",
        "market_cap",
        "average_dollar_volume_20d",
        "atr_20_percentage",
        "ma20",
        "ma50",
    }
    for field in list(missing_fields or []):
        text = _normalize_text(field)
        if text in critical_missing:
            reasons.append(f"missing_{text}")

    stale_fields = present("stale_fields") or []
    if isinstance(stale_fields, (str, bytes)):
        stale_fields = [stale_fields]
    if stale_fields:
        reasons.append("stale_fields_present")

    return list(dict.fromkeys(reasons))


def is_formal_selection_eligible(candidate: Mapping[str, Any]) -> bool:
    return not formal_selection_ineligibility_reasons(candidate)
