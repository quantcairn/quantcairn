#!/usr/bin/env python3
"""Daily AI stock selector runner

Usage: scripts/run_ai_selector.py
"""
import os
import sys
import subprocess

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
VENV_PREFIX = os.path.join(PROJECT_ROOT, ".venv")
if (
    os.path.exists(VENV_PYTHON)
    and os.path.realpath(sys.prefix) != os.path.realpath(VENV_PREFIX)
    and os.environ.get("SOXS_SKIP_VENV_REEXEC") != "1"
):
    env = os.environ.copy()
    env["SOXS_SKIP_VENV_REEXEC"] = "1"
    os.execve(VENV_PYTHON, [VENV_PYTHON, __file__, *sys.argv[1:]], env)

print(f"Using Python: {sys.executable}")
sys.path.insert(0, PROJECT_ROOT)

from src.ai_selector.integration import AISelector
from src.ai_selector.composition_filter import (
    CompositionFilter,
    is_inverse_etf,
    is_leveraged_or_inverse_etf,
)
from src.ai_selector.selector import AIStrategySelector
from src.ai_selector.range_score import RangeFitnessScorer
from src.ai_selector.trade_filter import TradeEligibilityFilter
from src.utils.market_calendar import market_session_context
from datetime import datetime
import os
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml

from src.config.local_env import load_local_ai_env
from src.ai_selector.settings import load_runtime_settings
from src.ai_selector.selector import write_selection_filter_log
from src.ai_selector.selection_state import write_selection_state
from src.ai_selector.config import load_runtime_config
from src.ai_selector.settings import resolve_price_band
from src.ai_selector.universe_filter import filter_universe_candidates, load_universe_rules
from src.data.fetcher import PriceFetcher
from src.notifier.alerts import notify_ai_selection_result
from src.candidate_validation import CandidateValidationStore

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_DIR / "reports"
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")
AI_SELECTOR_RUNTIME = load_runtime_config()
TOP_COUNT = max(1, int(AI_SELECTOR_RUNTIME.top_n))


def _load_ai_selector_file_config() -> dict:
    merged: dict = {}
    for path in (PROJECT_DIR / "config.yaml", PROJECT_DIR / "config.local.yaml"):
        try:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
            if isinstance(raw, dict):
                section = raw.get("ai_selector")
                if isinstance(section, dict):
                    merged.update(section)
        except Exception:
            continue
    return merged


_AI_SELECTOR_FILE_CONFIG = _load_ai_selector_file_config()
ENTRY_PROXIMITY_ENABLED = bool(_AI_SELECTOR_FILE_CONFIG.get("entry_proximity_enabled", True))
ENTRY_PROXIMITY_WEIGHT = max(0.0, min(1.0, float(_AI_SELECTOR_FILE_CONFIG.get("entry_proximity_weight", 0.0) or 0.0)))
RANGE_SCORER = RangeFitnessScorer()
TRADE_FILTER = TradeEligibilityFilter()
COMPOSITION_FILTER = CompositionFilter()
UNIVERSE_RULES = load_universe_rules()
CONSERVATIVE_FALLBACK_POOL = [
    "SOFI",
    "PLTR",
    "AMD",
    "AAPL",
    "BAC",
    "F",
    "T",
    "PFE",
    "KO",
    "INTC",
]


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _selection_date() -> str:
    return market_session_context(_et_now()).current_session.isoformat()


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_ticker(value: str) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _provider_metadata(
    output: dict,
    live_positions: list[dict] | None,
    ai_meta: dict | None = None,
) -> tuple[list[str], list[str], bool]:
    settings = dict(output.get("settings") or {})
    quality_report = dict(output.get("quality_filter_report") or {})
    data_mode = str(settings.get("data_mode") or "").strip().lower()
    fallback_used = bool(settings.get("fallback_used")) or bool(
        quality_report.get("timed_out")
    )

    providers_used: list[str] = ["selector_core", "yfinance"]
    providers_disabled: list[str] = []

    if ai_meta:
        providers_used.extend(list(ai_meta.get("providers_used") or []))
        providers_disabled.extend(list(ai_meta.get("providers_disabled") or []))
        if ai_meta.get("fallback_used"):
            fallback_used = True

    if os.environ.get("AI_SELECTOR_DIRECT_HISTORY", "1") != "0":
        providers_used.append("yahoo_chart")
    if data_mode in {"live", "mixed"}:
        providers_used.append("market_data_live")
    if data_mode in {"fallback", "mixed"} or fallback_used:
        providers_used.append("market_data_fallback")

    has_longbridge_creds = all(
        [
            os.environ.get("LONGBRIDGE_APP_KEY") or os.environ.get("LONGBRIDGE_API_KEY"),
            os.environ.get("LONGBRIDGE_APP_SECRET") or os.environ.get("LONGBRIDGE_API_SECRET"),
            os.environ.get("LONGBRIDGE_ACCESS_TOKEN"),
        ]
    )
    if has_longbridge_creds:
        providers_used.append("longbridge")
        if live_positions is not None:
            providers_used.append("longbridge_account")
    else:
        providers_disabled.extend(["longbridge", "longbridge_account"])

    openbb_enabled = _truthy_env("SOXS_OPENBB_ENABLED")
    if openbb_enabled:
        providers_used.append("openbb")
    else:
        providers_disabled.append("openbb")

    fmp_enabled = _truthy_env("SOXS_FMP_ENABLED") and bool(os.environ.get("FMP_API_KEY", "").strip())
    if fmp_enabled:
        providers_used.append("fmp")
    else:
        providers_disabled.append("fmp")

    providers_used = list(dict.fromkeys(providers_used))
    providers_disabled = [
        name for name in dict.fromkeys(providers_disabled) if name not in providers_used
    ]
    return providers_used, providers_disabled, fmp_enabled


def _run_integrated_ai_selector() -> dict:
    selector = AISelector()
    signals = selector.get_signals()
    top10 = selector.get_top10()
    metadata = dict(selector.last_run_metadata or {})
    preferred_symbols = [
        _normalize_ticker(item.get("ticker"))
        for item in top10
        if _normalize_ticker(item.get("ticker"))
    ]
    signal_map = {
        _normalize_ticker(item.get("ticker")): dict(item)
        for item in top10
        if _normalize_ticker(item.get("ticker"))
    }
    return {
        "enabled": bool(selector.config.enabled),
        "top3": list(signals or []),
        "top10": list(top10 or []),
        "preferred_symbols": preferred_symbols,
        "signal_map": signal_map,
        "provider_outputs": dict(selector.last_provider_outputs or {}),
        "providers_used": list(metadata.get("providers_used") or []),
        "providers_disabled": list(metadata.get("providers_disabled") or []),
        "fmp_enabled": bool(metadata.get("fmp_enabled", False)),
        "provider_fallback_used": bool(metadata.get("provider_fallback_used", False)),
        "fallback_used": bool(metadata.get("fallback_used", False)),
        "provider_audit": dict(metadata.get("provider_audit") or {}),
    }


def _annotate_with_ai_signals(rows: list[dict], signal_map: dict[str, dict]) -> list[dict]:
    annotated = []
    for raw in rows or []:
        item = dict(raw)
        ticker = _normalize_ticker(item.get("ticker"))
        ai_signal = dict(signal_map.get(ticker) or {})
        if ai_signal:
            item["ai_score"] = float(ai_signal.get("ai_score") or ai_signal.get("score") or 0.0)
            item["range_score"] = float(ai_signal.get("range_score") or item.get("range_score") or 50.0)
            item["final_score"] = float(ai_signal.get("final_score") or ai_signal.get("score") or item.get("score") or 0.0)
            item["confidence"] = float(ai_signal.get("confidence") or item.get("confidence") or 0.0)
            item["reason"] = str(ai_signal.get("reason") or item.get("reason") or "")
            item["source"] = str(ai_signal.get("source") or item.get("source") or "ai_selector")
            for key, value in ai_signal.items():
                if key == "ticker":
                    continue
                item.setdefault(key, value)
                if key in {
                    "current_session",
                    "previous_completed_session",
                    "next_session",
                    "last_completed_session",
                    "is_market_holiday",
                    "is_premarket",
                    "is_regular_session",
                    "is_after_hours",
                    "market_open",
                    "market_session_label",
                    "market_session_status",
                    "market_session_reason",
                    "daily_data_as_of",
                    "daily_data_status",
                    "premarket_snapshot_at",
                    "quote_timestamp",
                    "quote_age_seconds",
                    "premarket_last_price",
                    "premarket_change_pct",
                    "premarket_change_pct_from_previous_close",
                    "premarket_volume",
                    "premarket_dollar_volume",
                    "bid",
                    "ask",
                    "spread_pct",
                    "gap_pct",
                    "benchmark_symbols",
                    "benchmark_data_as_of",
                    "benchmark_change_pct",
                    "benchmark_volume",
                    "benchmark_alignment_status",
                    "benchmark_status",
                    "selection_stage",
                    "freshness_status",
                    "stale_reason",
                    "generated_at",
                    "finalized_at",
                    "trading_eligible",
                    "shadow_enabled",
                    "paper_enabled",
                    "live_enabled",
                    "premarket_snapshot_available",
                    "avg_10d_volume",
                    "close_history",
                    "returns",
                    "recent_low",
                    "recent_high",
                    "three_day_change_pct",
                    "asset_type",
                }:
                    item[key] = value
        annotated.append(item)
    return annotated


def _coalesce_float(*values: object, default: float = 50.0) -> float:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return float(default)


def _range_market_data(item: dict) -> dict:
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    return {
        "current_price": item.get("current_price") or item.get("price_midpoint_hint"),
        "avg_10d_volume": item.get("avg_10d_volume") or item.get("avg_daily_volume_hint"),
        "spread_pct": item.get("spread_pct_live") or item.get("spread_pct"),
        "three_day_change_pct": item.get("three_day_change_pct"),
        "close_history": series.get("closes") or series.get("close_history") or [],
        "returns": series.get("returns") or [],
        "recent_low": item.get("range_low"),
        "recent_high": item.get("range_high"),
        "bid": item.get("bid"),
        "ask": item.get("ask"),
    }


def _apply_range_scores(rows: list[dict]) -> list[dict]:
    scored = []
    for raw in rows or []:
        item = dict(raw)
        ticker = _normalize_ticker(item.get("ticker"))
        range_result = RANGE_SCORER.calculate(ticker, _range_market_data(item))
        ai_score = _coalesce_float(item.get("ai_score"), item.get("score"), default=50.0)
        range_score = _coalesce_float(range_result.get("range_score"), default=50.0)
        final_score = round(0.6 * ai_score + 0.4 * range_score, 2)
        entry = dict(range_result.get("entry") or {})
        entry_score = _coalesce_float(entry.get("entry_proximity_score"), default=50.0)
        if ENTRY_PROXIMITY_ENABLED and ENTRY_PROXIMITY_WEIGHT > 0.0:
            final_score = round(
                final_score * (1.0 - ENTRY_PROXIMITY_WEIGHT) + entry_score * ENTRY_PROXIMITY_WEIGHT,
                2,
            )
        item.update(range_result)
        item["ai_score"] = round(ai_score, 2)
        item["range_score"] = round(range_score, 2)
        item["final_score"] = final_score
        item["score"] = final_score
        item["entry"] = entry
        scored.append(item)
    return sorted(scored, key=lambda item: (-float(item.get("final_score") or 0.0), item.get("ticker") or ""))


def _trade_market_data(item: dict) -> dict:
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    return {
        "earnings_within_days": item.get("earnings_within_days"),
        "price_change_5d": item.get("price_change_5d") or item.get("day_change_pct"),
        "avg_volume": item.get("avg_volume") or item.get("avg_10d_volume") or item.get("volume"),
        "bid_ask_spread_pct": item.get("bid_ask_spread_pct") or item.get("spread_pct_live") or item.get("spread_pct"),
        "premarket_change_pct": item.get("premarket_change_pct") or item.get("gap_pct"),
        "gap_pct": item.get("gap_pct") or item.get("premarket_change_pct"),
        "premarket_volume": item.get("premarket_volume") or item.get("volume"),
        "spread_pct": item.get("spread_pct") or item.get("bid_ask_spread_pct"),
        "quote_age_seconds": item.get("quote_age_seconds"),
        "daily_data_as_of": item.get("daily_data_as_of"),
        "benchmark_data_as_of": item.get("benchmark_data_as_of"),
        "selection_stage": item.get("selection_stage"),
        "trading_eligible": item.get("trading_eligible"),
        "regime": item.get("regime") or "NORMAL",
        "data_age_seconds": item.get("data_age_seconds") or 0,
        "current_price": item.get("current_price") or item.get("price_midpoint_hint"),
        "close_history": series.get("closes") or series.get("close_history") or [],
        "returns": series.get("returns") or [],
        "recent_low": item.get("range_low"),
        "recent_high": item.get("range_high"),
        "bid": item.get("bid"),
        "ask": item.get("ask"),
    }


def _apply_trade_filter(rows: list[dict]) -> tuple[list[dict], dict]:
    market_data = {_normalize_ticker(item.get("ticker")): _trade_market_data(item) for item in rows or [] if _normalize_ticker(item.get("ticker"))}
    result = TRADE_FILTER.filter(rows or [], market_data)
    accepted = list(result.get("accepted") or [])
    accepted.sort(key=lambda item: (-float(item.get("final_score") or item.get("score") or 0.0), item.get("ticker") or ""))
    return accepted, dict(result)


def _apply_composition_filter(rows: list[dict], top_n: int = TOP_COUNT) -> tuple[list[dict], dict]:
    result = COMPOSITION_FILTER.filter_top_n(rows or [], top_n=top_n)
    accepted = list(result.get("accepted") or [])
    accepted.sort(key=lambda item: (-float(item.get("final_score") or item.get("score") or 0.0), item.get("ticker") or ""))
    return accepted, dict(result)


def _normalize_selection_metadata(item: dict) -> dict:
    normalized = dict(item or {})
    ticker = _normalize_ticker(normalized.get("ticker"))
    normalized["ticker"] = ticker
    normalized["leveraged_etf"] = bool(normalized.get("leveraged_etf")) or is_leveraged_or_inverse_etf(ticker)
    normalized["inverse_etf"] = bool(normalized.get("inverse_etf")) or is_inverse_etf(ticker)

    reject_reason = str(normalized.get("reject_reason") or "").strip()
    fallback_used = bool(normalized.get("fallback_used", False))
    if fallback_used and not reject_reason and not bool(normalized.get("trade_filter_passed", True)):
        reject_reason = str(normalized.get("fallback_reason") or "fallback_pool")
    if "trade_filter_passed" not in normalized:
        normalized["trade_filter_passed"] = True
    elif normalized.get("trade_filter_passed") is False and not reject_reason and not fallback_used:
        # A final TOP item without a rejection reason is an accepted candidate.
        normalized["trade_filter_passed"] = True
    normalized["reject_reason"] = reject_reason
    normalized["fallback_used"] = fallback_used
    normalized.setdefault("composition_filter_passed", True)
    normalized.setdefault("composition_reject_reason", "")
    return normalized


def _build_conservative_fallback_candidates(existing_symbols: set[str] | None = None) -> list[dict]:
    blocked = {str(item or "").strip().upper() for item in (existing_symbols or set()) if str(item or "").strip()}
    candidates: list[dict] = []
    for ticker in CONSERVATIVE_FALLBACK_POOL:
        ticker = _normalize_ticker(ticker)
        if not ticker or ticker in blocked:
            continue
        price = _live_candidate_price(ticker)
        if not price or price <= 0:
            continue
        row = {
            "ticker": ticker,
            "score": 55.0,
            "ai_score": 55.0,
            "range_score": 55.0,
            "final_score": 55.0,
            "confidence": 0.35,
            "reason": "conservative_fallback_pool",
            "source": "conservative_fallback_pool",
            "fallback_used": True,
            "fallback_reason": "top_n_not_filled",
            "ai_selected": False,
            "current_price": float(price),
            "range_low": round(float(price) * 0.96, 4),
            "range_high": round(float(price) * 1.04, 4),
            "risk": {"stop_loss_pct": 1.5},
            "size": 1,
            "trade_market_data": {
                "earnings_within_days": 999,
                "price_change_5d": 0.0,
                "avg_volume": 10_000_000,
                "bid_ask_spread_pct": 0.1,
                "regime": "NORMAL",
                "data_age_seconds": 0,
                "current_price": float(price),
                "close_history": [float(price)],
                "returns": [],
                "recent_low": round(float(price) * 0.96, 4),
                "recent_high": round(float(price) * 1.04, 4),
            },
        }
        candidates.append(row)
    return candidates


def _merged_selection_symbols(preferred_symbols: list[str] | None) -> list[str] | None:
    preferred = [
        _normalize_ticker(item)
        for item in (preferred_symbols or [])
        if _normalize_ticker(item)
    ]
    runtime_universe = [
        _normalize_ticker(item)
        for item in (load_runtime_config().universe or [])
        if _normalize_ticker(item)
    ]
    merged: list[str] = []
    seen: set[str] = set()
    for bucket in (preferred, runtime_universe):
        for symbol in bucket:
            if symbol in seen:
                continue
            merged.append(symbol)
            seen.add(symbol)
    return merged or None


def _live_candidate_price(ticker: str) -> float | None:
    symbol = _normalize_ticker(ticker)
    if not symbol:
        return None
    try:
        fetcher = PriceFetcher(symbol, poll_interval=0)
        quote = fetcher.get_quote()
        price = float(getattr(quote, "price", 0.0) or 0.0) if quote is not None else 0.0
        if price > 0:
            return price
        candles = fetcher.get_ohlcv(period="5d", interval="1d")
        closes = [float(getattr(c, "close", 0.0) or 0.0) for c in candles or [] if float(getattr(c, "close", 0.0) or 0.0) > 0]
        if closes:
            return closes[-1]
    except Exception:
        return None
    return None


def _candidate_price(item: dict) -> float | None:
    live_price = _live_candidate_price(str(item.get("ticker") or ""))
    if live_price and live_price > 0:
        return live_price
    for value in (
        item.get("current_price"),
        item.get("price_midpoint_hint"),
        ((item.get("metrics") or {}).get("last_close") if isinstance(item.get("metrics"), dict) else None),
    ):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    try:
        low = float(item.get("range_low") or 0.0)
        high = float(item.get("range_high") or 0.0)
    except (TypeError, ValueError):
        return None
    if low > 0 and high > low:
        return (low + high) / 2.0
    return None


def _enforce_price_band(
    candidates: list[dict],
    *,
    min_price: float,
    max_price: float,
) -> tuple[list[dict], list[str]]:
    filtered: list[dict] = []
    removed: list[str] = []
    for raw in candidates or []:
        item = dict(raw)
        ticker = _normalize_ticker(item.get("ticker"))
        price = _candidate_price(item)
        if price is None or price < min_price or price > max_price:
            if ticker:
                removed.append(ticker)
            continue
        item["current_price"] = round(price, 4)
        filtered.append(item)
    return filtered, removed


def _price_band_reject_item(item: dict, min_price: float, max_price: float) -> dict:
    ticker = _normalize_ticker(item.get("ticker"))
    price = _candidate_price(item)
    reason = "price_missing" if price is None else "price_out_of_range"
    rejected = {
        "ticker": ticker,
        "reason": reason,
        "price": round(float(price), 4) if price is not None else None,
        "min_price": float(min_price),
        "max_price": float(max_price),
        "allowed_range": f"${min_price:.2f}-${max_price:.2f}",
        "source": str(item.get("source") or ""),
    }
    return rejected


def _finalize_price_band(candidates: list[dict], min_price: float, max_price: float) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []
    for raw in candidates or []:
        item = dict(raw)
        price = _candidate_price(item)
        ticker = _normalize_ticker(item.get("ticker"))
        if price is None or price < min_price or price > max_price:
            if ticker:
                rejected.append(_price_band_reject_item(item, min_price, max_price))
            continue
        item["current_price"] = round(price, 4)
        accepted.append(item)
    return accepted, rejected


def _finalize_universe_filter(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    return filter_universe_candidates(candidates or [], rules=UNIVERSE_RULES)


def _price_rejections_from_universe(rows: list[dict]) -> list[dict]:
    result: list[dict] = []
    for row in rows or []:
        reasons = list(row.get("rejection_reason") or [])
        if "price_out_of_range" not in reasons and "price_missing" not in reasons:
            continue
        item = dict(row)
        allowed = item.get("allowed_price_range") if isinstance(item.get("allowed_price_range"), dict) else {}
        item["min_price"] = allowed.get("min")
        item["max_price"] = allowed.get("max")
        if item.get("min_price") is not None and item.get("max_price") is not None:
            item["allowed_range"] = f"${float(item['min_price']):.2f}-${float(item['max_price']):.2f}"
        result.append(item)
    return result


def _universe_settings_payload() -> dict:
    payload: dict[str, dict] = {}
    for asset_type, rule in UNIVERSE_RULES.items():
        payload[asset_type] = {
            "price_min": rule.price_min,
            "price_max": rule.price_max,
            "min_market_cap": rule.min_market_cap,
            "min_average_dollar_volume": rule.min_average_dollar_volume,
            "atr_20_pct_min": rule.atr_20_pct_min,
            "atr_20_pct_max": rule.atr_20_pct_max,
        }
    return payload


def _merge_rejected_rows(rows: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in rows or []:
        ticker = str(item.get("ticker") or "").strip().upper()
        reason = str(item.get("reason") or "").strip()
        price = str(item.get("price") if item.get("price") is not None else "")
        key = (ticker, reason, price)
        if not ticker or key in seen:
            continue
        merged.append(dict(item))
        seen.add(key)
    return merged


def _normalize_entry_report_fields(item: dict) -> dict:
    normalized = dict(item or {})
    entry = normalized.get("entry") if isinstance(normalized.get("entry"), dict) else {}
    entry_payload = {
        "entry_proximity_score": _coalesce_float(entry.get("entry_proximity_score"), normalized.get("entry_proximity_score"), default=50.0),
        "good_for_entry_now": bool(entry.get("good_for_entry_now", normalized.get("good_for_entry_now", False))),
        "entry_quality": str(entry.get("entry_quality") or normalized.get("entry_quality") or "unknown"),
        "entry_reason": str(entry.get("entry_reason") or normalized.get("entry_reason") or ""),
        "range_position": entry.get("range_position", normalized.get("range_position")),
        "dist_to_support": entry.get("dist_to_support", normalized.get("dist_to_support")),
        "dist_to_resistance": entry.get("dist_to_resistance", normalized.get("dist_to_resistance")),
    }
    normalized["entry"] = entry_payload
    normalized["entry_proximity_score"] = entry_payload["entry_proximity_score"]
    normalized["good_for_entry_now"] = entry_payload["good_for_entry_now"]
    normalized["entry_quality"] = entry_payload["entry_quality"]
    normalized["entry_reason"] = entry_payload["entry_reason"]
    normalized["range_position"] = entry_payload["range_position"]
    normalized["dist_to_support"] = entry_payload["dist_to_support"]
    normalized["dist_to_resistance"] = entry_payload["dist_to_resistance"]
    return normalized


def _warning_stage_for_selection(selection_stage: str) -> str:
    stage = str(selection_stage or "").strip().upper()
    if stage in {"FINALIZED", "PRELIMINARY", "REFINED"}:
        return stage
    if stage in {"QUALITY_REFINED", "QUALITY_BACKFILLED"}:
        return "REFINED"
    if stage in {"QUALITY_TIMED_OUT_BACKFILLED", "FAST_PRELIMINARY"}:
        return "PRELIMINARY"
    return "FINALIZED"


def _normalize_warning_record(
    warning: dict | str,
    *,
    stage: str,
    requested_count: int | None = None,
    selected_count: int | None = None,
    missing_count: int | None = None,
    symbols: list[str] | None = None,
    details: str = "",
) -> dict:
    if isinstance(warning, dict):
        record = dict(warning)
    else:
        text = str(warning or "").strip()
        code = text.split(":", 1)[0] if ":" in text else text or "warning"
        record = {
            "warning_code": code,
            "details": text,
        }
    record["warning_code"] = str(record.get("warning_code") or record.get("code") or "warning").strip()
    record["stage"] = str(record.get("stage") or stage or "FINALIZED").strip().upper()
    if requested_count is not None and record.get("requested_count") is None:
        record["requested_count"] = int(requested_count)
    if selected_count is not None and record.get("selected_count") is None:
        record["selected_count"] = int(selected_count)
    if missing_count is not None and record.get("missing_count") is None:
        record["missing_count"] = int(missing_count)
    if symbols is not None and record.get("symbols") is None:
        record["symbols"] = [str(item).strip().upper() for item in symbols if str(item).strip()]
    if details and not record.get("details"):
        record["details"] = details
    record.setdefault("details", "")
    record.setdefault("requested_count", requested_count if requested_count is not None else None)
    record.setdefault("selected_count", selected_count if selected_count is not None else None)
    record.setdefault("missing_count", missing_count if missing_count is not None else None)
    record.setdefault("symbols", [str(item).strip().upper() for item in symbols or [] if str(item).strip()])
    return record


def _dedupe_warning_records(records: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()
    for record in records or []:
        normalized = _normalize_warning_record(record, stage=record.get("stage") if isinstance(record, dict) else "FINALIZED")
        key = (
            normalized.get("warning_code"),
            normalized.get("stage"),
            normalized.get("requested_count"),
            normalized.get("selected_count"),
            normalized.get("missing_count"),
            tuple(normalized.get("symbols") or []),
            normalized.get("details"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(normalized)
    return merged


def _format_warning_record(record: dict) -> str:
    code = str(record.get("warning_code") or "warning").strip()
    stage = str(record.get("stage") or "FINALIZED").strip().upper()
    parts = [f"{code}", f"stage={stage}"]
    if record.get("requested_count") is not None:
        parts.append(f"requested={record.get('requested_count')}")
    if record.get("selected_count") is not None:
        parts.append(f"selected={record.get('selected_count')}")
    if record.get("missing_count") is not None:
        parts.append(f"missing={record.get('missing_count')}")
    if record.get("symbols"):
        parts.append(f"symbols={'/'.join(record.get('symbols') or [])}")
    details = str(record.get("details") or "").strip()
    if details:
        parts.append(details)
    return "; ".join(parts)


def _build_provider_audit_summary(provider_audit: dict[str, dict], provider_outputs: dict[str, dict]) -> dict:
    summary: dict[str, object] = {
        "provider_attempts": 0,
        "provider_successes": 0,
        "provider_failures": 0,
        "provider_timeouts": 0,
        "provider_empty_responses": 0,
        "provider_fallbacks": 0,
        "provider_mocks": 0,
        "provider_contributors": [],
        "records": [],
    }
    contributors: set[str] = set()
    for provider_name, record in sorted((provider_audit or {}).items()):
        provider_record = dict(record or {})
        provider_record.setdefault("provider_name", provider_name)
        provider_outputs_for_provider = dict(provider_outputs.get(provider_name) or {})
        provider_record["attempted"] = int(provider_record.get("attempted", len(provider_outputs_for_provider) or 0) or 0)
        provider_record["success"] = int(provider_record.get("success", 0) or 0)
        provider_record["failure"] = int(provider_record.get("failure", max(0, provider_record["attempted"] - provider_record["success"])) or 0)
        provider_record["timed_out"] = int(provider_record.get("timed_out", 0) or 0)
        provider_record["empty_response"] = int(provider_record.get("empty_response", 0) or 0)
        provider_record["fallback_used"] = int(provider_record.get("fallback_used", 0) or 0)
        provider_record["mock_used"] = int(provider_record.get("mock_used", 0) or 0)
        provider_record["contributed_fields"] = list(provider_record.get("contributed_fields") or [])
        summary["provider_attempts"] += provider_record["attempted"]
        summary["provider_successes"] += provider_record["success"]
        summary["provider_failures"] += provider_record["failure"]
        summary["provider_timeouts"] += provider_record["timed_out"]
        summary["provider_empty_responses"] += provider_record["empty_response"]
        summary["provider_fallbacks"] += provider_record["fallback_used"]
        summary["provider_mocks"] += provider_record["mock_used"]
        contributors.update(provider_record["contributed_fields"])
        summary["records"].append(provider_record)
    summary["provider_contributors"] = sorted(contributors)
    return summary


def _selection_outcome(summary: dict, *, provider_audit: dict[str, dict] | None = None) -> dict[str, object]:
    top_items = list(summary.get("top3") or summary.get("top5") or [])
    top_count = int(summary.get("target_top_n") or 3)
    selected_count = int(summary.get("selection_count") or len(top_items) or 0)
    missing_count = max(0, top_count - selected_count)
    provider_outputs = dict(summary.get("provider_outputs") or {})
    warnings = _dedupe_warning_records(
        [
            *list((summary.get("quality_filter_report") or {}).get("warning_records") or []),
            *list((summary.get("quality_filter_report") or {}).get("warnings_structured") or []),
            *list((summary.get("composition_filter") or {}).get("warning_records") or []),
            *list((summary.get("composition_filter") or {}).get("warnings_structured") or []),
        ]
    )
    top_n_warning = None
    if missing_count > 0:
        top_n_warning = _normalize_warning_record(
            {
                "warning_code": "top_n_not_filled",
                "stage": "FINALIZED",
                "requested_count": top_count,
                "selected_count": selected_count,
                "missing_count": missing_count,
                "symbols": [str(item.get("ticker") or "").upper() for item in top_items if str(item.get("ticker") or "").strip()],
                "details": "final TOP still below requested count",
            },
            stage="FINALIZED",
            requested_count=top_count,
            selected_count=selected_count,
            missing_count=missing_count,
            symbols=[str(item.get("ticker") or "").upper() for item in top_items if str(item.get("ticker") or "").strip()],
            details="final TOP still below requested count",
        )
        warnings = [item for item in warnings if item.get("warning_code") != "top_n_not_filled"]
        warnings.append(top_n_warning)

    provider_audit_summary = _build_provider_audit_summary(provider_audit or {}, provider_outputs)
    fallback_used = bool(summary.get("fallback_used", False)) or bool(summary.get("provider_fallback_used", False))
    mock_used = bool(provider_audit_summary.get("provider_mocks", 0))
    timed_out = bool((summary.get("quality_filter_report") or {}).get("timed_out", False))
    invalid_candidates = []
    degraded_reasons: set[str] = set()
    for item in top_items:
        candidate = dict(item or {})
        candidate_fallback = bool(candidate.get("fallback_used") or candidate.get("quality_backfill") or candidate.get("fallback_history_incomplete") or candidate.get("data_status") in {"STALE", "INVALID"} or candidate.get("scoring_eligible") is False)
        fallback_sources: list[str] = []
        mock_sources: list[str] = []
        ticker = _normalize_ticker(candidate.get("ticker"))
        for provider_name, provider_rows in (provider_outputs := dict(summary.get("provider_outputs") or {})).items():
            provider_row = dict(provider_rows.get(ticker) or {}) if isinstance(provider_rows, dict) else {}
            if not provider_row:
                continue
            text = " ".join(str(provider_row.get(key) or "") for key in ("reason", "source", "error_message", "error_code")).lower()
            is_mock = "mock" in text or str(provider_row.get("source") or "").lower().endswith("_mock")
            is_fallback = bool(provider_row.get("fallback")) or is_mock
            if is_fallback:
                fallback_sources.append(provider_name)
                candidate_fallback = True
            if is_mock:
                mock_sources.append(provider_name)
        candidate["candidate_fallback"] = bool(candidate_fallback)
        candidate["fallback_sources"] = sorted(set(fallback_sources))
        candidate["mock_used"] = bool(mock_sources)
        candidate["mock_sources"] = sorted(set(mock_sources))
        candidate["degraded"] = bool(candidate["candidate_fallback"] or candidate["mock_used"] or candidate.get("data_status") in {"STALE", "INVALID"})
        if candidate["candidate_fallback"]:
            degraded_reasons.add("fallback_used")
        if candidate["mock_used"]:
            degraded_reasons.add("mock_used")
        if candidate.get("data_status") == "STALE":
            degraded_reasons.add("stale_data")
        if candidate.get("data_status") == "INVALID":
            invalid_candidates.append(ticker)
            degraded_reasons.add("invalid_data")
        candidate["degradation_reasons"] = sorted(
            set(
                [
                    *(["fallback_used"] if candidate["candidate_fallback"] else []),
                    *(["mock_used"] if candidate["mock_used"] else []),
                    *(["stale_data"] if candidate.get("data_status") == "STALE" else []),
                    *(["invalid_data"] if candidate.get("data_status") == "INVALID" else []),
                ]
            )
        )
        item.update(candidate)
    result_quality = "COMPLETE"
    if invalid_candidates:
        result_quality = "INVALID"
    elif fallback_used or mock_used or timed_out or missing_count > 0 or bool(degraded_reasons):
        result_quality = "DEGRADED"
    research_admission = "RESEARCH_READY"
    if result_quality == "DEGRADED":
        research_admission = "RESEARCH_ONLY"
    elif result_quality == "INVALID":
        research_admission = "BLOCKED"
    execution_status = "COMPLETED"
    if invalid_candidates and not top_items:
        execution_status = "FAILED"
    return {
        "execution_status": execution_status,
        "result_quality": result_quality,
        "research_admission": research_admission,
        "selected_top_n": selected_count,
        "requested_top_n": top_count,
        "top_n_complete": selected_count >= top_count,
        "top_n_missing_count": missing_count,
        "top_n_shortfall_reason": "top_n_not_filled" if missing_count > 0 else "",
        "top_n_warning": top_n_warning,
        "warnings_structured": warnings,
        "warnings": [_format_warning_record(item) for item in warnings],
        "provider_audit": provider_audit_summary,
        "provider_outputs": provider_outputs,
        "fallback_used": fallback_used,
        "mock_used": mock_used,
        "degraded": result_quality != "COMPLETE",
        "degradation_reasons": sorted(degraded_reasons),
        "invalid_candidates": invalid_candidates,
    }


def _enrich_selection_rows(
    rows: list[dict],
    *,
    provider_outputs: dict[str, dict] | None = None,
) -> list[dict]:
    enriched: list[dict] = []
    provider_outputs = dict(provider_outputs or {})
    for raw in rows or []:
        item = dict(raw or {})
        ticker = _normalize_ticker(item.get("ticker"))
        candidate_fallback = bool(
            item.get("fallback_used")
            or item.get("quality_backfill")
            or item.get("fallback_history_incomplete")
            or item.get("data_status") in {"STALE", "INVALID"}
            or item.get("scoring_eligible") is False
        )
        fallback_sources: list[str] = []
        mock_sources: list[str] = []
        for provider_name, provider_rows in provider_outputs.items():
            provider_row = dict(provider_rows.get(ticker) or {}) if isinstance(provider_rows, dict) else {}
            if not provider_row:
                continue
            text = " ".join(
                str(provider_row.get(key) or "")
                for key in ("reason", "source", "error_message", "error_code")
            ).lower()
            is_mock = "mock" in text or str(provider_row.get("source") or "").lower().endswith("_mock")
            is_fallback = bool(provider_row.get("fallback")) or is_mock
            if is_fallback:
                fallback_sources.append(provider_name)
                candidate_fallback = True
            if is_mock:
                mock_sources.append(provider_name)
        item["candidate_fallback"] = bool(candidate_fallback)
        item["fallback_sources"] = sorted(set(fallback_sources))
        item["mock_used"] = bool(mock_sources)
        item["mock_sources"] = sorted(set(mock_sources))
        item["degraded"] = bool(item["candidate_fallback"] or item["mock_used"] or item.get("data_status") in {"STALE", "INVALID"})
        item["degradation_reasons"] = sorted(
            set(
                [
                    *(["fallback_used"] if item["candidate_fallback"] else []),
                    *(["mock_used"] if item["mock_used"] else []),
                    *(["stale_data"] if item.get("data_status") == "STALE" else []),
                    *(["invalid_data"] if item.get("data_status") == "INVALID" else []),
                ]
            )
        )
        item["current_validation_status"] = str(item.get("validation_status") or item.get("current_validation_status") or "AI_CANDIDATE")
        item["trade_admission_status"] = "NOT_TRADABLE"
        if item["current_validation_status"] in {"PAPER_ELIGIBLE", "LIVE_ELIGIBLE"}:
            item["trade_admission_status"] = item["current_validation_status"]
        enriched.append(item)
    return enriched


def _filter_entry_quality(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []
    for raw in candidates or []:
        item = _normalize_entry_report_fields(dict(raw))
        quality = str(item.get("entry_quality") or "unknown").strip().lower()
        if quality in {"poor", "very_poor"}:
            rejected.append(
                {
                    "ticker": _normalize_ticker(item.get("ticker")),
                    "reason": "entry_quality_too_low",
                    "entry_quality": quality,
                    "good_for_entry_now": bool(item.get("good_for_entry_now", False)),
                    "entry_reason": str(item.get("entry_reason") or ""),
                    "entry_proximity_score": _coalesce_float(item.get("entry_proximity_score"), default=50.0),
                    "range_position": item.get("range_position"),
                    "dist_to_support": item.get("dist_to_support"),
                    "dist_to_resistance": item.get("dist_to_resistance"),
                    "source": str(item.get("source") or ""),
                }
            )
            continue
        accepted.append(item)
    return accepted, rejected


def _build_report_top10(
    selector_top10: list[dict],
    selected: list[dict],
    signal_map: dict[str, dict],
    live_positions: list[dict] | None,
) -> list[dict]:
    candidates = []
    for source_rows in (selector_top10 or [], selected or []):
        for item in source_rows:
            candidates.append(dict(item))
    if not candidates:
        candidates = list(selected or [])
    candidates = _merge_live_position_flags(candidates, live_positions or [])
    candidates = _annotate_with_ai_signals(candidates, signal_map or {})
    if not candidates:
        fallback_rows = []
        for item in selected or []:
            row = dict(item)
            row.setdefault("selection_penalty_reason", "top10_backfilled_from_selected")
            fallback_rows.append(row)
        candidates = fallback_rows
    deduped: list[dict] = []
    seen: set[str] = set()
    for raw in candidates:
        ticker = _normalize_ticker(raw.get("ticker"))
        if not ticker or ticker in seen:
            continue
        item = dict(raw)
        item["ticker"] = ticker
        deduped.append(item)
        seen.add(ticker)
    return deduped


def _prioritize_ai_rank(rows: list[dict], signal_map: dict[str, dict]) -> list[dict]:
    def _sort_key(item: dict):
        ticker = _normalize_ticker(item.get("ticker"))
        signal = signal_map.get(ticker) or {}
        ai_score = float(signal.get("final_score") or signal.get("ai_score") or signal.get("score") or -1.0)
        base_score = float(item.get("score") or 0.0)
        return (-ai_score, -base_score, ticker)

    return sorted((dict(item) for item in rows or []), key=_sort_key)


def _write_reports(summary: dict) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    latest_json = REPORTS_DIR / "ai_selection_latest.json"
    dated_json = REPORTS_DIR / f"ai_selection_{_et_now().strftime('%Y%m%d')}.json"
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    latest_json.write_text(payload, encoding="utf-8")
    dated_json.write_text(payload, encoding="utf-8")
    return latest_json, dated_json


def _merge_live_position_flags(items: list[dict], positions: list[dict]) -> list[dict]:
    live_map = {
        str(pos.get("ticker") or "").strip().upper(): dict(pos)
        for pos in (positions or [])
    }
    merged = []
    for raw in items or []:
        item = dict(raw)
        ticker = str(item.get("ticker") or "").strip().upper()
        live_pos = live_map.get(ticker)
        if live_pos:
            item["existing_position"] = True
            item["live_quantity"] = int(live_pos.get("quantity") or 0)
            item["protected_position"] = True
            if ticker == "SOXS":
                item["reduce_only"] = True
        merged.append(item)
    return merged


def _restart_top_engines() -> int:
    if os.environ.get("AI_SELECTOR_RESTART_TOP", "1") == "0":
        print("AI_SELECTOR_RESTART_TOP=0; skipping TOP engine restart.")
        return 0
    multi_launch = PROJECT_DIR / "multi_launch.sh"
    if not multi_launch.exists():
        print(f"Missing launcher: {multi_launch}")
        return 1
    return subprocess.run(
        ["/bin/bash", str(multi_launch), "restart-top"],
        cwd=PROJECT_DIR,
        check=False,
    ).returncode


def _spawn_background_refinement(expected_timestamp: str) -> None:
    if os.environ.get("AI_SELECTOR_BACKGROUND_REFINEMENT", "1") != "1":
        return
    refine_script = PROJECT_DIR / "scripts" / "refine_ai_selection_report.py"
    if not refine_script.exists():
        return
    env = os.environ.copy()
    env.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    env.setdefault("AI_SELECTOR_ALLOW_PROXY_MARKET", "0")
    env.setdefault("AI_SELECTOR_DIRECT_HISTORY", "1")
    env.setdefault("AI_SELECTOR_SKIP_YFINANCE_HISTORY", "0")
    env.setdefault("AI_SELECTOR_HTTP_TIMEOUT_SECONDS", "2")
    env.setdefault("AI_SELECTOR_FILTER_CANDIDATE_LIMIT", "20")
    env.setdefault("AI_SELECTOR_TOTAL_BUDGET_SECONDS", "30")
    env.setdefault("AI_SELECTOR_QUALITY_BUDGET_SECONDS", "20")
    env["AI_SELECTOR_EXPECTED_TIMESTAMP"] = expected_timestamp
    env["AI_SELECTOR_REFINEMENT_ONLY"] = "1"
    with open(PROJECT_DIR / "logs" / "ai_selector_refine.out.log", "a", encoding="utf-8") as out, open(
        PROJECT_DIR / "logs" / "ai_selector_refine.err.log",
        "a",
        encoding="utf-8",
    ) as err:
        subprocess.Popen(
            [sys.executable, str(refine_script)],
            cwd=PROJECT_DIR,
            stdout=out,
            stderr=err,
            env=env,
            start_new_session=True,
        )


def _live_equity_positions() -> list[dict] | None:
    """Return current long equity positions; options are managed outside Top3 stock slots."""
    try:
        from src.dashboard.combined import _fetch_live_account_summary

        account = _fetch_live_account_summary()
    except Exception as exc:
        print(f"Could not verify live positions: {exc}")
        return None
    if not isinstance(account, dict) or account.get("data_stale"):
        print("Could not verify live positions; existing TOP configs will be preserved.")
        return None
    positions = []
    for pos in account.get("positions") or []:
        ticker = str(pos.get("ticker") or "").strip().upper().removesuffix(".US")
        quantity = int(pos.get("quantity") or 0)
        price = float(pos.get("current_price") or pos.get("avg_entry_price") or 0.0)
        if quantity <= 0 or price <= 0 or not EQUITY_SYMBOL_RE.fullmatch(ticker):
            continue
        positions.append({"ticker": ticker, "quantity": quantity, "current_price": price})
    return positions


def _pin_live_positions(selected: list[dict], positions: list[dict], limit: int = TOP_COUNT) -> list[dict]:
    """Reserve TOP slots for real equity holdings so exits remain managed."""
    selected_by_ticker = {
        str(item.get("ticker") or "").upper(): dict(item) for item in selected
    }
    pinned = []
    pinned_tickers = set()
    for position in positions:
        ticker = str(position.get("ticker") or "").upper()
        if (
            not ticker
            or ticker in pinned_tickers
            or not EQUITY_SYMBOL_RE.fullmatch(ticker)
        ):
            continue
        item = selected_by_ticker.get(ticker)
        if item is None:
            price = float(position.get("current_price") or 0.0)
            if price <= 0:
                continue
            item = {
                "ticker": ticker,
                "score": 0.0,
                "range_low": price * 0.95,
                "range_high": price * 1.05,
                "risk": {"stop_loss_pct": 1.5},
                "size": int(position.get("quantity") or 1),
                "selection_penalty_reason": "live position protection",
            }
            item["ai_selected"] = False
            item["reduce_only"] = True
        else:
            item["ai_selected"] = True
            item["reduce_only"] = bool(item.get("reduce_only", False))
        item["existing_position"] = True
        item["protected_position"] = True
        if ticker == "SOXS":
            item["reduce_only"] = True
        item["pinned_live_position"] = True
        pinned.append(item)
        pinned_tickers.add(ticker)

    remaining = [
        dict(item)
        for item in selected
        if str(item.get("ticker") or "").upper() not in pinned_tickers
    ]
    return (pinned + remaining)[:limit]


def _split_selected_and_protected_positions(
    candidates: list[dict],
    positions: list[dict],
    limit: int = TOP_COUNT,
) -> tuple[list[dict], list[dict]]:
    protected_map: dict[str, dict] = {}
    for position in positions or []:
        ticker = str(position.get("ticker") or "").strip().upper()
        if not ticker or not EQUITY_SYMBOL_RE.fullmatch(ticker):
            continue
        current_price = float(position.get("current_price") or 0.0)
        if current_price <= 0:
            continue
        protected_map[ticker] = {
            "ticker": ticker,
            "score": 0.0,
            "range_low": current_price * 0.95,
            "range_high": current_price * 1.05,
            "risk": {"stop_loss_pct": 1.5},
            "size": int(position.get("quantity") or 1),
            "selection_penalty_reason": "live position protection",
            "existing_position": True,
            "protected_position": True,
            "reduce_only": ticker == "SOXS",
            "pinned_live_position": True,
        }

    tradable: list[dict] = []
    seen_tradable: set[str] = set()
    for raw in candidates or []:
        item = dict(raw)
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen_tradable:
            continue
        if ticker in protected_map:
            protected = dict(protected_map[ticker])
            protected.update(
                {
                    "score": float(item.get("score") or protected.get("score") or 0.0),
                    "confidence": float(item.get("confidence") or protected.get("confidence") or 0.0),
                    "reason": str(
                        item.get("reason")
                        or item.get("selection_penalty_reason")
                        or protected.get("selection_penalty_reason")
                        or "live position protection"
                    ),
                    "source": str(item.get("source") or protected.get("source") or "ai_selector"),
                    "ai_selected": True,
                }
            )
            if isinstance(item.get("entry"), dict):
                protected["entry"] = dict(item.get("entry") or {})
            protected["reduce_only"] = True if ticker == "SOXS" else bool(protected.get("reduce_only", False))
            protected_map[ticker] = protected
            continue
        tradable.append(item)
        seen_tradable.add(ticker)
        if len(tradable) >= limit:
            break

    protected_positions = list(protected_map.values())
    protected_positions.sort(key=lambda item: (item.get("ticker") or ""))
    return tradable[:limit], protected_positions

def main():
    load_local_ai_env()
    runtime_settings = load_runtime_settings()
    min_price, max_price = resolve_price_band(runtime_settings)
    universe_price_min = min(rule.price_min for rule in UNIVERSE_RULES.values())
    universe_price_max = max(rule.price_max for rule in UNIVERSE_RULES.values())
    market_context = market_session_context(_et_now())
    os.environ.setdefault("AI_SELECTOR_MIN_PRICE", str(min_price))
    os.environ.setdefault("AI_SELECTOR_MAX_PRICE", str(max_price))
    os.environ.setdefault(
        "AI_SELECTOR_AUTO_REFRESH_MINUTES",
        str(runtime_settings.get("auto_refresh_minutes", 5)),
    )
    configured_max_symbols = int(runtime_settings.get("max_symbols", 20) or 20)
    os.environ.setdefault("AI_SELECTOR_MAX_SYMBOLS", str(max(5, min(configured_max_symbols, 20))))
    os.environ.setdefault("AI_SELECTOR_ALLOW_PROXY_MARKET", "0")
    os.environ.setdefault("AI_SELECTOR_DIRECT_HISTORY", "1")
    os.environ.setdefault("AI_SELECTOR_SKIP_YFINANCE_HISTORY", "0")
    os.environ.setdefault("AI_SELECTOR_HTTP_TIMEOUT_SECONDS", "3")
    live_positions = _live_equity_positions()
    if live_positions is None and _has_live_top_configs():
        print("Live position verification failed; refusing to run selection or replace TOP configs.")
        sys.exit(1)

    integrated_ai = _run_integrated_ai_selector()
    preferred_symbols = integrated_ai.get("preferred_symbols") or None
    selection_symbols = _merged_selection_symbols(preferred_symbols)
    sel = AIStrategySelector()
    out = sel.run_selection(write_configs=False, symbols_override=selection_symbols)
    selected = out.get('top5') or out.get('top3') or []
    if not selected and selection_symbols:
        integrated_ai["fallback_used"] = True
        out = sel.run_selection(write_configs=False)
        selected = out.get('top5') or out.get('top3') or []

    selected = _annotate_with_ai_signals(list(selected or []), integrated_ai.get("signal_map") or {})
    selected = [_normalize_selection_metadata(item) for item in selected]
    selected = _apply_range_scores(selected)
    price_band_rejected_rows: list[dict] = []
    report_top10 = _apply_range_scores(_build_report_top10(
        list(out.get("top10") or []),
        list(selected),
        integrated_ai.get("signal_map") or {},
        live_positions or [],
    ))
    report_top10, report_universe_rejected = _finalize_universe_filter(report_top10)
    universe_rejected_rows: list[dict] = list(report_universe_rejected)
    price_band_rejected_rows.extend(_price_rejections_from_universe(report_universe_rejected))
    candidate_pool = _annotate_with_ai_signals(list(report_top10 or []), integrated_ai.get("signal_map") or {})
    if integrated_ai.get("preferred_symbols"):
        candidate_pool = _prioritize_ai_rank(candidate_pool, integrated_ai.get("signal_map") or {})
    selected, protected_positions = _split_selected_and_protected_positions(
        candidate_pool,
        live_positions or [],
        limit=min(sel.selection_size, TOP_COUNT),
    )
    selection_stage = str((out.get("settings") or {}).get("selection_stage") or "")
    min_price, max_price = universe_price_min, universe_price_max
    fallback_pool_used = False
    trade_filter_report: dict = {"rejected": [], "fallback_used": False}
    fallback_trade_report: dict = {"rejected": [], "fallback_used": False}
    composition_filter_report: dict = {"rejected": [], "warnings": []}

    selected, initial_universe_rejected = _finalize_universe_filter(selected)
    universe_rejected_rows.extend(initial_universe_rejected)
    price_band_rejected_rows.extend(_price_rejections_from_universe(initial_universe_rejected))
    selected = _apply_range_scores(_annotate_with_ai_signals(selected, integrated_ai.get("signal_map") or {}))
    selected = [_normalize_selection_metadata(item) for item in selected]

    if selection_stage != "fast_preliminary":
        selected, trade_filter_report = _apply_trade_filter(selected)
        selected, composition_filter_report = _apply_composition_filter(selected, top_n=TOP_COUNT)
        allow_conservative_fallback = True
        if 0 < len(selected) < TOP_COUNT and allow_conservative_fallback:
            blocked_symbols = {
                str(item.get("ticker") or "").strip().upper()
                for item in list(selected) + list(protected_positions)
                if str(item.get("ticker") or "").strip()
            }
            fallback_candidates = _build_conservative_fallback_candidates(blocked_symbols)
            if fallback_candidates:
                fallback_pool_used = True
                fallback_candidates = _apply_range_scores(fallback_candidates)
                fallback_candidates, fallback_trade_report = _apply_trade_filter(fallback_candidates)
                fallback_candidates, fallback_universe_rejected = _finalize_universe_filter(fallback_candidates)
                universe_rejected_rows.extend(fallback_universe_rejected)
                price_band_rejected_rows.extend(_price_rejections_from_universe(fallback_universe_rejected))
                fallback_trade_report["rejected"] = list(fallback_trade_report.get("rejected") or [])
                fallback_candidates, fallback_composition_report = _apply_composition_filter(fallback_candidates, top_n=TOP_COUNT)
                composition_filter_report.setdefault("rejected", [])
                composition_filter_report.setdefault("warnings", [])
                composition_filter_report["rejected"].extend(list(fallback_composition_report.get("rejected") or []))
                composition_filter_report["warnings"].extend(list(fallback_composition_report.get("warnings") or []))
                existing_tickers = {
                    str(item.get("ticker") or "").strip().upper()
                    for item in selected
                    if str(item.get("ticker") or "").strip()
                }
                for item in fallback_candidates:
                    ticker = str(item.get("ticker") or "").strip().upper()
                    if not ticker or ticker in existing_tickers or len(selected) >= TOP_COUNT:
                        continue
                    item_price = _candidate_price(item)
                    item["current_price"] = round(float(item_price), 4)
                    item["fallback_used"] = True
                    item["fallback_reason"] = "top_n_not_filled"
                    item["source"] = "conservative_fallback_pool"
                    item["selection_penalty_reason"] = "conservative_fallback_pool"
                    item["trade_filter_passed"] = bool(item.get("trade_filter_passed", True))
                    item["reject_reason"] = str(item.get("reject_reason") or "")
                    item["composition_filter_passed"] = bool(item.get("composition_filter_passed", True))
                    item["composition_reject_reason"] = str(item.get("composition_reject_reason") or "")
                    item["final_rank"] = len(selected) + 1
                    selected.append(_normalize_selection_metadata(item))
                    existing_tickers.add(ticker)
                selected.sort(key=lambda item: (-float(item.get("final_score") or item.get("score") or 0.0), item.get("ticker") or ""))
                for idx, item in enumerate(selected, start=1):
                    item["final_rank"] = idx
                if len(selected) < TOP_COUNT and not any(
                    str(warning).startswith("top_n_not_filled")
                    for warning in composition_filter_report.get("warnings") or []
                ):
                    composition_filter_report.setdefault("warnings", [])
                    composition_filter_report["warnings"].append(f"top_n_not_filled:{len(selected)}/{TOP_COUNT}")
            else:
                composition_filter_report.setdefault("warnings", [])
                composition_filter_report["warnings"].append(f"top_n_not_filled:{len(selected)}/{TOP_COUNT}")

    selected, final_composition_report = _apply_composition_filter(selected, top_n=TOP_COUNT)
    composition_filter_report.setdefault("rejected", [])
    composition_filter_report.setdefault("warnings", [])
    composition_filter_report["rejected"].extend(list(final_composition_report.get("rejected") or []))
    composition_filter_report["warnings"].extend(list(final_composition_report.get("warnings") or []))

    selected, final_universe_rejected = _finalize_universe_filter(selected)
    universe_rejected_rows.extend(final_universe_rejected)
    price_band_rejected_rows.extend(_price_rejections_from_universe(final_universe_rejected))
    selected = [_normalize_entry_report_fields(_normalize_selection_metadata(item)) for item in selected]
    selected, low_quality_rejected = _filter_entry_quality(selected)
    report_top10 = [_normalize_entry_report_fields(item) for item in report_top10]
    preserved_positions = [
        str(item.get("ticker") or "").upper()
        for item in protected_positions
    ]
    quality_report = dict(out.get("quality_filter_report") or {})
    quality_report["final_selected_symbols"] = [
        str(item.get("ticker") or "").upper() for item in selected
    ]
    quality_report["existing_real_positions_preserved"] = preserved_positions
    quality_report["removed_out_of_price_band"] = _merge_rejected_rows(price_band_rejected_rows)
    quality_report["removed_by_universe_filter"] = _merge_rejected_rows(universe_rejected_rows)
    quality_report["trade_filter_passed"] = [bool(item.get("trade_filter_passed", False)) for item in selected]
    quality_report["reject_reason"] = [str(item.get("reject_reason") or "") for item in selected]
    trade_filter_rejected = list(trade_filter_report.get("rejected") or [])
    trade_filter_rejected.extend(list(fallback_trade_report.get("rejected") or []) if fallback_pool_used else [])
    quality_report["trade_filter_rejected"] = trade_filter_rejected
    quality_report["fallback_used"] = bool(trade_filter_report.get("fallback_used", False)) or bool(integrated_ai.get("fallback_used")) or bool(fallback_pool_used)
    quality_report["fallback_pool_used"] = bool(fallback_pool_used)
    quality_report["removed_low_entry_quality"] = low_quality_rejected
    quality_report["entry_quality_threshold"] = ["excellent", "good", "neutral"]
    quality_report["composition_filter"] = {
        "max_leveraged_etf_in_top3": 1,
        "rejected": list(composition_filter_report.get("rejected") or []),
        "warnings": list(composition_filter_report.get("warnings") or []),
    }
    quality_report["selection_count"] = len(selected)
    quality_report["target_top_n"] = TOP_COUNT
    quality_report["top_n_filled"] = len(selected) >= TOP_COUNT
    quality_report["missing_slots"] = max(0, TOP_COUNT - len(selected))
    quality_report["disabled_configs"] = [
        f"TOP{i}.yaml" for i in range(len(selected) + 1, TOP_COUNT + 1)
    ]
    out["quality_filter_report"] = quality_report
    out["top10"] = list(report_top10)
    out["settings"] = dict(out.get("settings") or {})
    out["settings"]["min_price"] = float(universe_price_min)
    out["settings"]["max_price"] = float(universe_price_max)
    out["settings"]["price_band"] = {"min": float(universe_price_min), "max": float(universe_price_max)}
    out["settings"]["universe_filter"] = _universe_settings_payload()
    out["settings"]["selection_stage"] = selection_stage
    out["settings"]["entry_proximity_enabled"] = bool(ENTRY_PROXIMITY_ENABLED)
    out["settings"]["entry_proximity_weight"] = float(ENTRY_PROXIMITY_WEIGHT)
    write_selection_filter_log(quality_report)
    summary_top3_source = list(selected or report_top10[:TOP_COUNT])
    market_stage = str(
        summary_top3_source[0].get("selection_stage")
        if summary_top3_source and isinstance(summary_top3_source[0], dict) and summary_top3_source[0].get("selection_stage")
        else (out.get("settings") or {}).get("selection_stage")
        or market_context.to_dict().get("session_label", "PRELIMINARY")
    ).strip().upper()
    if market_stage == "FAST_PRELIMINARY":
        market_stage = "PRELIMINARY"
    elif market_stage in {"QUALITY_REFINED", "QUALITY_BACKFILLED", "QUALITY_TIMED_OUT_BACKFILLED"}:
        market_stage = "FINALIZED"
    if market_stage not in {"PRELIMINARY", "PREMARKET_REFRESHED", "FINALIZED", "STALE", "INVALID"}:
        market_stage = "PRELIMINARY"
    if selected and market_stage == "FINALIZED":
        from src.ai_selector.config_writer import write_top_configs
        for item in selected:
            item["selection_date"] = market_context.current_session.isoformat()
            item["protected_position"] = bool(item.get("protected_position") or item.get("existing_position"))
            item.update(_normalize_selection_metadata(item))
            item["fallback_used"] = bool(item.get("fallback_used", False))
            item["composition_filter_passed"] = bool(item.get("composition_filter_passed", True))
            item["composition_reject_reason"] = str(item.get("composition_reject_reason") or "")
            item["final_rank"] = int(item.get("final_rank") or 0)
        write_top_configs(selected)
        selected = list(selected[:TOP_COUNT])
        for idx, item in enumerate(selected, start=1):
            item["final_rank"] = idx
        out["top5"] = list(selected)
        out["top3"] = list(selected)
        formatter = getattr(sel, "_format_report_rows", None)
        if callable(formatter):
            out["report"] = formatter(selected)
        else:
            out["report"] = [
                {"rank": idx, "ticker": row.get("ticker"), "score": row.get("score")}
                for idx, row in enumerate(selected, start=1)
            ]
    else:
        out["top5"] = list(summary_top3_source)
        out["top3"] = list(summary_top3_source)
        formatter = getattr(sel, "_format_report_rows", None)
        if callable(formatter):
            out["report"] = formatter(summary_top3_source)
        else:
            out["report"] = [
                {"rank": idx, "ticker": row.get("ticker"), "score": row.get("score")}
                for idx, row in enumerate(summary_top3_source, start=1)
            ]
        import src.ai_selector.config_writer as config_writer_module
        clear_top_configs = getattr(config_writer_module, "clear_top_configs", None)
        if callable(clear_top_configs):
            clear_top_configs()
    timestamp = datetime.now().isoformat()
    print(f"AI selection completed at {timestamp}")
    print("Top10:")
    for i, t in enumerate(out['top10'], start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")
    print("Top3:")
    for i, t in enumerate(selected, start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")

    providers_used, providers_disabled, fmp_enabled = _provider_metadata(out, live_positions, integrated_ai)
    report_fallback_used = bool(integrated_ai.get("fallback_used")) or bool(fallback_pool_used) or any(
        bool(item.get("fallback_history_incomplete"))
        or str(item.get("selection_penalty_reason") or "").startswith("quality_filter_backfill")
        or bool(item.get("fallback_used"))
        for item in selected
    ) or bool(trade_filter_report.get("fallback_used", False))
    out["settings"]["fallback_used"] = report_fallback_used

    top3_summary = [_normalize_entry_report_fields(item) for item in list(selected or summary_top3_source)]
    first_item = top3_summary[0] if top3_summary else {}
    current_session = market_context.current_session.isoformat()
    summary = {
        'timestamp': timestamp,
        'generated_at': timestamp,
        'selection_date': current_session,
        'selection_stage': market_stage,
        'market_context': market_context.to_dict(),
        'providers_used': providers_used,
        'providers_disabled': providers_disabled,
        'fmp_enabled': fmp_enabled,
        'provider_fallback_used': bool(integrated_ai.get("provider_fallback_used", False)),
        'top10': out.get('top10', []),
        'top5': list(selected),
        'top3': top3_summary,
        'selection_count': len(selected),
        'candidate_count': len(top3_summary),
        'target_top_n': TOP_COUNT,
        'top_n_filled': len(selected) >= TOP_COUNT,
        'missing_slots': max(0, TOP_COUNT - len(selected)),
        'fallback_pool_used': bool(fallback_pool_used),
        'disabled_configs': [
            f"TOP{i}.yaml" for i in range(len(selected) + 1, TOP_COUNT + 1)
        ],
        'protected_positions': [
            {
                "ticker": str(item.get("ticker") or "").upper(),
                "range_low": item.get("range_low"),
                "range_high": item.get("range_high"),
                "current_price": item.get("current_price"),
                "score": item.get("score"),
                "confidence": item.get("confidence"),
                "reason": item.get("reason"),
                "source": item.get("source"),
                "protected_position": True,
                "reduce_only": bool(item.get("reduce_only", False)),
            }
            for item in protected_positions
        ],
        'fallback_used': report_fallback_used,
        'report': out.get('report', []),
        'settings': out.get('settings', {}),
        'quality_filter_report': out.get('quality_filter_report', {}),
        'composition_filter': {
            'max_leveraged_etf_in_top3': 1,
            'rejected': list(composition_filter_report.get("rejected") or []),
            'warnings': list(composition_filter_report.get("warnings") or []),
        },
        'last_completed_session': market_context.previous_completed_session.isoformat(),
        'daily_data_as_of': first_item.get("daily_data_as_of"),
        'daily_data_status': first_item.get("daily_data_status"),
        'premarket_snapshot_at': first_item.get("premarket_snapshot_at"),
        'premarket_change_pct': first_item.get("premarket_change_pct"),
        'gap_pct': first_item.get("gap_pct"),
        'premarket_volume': first_item.get("premarket_volume"),
        'spread_pct': first_item.get("spread_pct"),
        'quote_age_seconds': first_item.get("quote_age_seconds"),
        'benchmark_data_as_of': first_item.get("benchmark_data_as_of"),
        'freshness_status': first_item.get("freshness_status"),
        'stale_reason': first_item.get("stale_reason"),
        'trading_eligible': False,
        'shadow_enabled': False,
        'paper_enabled': False,
        'live_enabled': False,
        'finalized_at': first_item.get("finalized_at"),
    }
    summary["provider_outputs"] = dict(integrated_ai.get("provider_outputs") or {})
    outcome = _selection_outcome(summary, provider_audit=integrated_ai.get("provider_audit") or {})
    summary.update(outcome)
    summary["top10"] = _enrich_selection_rows(list(summary.get("top10") or []), provider_outputs=summary["provider_outputs"])
    summary["top5"] = _enrich_selection_rows(list(summary.get("top5") or []), provider_outputs=summary["provider_outputs"])
    summary["top3"] = _enrich_selection_rows(list(summary.get("top3") or []), provider_outputs=summary["provider_outputs"])
    if summary.get("warnings_structured"):
        summary["warnings_structured"] = _dedupe_warning_records(list(summary.get("warnings_structured") or []))
        summary["warnings"] = [_format_warning_record(item) for item in summary["warnings_structured"]]

    latest_report_path, _ = _write_reports(summary)
    write_selection_state(
        et_date=current_session,
        generated_at=timestamp,
        selected_symbols=[str(item.get("ticker") or "").strip().upper() for item in selected],
        report_path=str(latest_report_path),
    )

    _publish_candidate_validation_records(summary)
    notification_rows = selected or summary_top3_source
    _notify_selection_result(summary, notification_rows)

    if selected and market_stage == "FINALIZED":
        restart_code = _restart_top_engines()
        if restart_code != 0:
            print(f"TOP restart failed with exit code {restart_code}.")
            sys.exit(restart_code)

    if str((summary.get("settings") or {}).get("selection_stage") or "") == "fast_preliminary":
        _spawn_background_refinement(timestamp)


def _has_live_top_configs() -> bool:
    import yaml

    for index in range(1, 6):
        path = PROJECT_DIR / "configs" / f"TOP{index}.yaml"
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if str(config.get("mode") or "").strip().lower() == "live":
            return True
    return False


def _publish_candidate_validation_records(summary: dict) -> None:
    try:
        store = CandidateValidationStore()
        candidate_rows = list(summary.get("top10") or [])
        if not candidate_rows:
            candidate_rows = list(summary.get("top3") or [])
        if not candidate_rows:
            candidate_rows = list(summary.get("report") or [])
        store.ingest_ai_selection_report(summary, candidate_rows)
    except Exception as exc:
        print(f"AI selection candidate validation warning: {exc}")


def _notify_selection_result(summary: dict, selected: list[dict]) -> None:
    try:
        notify_ai_selection_result(summary, top_configs=list(selected))
    except Exception as exc:
        print(f"AI selection notification warning: {exc}")


def _load_final_top_configs(limit: int = TOP_COUNT) -> list[dict]:
    import yaml

    configs: list[dict] = []
    for index in range(1, limit + 1):
        path = PROJECT_DIR / "configs" / f"TOP{index}.yaml"
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(data, dict):
            configs.append(data)
    return configs

if __name__ == '__main__':
    main()
