"""Per-symbol data quality diagnostics for the MARKET_DATA pipeline stage.

Traces the exact failure path for every symbol that doesn't reach scoring:
  1. OHLCV fetch (PriceFetcher / yfinance) → rows available?
  2. Fallback profile → exists?
  3. Fallback + universe filter → passes?

Never modifies scoring or selection logic.  Only reads existing data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd

from src.config.runtime_values import get_runtime_env
from src.data.fetcher import PriceFetcher, _provider_ticker
from src.openalpha.data_quality import evaluate_candidate_data_quality, normalize_candidate_quality_state
from src.openalpha.market_context import build_candidate_market_snapshot
from src.openalpha.selection_report import normalize_provider_audit
from src.openalpha.universe_filter import (
    UniverseEvaluation,
    evaluate_universe_candidate,
    infer_asset_type,
)

logger = logging.getLogger(__name__)

MIN_HISTORY_ROWS = 60


@dataclass(frozen=True, slots=True)
class MarketDataAudit:
    symbol: str
    provider_attempts: tuple[dict[str, Any], ...]
    provider_used: str
    cache_status: str
    quote_status: str
    ohlcv_status: str
    history_status: str
    benchmark_status: str
    first_failure_node: str
    normalized_failure_reason: str
    retry_count: int
    formal_data_ready: bool
    record_completeness: str = ""
    market_data_sufficiency: str = ""
    research_evidence_status: str = ""
    formal_scoring_eligibility: bool = False
    data_status: str = ""
    freshness_status: str = ""
    quote_fetch_status: str = ""
    ohlcv_fetch_status: str = ""
    benchmark_alignment_status: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider_attempts": [dict(item) for item in self.provider_attempts],
            "provider_used": self.provider_used,
            "cache_status": self.cache_status,
            "quote_status": self.quote_status,
            "ohlcv_status": self.ohlcv_status,
            "history_status": self.history_status,
            "benchmark_status": self.benchmark_status,
            "first_failure_node": self.first_failure_node,
            "normalized_failure_reason": self.normalized_failure_reason,
            "retry_count": self.retry_count,
            "formal_data_ready": self.formal_data_ready,
            "record_completeness": self.record_completeness,
            "market_data_sufficiency": self.market_data_sufficiency,
            "research_evidence_status": self.research_evidence_status,
            "formal_scoring_eligibility": self.formal_scoring_eligibility,
            "data_status": self.data_status,
            "freshness_status": self.freshness_status,
            "quote_fetch_status": self.quote_fetch_status,
            "ohlcv_fetch_status": self.ohlcv_fetch_status,
            "benchmark_alignment_status": self.benchmark_alignment_status,
            "notes": list(self.notes),
        }


_AUDIT_REASON_ALIASES: dict[str, str] = {
    "CACHE_ERROR": "cache_error",
    "YFINANCE_CACHE_ERROR": "cache_error",
    "cache_dir_not_writable": "cache_error",
    "cache_dir_create_failed": "cache_error",
    "cache_config_failed": "cache_error",
    "PROVIDER_ERROR": "provider_error",
    "FAILED": "provider_failure",
    "PARTIAL": "provider_partial",
    "NOT_RUN": "research_evidence_not_run",
    "provider_not_run": "research_evidence_not_run",
    "YAHOO_UNAUTHORIZED": "auth_failed",
    "DNS_ERROR": "provider_error",
    "EMPTY_RESPONSE": "empty_response",
    "EMPTY_JSON": "empty_response",
    "MISSING_CHART": "empty_response",
    "MISSING_RESULT": "empty_response",
    "EMPTY_RESULT": "empty_response",
    "MALFORMED_RESPONSE": "invalid_payload",
    "NON_DICT_JSON": "invalid_payload",
    "NON_DICT_RESULT": "invalid_payload",
    "CHART_PARSE_ERROR": "invalid_payload",
    "NO_HISTORY": "ohlcv_missing",
    "quote_missing": "quote_missing",
    "ohlcv_missing": "ohlcv_missing",
    "history_insufficient": "history_insufficient",
    "benchmark_invalid": "benchmark_invalid",
    "benchmark_alignment_failed": "benchmark_invalid",
    "critical_market_data_missing": "market_data_missing",
    "critical_market_data_stale": "market_data_stale",
    "market_data_sufficiency_failed": "market_data_sufficiency_failed",
    "formal_scoring_ineligible": "formal_scoring_ineligible",
}

_SENTINEL_PROVIDER_NAMES = {
    "",
    "N/A",
    "NA",
    "NONE",
    "NULL",
    "UNKNOWN",
    "UNAVAILABLE",
}


def _provider_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.upper() in _SENTINEL_PROVIDER_NAMES:
        return ""
    return text


def _audit_reason(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for piece in (text, text.split(":", 1)[0], text.split(";", 1)[0]):
            key = piece.strip().upper() if piece.isupper() else piece.strip().lower()
            if key in _AUDIT_REASON_ALIASES:
                return _AUDIT_REASON_ALIASES[key]
        lowered = text.lower()
        if "cache" in lowered and "error" in lowered:
            return "cache_error"
        if "invalid crumb" in lowered or "unauthorized" in lowered or "401" in lowered:
            return "auth_failed"
        if "dns" in lowered or "getaddrinfo" in lowered:
            return "provider_error"
        if "empty" in lowered or "missing result" in lowered or "missing chart" in lowered:
            return "empty_response"
        if "malformed" in lowered or "parse" in lowered or "non-dict" in lowered:
            return "invalid_payload"
        if "benchmark" in lowered and "align" in lowered:
            return "benchmark_invalid"
    return "unknown"


def _node_status(status: Any) -> str:
    text = str(status or "").strip().upper()
    if text in {"COMPLETE", "VALID", "OK", "LATEST_COMPLETED_SESSION"}:
        return "COMPLETE"
    if text in {"CACHE_ERROR", "EMPTY_RESPONSE", "MALFORMED_RESPONSE", "PROVIDER_ERROR", "INVALID", "MISSING", "STALE", "FAILED"}:
        return text
    if not text:
        return "UNKNOWN"
    return text


def _first_failure_node(attempts: Sequence[dict[str, Any]], formal_ready: bool) -> str:
    for attempt in attempts:
        status = _node_status(attempt.get("status"))
        if status not in {"COMPLETE", "VALID", "OK", "READY"}:
            return str(attempt.get("node") or attempt.get("provider") or "unknown")
    return "validation" if not formal_ready else ""


def _build_provider_attempts(snapshot: Mapping[str, Any], quality: Mapping[str, Any], provider_audit: Mapping[str, Any] | None = None, provider_outputs: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    benchmark_symbols = list(snapshot.get("benchmark_symbols") or [])
    benchmark_quote_status = dict(snapshot.get("benchmark_quote_fetch_status") or {})
    benchmark_quote_error_code = dict(snapshot.get("benchmark_quote_error_code") or {})
    benchmark_quote_error_message = dict(snapshot.get("benchmark_quote_error_message") or {})
    benchmark_quote_provider_used = dict(snapshot.get("benchmark_quote_provider_used") or {})
    benchmark_quote_retry_count = dict(snapshot.get("benchmark_quote_retry_count") or {})
    benchmark_ohlcv_status = dict(snapshot.get("benchmark_ohlcv_fetch_status") or {})
    benchmark_ohlcv_error_code = dict(snapshot.get("benchmark_ohlcv_error_code") or {})
    benchmark_ohlcv_error_message = dict(snapshot.get("benchmark_ohlcv_error_message") or {})
    benchmark_ohlcv_provider_used = dict(snapshot.get("benchmark_ohlcv_provider_used") or {})
    benchmark_ohlcv_retry_count = dict(snapshot.get("benchmark_ohlcv_retry_count") or {})

    attempts.append(
        {
            "node": "cache",
            "provider": "yfinance_cache",
            "status": str(snapshot.get("cache_status") or "UNKNOWN").upper(),
            "failure_reason": _audit_reason(snapshot.get("cache_error_message"), snapshot.get("cache_status")),
            "retry_count": 0,
        }
    )
    attempts.append(
        {
            "node": "quote",
            "provider": _provider_name(snapshot.get("quote_provider_used")).lower() or "unavailable",
            "status": str(snapshot.get("quote_status") or "UNKNOWN").upper(),
            "failure_reason": _audit_reason(
                snapshot.get("quote_fetch_error_code"),
                snapshot.get("quote_fetch_error_message"),
                snapshot.get("quote_fetch_status"),
                snapshot.get("quote_status"),
            ),
            "retry_count": int(snapshot.get("quote_retry_count") or 0),
        }
    )
    attempts.append(
        {
            "node": "ohlcv",
            "provider": _provider_name(snapshot.get("ohlcv_provider_used")).lower() or "unavailable",
            "status": str(snapshot.get("ohlcv_status") or "UNKNOWN").upper(),
            "failure_reason": _audit_reason(
                snapshot.get("ohlcv_fetch_error_code"),
                snapshot.get("ohlcv_fetch_error_message"),
                snapshot.get("ohlcv_fetch_status"),
                snapshot.get("ohlcv_status"),
                snapshot.get("history_status"),
            ),
            "retry_count": int(snapshot.get("ohlcv_retry_count") or 0),
        }
    )
    attempts.append(
        {
            "node": "history",
            "provider": _provider_name(snapshot.get("history_provider_used"))
            .lower()
            or _provider_name(snapshot.get("ohlcv_provider_used")).lower()
            or "unavailable",
            "status": str(snapshot.get("history_status") or "UNKNOWN").upper(),
            "failure_reason": _audit_reason(
                "history_insufficient" if snapshot.get("history_missing_windows") else "",
                snapshot.get("history_status"),
            ),
            "retry_count": int(snapshot.get("history_retry_count") or 0),
        }
    )

    for benchmark_symbol in benchmark_symbols:
        attempts.append(
            {
                "node": f"benchmark_quote:{benchmark_symbol}",
                "provider": _provider_name(benchmark_quote_provider_used.get(benchmark_symbol)).lower() or "benchmark",
                "status": str(benchmark_quote_status.get(benchmark_symbol) or "UNKNOWN").upper(),
                "failure_reason": _audit_reason(
                    benchmark_quote_error_code.get(benchmark_symbol),
                    benchmark_quote_error_message.get(benchmark_symbol),
                    benchmark_quote_status.get(benchmark_symbol),
                ),
                "retry_count": int(benchmark_quote_retry_count.get(benchmark_symbol) or 0),
            }
        )
        attempts.append(
            {
                "node": f"benchmark_ohlcv:{benchmark_symbol}",
                "provider": _provider_name(benchmark_ohlcv_provider_used.get(benchmark_symbol)).lower() or "benchmark",
                "status": str(benchmark_ohlcv_status.get(benchmark_symbol) or "UNKNOWN").upper(),
                "failure_reason": _audit_reason(
                    benchmark_ohlcv_error_code.get(benchmark_symbol),
                    benchmark_ohlcv_error_message.get(benchmark_symbol),
                    benchmark_ohlcv_status.get(benchmark_symbol),
                ),
                "retry_count": int(benchmark_ohlcv_retry_count.get(benchmark_symbol) or 0),
            }
        )

    attempts.append(
        {
            "node": "validation",
            "provider": "data_quality",
            "status": "COMPLETE" if quality.get("formal_scoring_eligibility") else "FAILED",
            "failure_reason": _audit_reason(
                quality.get("market_data_sufficiency"),
                quality.get("data_status"),
                quality.get("research_evidence_status"),
                quality.get("blocking_reasons"),
            ),
            "retry_count": 0,
        }
    )
    return attempts


def build_market_data_audit(
    symbol: str,
    *,
    now_et: datetime | None = None,
    snapshot: Mapping[str, Any] | None = None,
    data_quality: Mapping[str, Any] | None = None,
    provider_audit: Mapping[str, Any] | None = None,
    provider_outputs: Mapping[str, Any] | None = None,
) -> MarketDataAudit:
    snapshot = dict(snapshot or build_candidate_market_snapshot(symbol, now_et=now_et))
    quality_result = data_quality
    if quality_result is None:
        quality_result = evaluate_candidate_data_quality(
            snapshot,
            provider_audit=provider_audit,
            provider_outputs=provider_outputs,
        ).to_dict()
    elif hasattr(quality_result, "to_dict"):
        quality_result = quality_result.to_dict()
    quality = dict(quality_result or {})
    normalized = normalize_candidate_quality_state(
        snapshot,
        data_quality=quality,
        provider_audit=provider_audit,
        provider_outputs=provider_outputs,
    )
    provider_summary = normalize_provider_audit(
        dict(provider_audit or {}),
        dict(provider_outputs or {}),
    )
    attempts = _build_provider_attempts(snapshot, normalized, provider_audit, provider_outputs)
    provider_used = (
        _provider_name(snapshot.get("quote_provider_used"))
        or _provider_name(snapshot.get("ohlcv_provider_used"))
        or _provider_name(snapshot.get("history_provider_used"))
        or "UNKNOWN"
    )
    if provider_used == "UNKNOWN":
        for entry in attempts:
            provider_name = _provider_name(entry.get("provider"))
            if provider_name and _node_status(entry.get("status")) in {"COMPLETE", "VALID", "OK"}:
                provider_used = provider_name
                break
    retry_count = sum(int(entry.get("retry_count") or 0) for entry in attempts)
    first_failure = _first_failure_node(attempts, bool(normalized.get("formal_scoring_eligibility")))
    normalized_failure_reason = "unknown"
    if first_failure:
        for entry in attempts:
            if str(entry.get("node") or "") == first_failure:
                normalized_failure_reason = str(entry.get("failure_reason") or "unknown")
                break
    formal_data_ready = bool(normalized.get("formal_scoring_eligibility"))
    return MarketDataAudit(
        symbol=str(snapshot.get("symbol") or _provider_ticker(symbol) or symbol).upper(),
        provider_attempts=tuple(attempts),
        provider_used=str(provider_used).upper(),
        cache_status=str(snapshot.get("cache_status") or "UNKNOWN").upper(),
        quote_status=str(snapshot.get("quote_status") or "UNKNOWN").upper(),
        ohlcv_status=str(snapshot.get("ohlcv_status") or "UNKNOWN").upper(),
        history_status=str(snapshot.get("history_status") or "UNKNOWN").upper(),
        benchmark_status=str(snapshot.get("benchmark_status") or "UNKNOWN").upper(),
        first_failure_node=first_failure,
        normalized_failure_reason=normalized_failure_reason,
        retry_count=retry_count,
        formal_data_ready=formal_data_ready,
        record_completeness=str(normalized.get("record_completeness") or "").upper(),
        market_data_sufficiency=str(normalized.get("market_data_sufficiency") or "").upper(),
        research_evidence_status=str(normalized.get("research_evidence_status") or "").upper(),
        formal_scoring_eligibility=bool(normalized.get("formal_scoring_eligibility")),
        data_status=str(normalized.get("data_status") or quality.get("data_status") or "").upper(),
        freshness_status=str(snapshot.get("freshness_status") or "").upper(),
        quote_fetch_status=str(snapshot.get("quote_fetch_status") or "").upper(),
        ohlcv_fetch_status=str(snapshot.get("ohlcv_fetch_status") or "").upper(),
        benchmark_alignment_status=str(snapshot.get("benchmark_alignment_status") or "").upper(),
        notes=tuple(provider_summary.get("contributors") or []),
    )


def diagnose_market_data(
    symbols: Sequence[str],
    *,
    now_et: datetime | None = None,
) -> list[MarketDataAudit]:
    return [build_market_data_audit(symbol, now_et=now_et) for symbol in symbols]


# ═══════════════════════════════════════════════════════════════════════════════
# Diagnostic entry point
# ═══════════════════════════════════════════════════════════════════════════════


def diagnose_market_data_drops(
    universe_symbols: list[str],
    scored_symbols: list[str],
) -> list[dict[str, Any]]:
    """For every universe symbol that didn't reach scoring, produce a diagnostic record.

    Returns a list of dropped records suitable for passing to
    FunnelTracker.add_stage(dropped=...).
    """
    scored_set = {s.strip().upper() for s in scored_symbols}
    records: list[dict[str, Any]] = []

    for symbol in universe_symbols:
        sym = symbol.strip().upper()
        if sym in scored_set:
            continue  # This one survived — don't include in diagnostics

        diag = _diagnose_one(sym)
        records.append(
            {
                "symbol": sym,
                "reason_code": diag["reason_code"],
                "reason_detail": diag["reason_detail"],
                "available_rows": diag["available_rows"],
                "required_rows": diag["required_rows"],
                "missing_fields": diag["missing_fields"],
                "ohlcv_error": diag.get("ohlcv_error"),
                "has_fallback_profile": diag["has_fallback_profile"],
                "fallback_rejected_by_universe": diag["fallback_rejected_by_universe"],
            }
        )

    return records


def check_data_availability(
    symbols: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Pre-scoring market data check. Determines which symbols have actual
    OHLCV data available, independently of scoring logic.

    Returns:
        available  — symbols with >= MIN_HISTORY_ROWS of OHLCV data
        dropped    — per-symbol diagnostic records for unavailable symbols
    """
    available: list[str] = []
    dropped: list[dict[str, Any]] = []

    for symbol in symbols:
        sym = symbol.strip().upper()
        diag = _diagnose_one(sym)
        if diag["available_rows"] >= MIN_HISTORY_ROWS:
            available.append(sym)
        else:
            dropped.append(
                {
                    "symbol": sym,
                    "reason_code": diag["reason_code"],
                    "reason_detail": diag["reason_detail"],
                    "available_rows": diag["available_rows"],
                    "required_rows": diag["required_rows"],
                    "missing_fields": diag["missing_fields"],
                    "ohlcv_error": diag.get("ohlcv_error"),
                    "has_fallback_profile": diag["has_fallback_profile"],
                    "fallback_rejected_by_universe": diag["fallback_rejected_by_universe"],
                }
            )

    return available, dropped


# ═══════════════════════════════════════════════════════════════════════════════
# Symbol-level diagnostic
# ═══════════════════════════════════════════════════════════════════════════════


def _diagnose_one(symbol: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "symbol": symbol,
        "reason_code": "unknown",
        "reason_detail": "",
        "available_rows": 0,
        "required_rows": MIN_HISTORY_ROWS,
        "missing_fields": [],
        "ohlcv_error": None,
        "has_fallback_profile": False,
        "fallback_rejected_by_universe": False,
        "fallback_reasons": [],
    }

    # ── 1. Try OHLCV fetch (same path the Scorer uses) ───────────────────
    ohlcv_rows, ohlcv_error = _try_ohlcv_fetch(symbol)
    result["available_rows"] = ohlcv_rows
    result["ohlcv_error"] = ohlcv_error

    # ── 2. Check fallback profile ───────────────────────────────────────
    fallback = _lookup_fallback_profile(symbol)
    if fallback is not None:
        result["has_fallback_profile"] = True
        eval_result = _evaluate_fallback_against_universe_filter(symbol, fallback)
        if eval_result.rejected:
            result["fallback_rejected_by_universe"] = True
            result["fallback_reasons"] = list(eval_result.rejection_reason)
            result["missing_fields"].extend(eval_result.rejection_reason)

    # ── 3. Classify the failure ──────────────────────────────────────────
    _classify_failure(result, symbol)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Data fetch attempt (mirrors Scorer._load_history)
# ═══════════════════════════════════════════════════════════════════════════════


def _try_ohlcv_fetch(symbol: str) -> tuple[int, str | None]:
    """Try to fetch OHLCV data for a symbol.

    Returns (row_count, error_message).  row_count of 0 means no data
    was returned by any source.
    """
    provider_symbol = _provider_ticker(symbol)
    errors: list[str] = []

    # Path 1: PriceFetcher (primary — same as Scorer._load_history)
    try:
        fetcher = PriceFetcher(provider_symbol, poll_interval=0)
        try:
            candles = fetcher.get_ohlcv(period="1y", interval="1d")
            if candles:
                return len(candles), None
        finally:
            fetcher.close()
    except Exception as exc:
        errors.append(f"PriceFetcher: {_short_error(exc)}")

    # Path 2: Yahoo chart API (fallback — mirrors Scorer._fetch_chart_daily)
    try:
        df = _fetch_yahoo_chart_direct(provider_symbol)
        if df is not None and not df.empty:
            return len(df), None
    except Exception as exc:
        errors.append(f"chart_api: {_short_error(exc)}")

    # Path 3: yfinance download
    try:
        import yfinance as yf

        df = yf.download(provider_symbol, period="260d", interval="1d", progress=False)
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return len(df), None
    except Exception as exc:
        errors.append(f"yfinance: {_short_error(exc)}")

    error = "; ".join(errors) if errors else None
    return 0, error


def _fetch_yahoo_chart_direct(symbol: str) -> pd.DataFrame | None:
    """Direct Yahoo Finance v8 chart API call (no yfinance dependency)."""
    import urllib.request
    import json

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=1y&interval=1d"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except Exception:
        return None

    try:
        payload = json.loads(resp.read().decode())
    except Exception:
        return None

    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        return None

    result = (chart.get("result") or [None])[0]
    if not result:
        return None

    quote = (result.get("indicators", {}).get("quote") or [None])[0]
    timestamps = result.get("timestamp") or []
    if not quote or not timestamps:
        return None

    df = pd.DataFrame(
        {
            "Open": pd.to_numeric(
                pd.Series(quote.get("open") or []), errors="coerce"
            ),
            "High": pd.to_numeric(
                pd.Series(quote.get("high") or []), errors="coerce"
            ),
            "Low": pd.to_numeric(
                pd.Series(quote.get("low") or []), errors="coerce"
            ),
            "Close": pd.to_numeric(
                pd.Series(quote.get("close") or []), errors="coerce"
            ),
            "Volume": pd.to_numeric(
                pd.Series(quote.get("volume") or []), errors="coerce"
            ),
        }
    )
    # Drop rows with NaN Close
    df = df.dropna(subset=["Close"])
    return df if not df.empty else None


# ═══════════════════════════════════════════════════════════════════════════════
# Fallback profile check
# ═══════════════════════════════════════════════════════════════════════════════


def _lookup_fallback_profile(symbol: str) -> dict | None:
    """Check whether the symbol has a hard-coded fallback profile in Scorer."""
    try:
        from src.scoring.scorer import Scorer

        return Scorer.FALLBACK_PROFILES.get(symbol)
    except Exception:
        return None


def _evaluate_fallback_against_universe_filter(
    symbol: str, profile: dict
) -> UniverseEvaluation:
    """Replay the universe filter check that _fallback_scored_item performs."""
    support = float(profile["range_low"])
    resistance = float(profile["range_high"])
    price_mid = (support + resistance) / 2.0
    band_pct = (
        ((resistance - support) / price_mid * 100.0) if price_mid else 0.0
    )

    from src.scoring.scorer import Scorer

    market_cap = Scorer.FALLBACK_MARKET_CAP.get(symbol)

    candidate = {
        "ticker": symbol,
        "current_price": price_mid,
        "asset_type": infer_asset_type(symbol),
        "market_cap": market_cap,
        "average_dollar_volume_20d": float(profile["volume"]) * price_mid,
        "atr_20_percentage": band_pct / 2.0,
    }

    # Fallback uses skip_atr_validation to avoid rejecting synthetic volatility
    return evaluate_universe_candidate(candidate, skip_atr_validation=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Failure classification
# ═══════════════════════════════════════════════════════════════════════════════

_REASON_LABELS: dict[str, str] = {
    "no_ohlcv_data_and_no_fallback": "无OHLCV数据且无回退配置",
    "no_ohlcv_data_fallback_blocked_universe": "无OHLCV数据且回退配置被Universe过滤拦截",
    "insufficient_ohlcv_rows": "OHLCV历史数据不足(需>=60行)",
    "ohlcv_success_but_scoring_failed": "数据获取成功但评分管道异常失败",
    "market_data_sufficiency_failed": "行情数据不足",
}


def _classify_failure(result: dict[str, Any], symbol: str) -> None:
    rows = result["available_rows"]
    has_fallback = result["has_fallback_profile"]
    fallback_blocked = result["fallback_rejected_by_universe"]
    fallback_reasons = result.get("fallback_reasons") or []

    if rows < MIN_HISTORY_ROWS:
        if not has_fallback:
            result["reason_code"] = "no_ohlcv_data_and_no_fallback"
            result["missing_fields"].append("ohlcv_history")
            result["missing_fields"].append("fallback_profile")
        elif fallback_blocked:
            result["reason_code"] = "no_ohlcv_data_fallback_blocked_universe"
            for r in fallback_reasons:
                if r not in result["missing_fields"]:
                    result["missing_fields"].append(r)
        else:
            result["reason_code"] = "insufficient_ohlcv_rows"
    else:
        result["reason_code"] = "ohlcv_success_but_scoring_failed"

    label = _REASON_LABELS.get(result["reason_code"], result["reason_code"])
    reasons_detail = (
        f"fallback_block: [{', '.join(fallback_reasons)}]"
        if fallback_reasons
        else "no fallback"
    )
    fbtxt = " (回退被拦截)" if fallback_blocked else ""
    result["reason_detail"] = (
        f"{label}{fbtxt}. available={rows}/{MIN_HISTORY_ROWS} rows. ohlcv_err={result['ohlcv_error'] or 'none'}. {reasons_detail}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _short_error(exc: Exception) -> str:
    msg = str(exc)
    # Strip stack traces: take first meaningful line
    for line in msg.split("\n"):
        line = line.strip()
        if line and "Traceback" not in line and "File " not in line[:6]:
            return line[:200]
    return msg[:200]
