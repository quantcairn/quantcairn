"""Per-symbol data quality diagnostics for the MARKET_DATA pipeline stage.

Traces the exact failure path for every symbol that doesn't reach scoring:
  1. OHLCV fetch (PriceFetcher / yfinance) → rows available?
  2. Fallback profile → exists?
  3. Fallback + universe filter → passes?

Never modifies scoring or selection logic.  Only reads existing data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Sequence

import numpy as np
import pandas as pd

from src.config.runtime_values import get_runtime_env
from src.data.fetcher import PriceFetcher, _provider_ticker
from src.openalpha.universe_filter import (
    UniverseEvaluation,
    evaluate_universe_candidate,
    infer_asset_type,
)

logger = logging.getLogger(__name__)

MIN_HISTORY_ROWS = 60


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

    return evaluate_universe_candidate(candidate)


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
