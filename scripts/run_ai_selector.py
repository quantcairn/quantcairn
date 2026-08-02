#!/usr/bin/env python3
"""Daily AI stock selector runner

Usage: scripts/run_ai_selector.py
"""
import os
import sys
import subprocess
import argparse
from collections import Counter
from functools import lru_cache

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

from src.openalpha.integration import AISelector
from src.openalpha.composition_filter import (
    CompositionFilter,
    is_inverse_etf,
    is_leveraged_or_inverse_etf,
)
from src.openalpha.selector import AIStrategySelector
from src.openalpha.range_score import RangeFitnessScorer
from src.openalpha.trade_filter import TradeEligibilityFilter
from src.utils.market_calendar import market_session_context, required_selection_date
from datetime import datetime
import os
import json
import re
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml

from src.config.local_env import load_local_ai_env
from src.openalpha.settings import load_runtime_settings
from src.openalpha import selector as _selector_module
from src.openalpha.config import load_runtime_config
from src.openalpha.settings import resolve_price_band
from src.openalpha.data_quality import (
    enrich_candidate_quality,
    evaluate_candidate_data_quality,
    formal_selection_ineligibility_reasons,
    is_formal_selection_eligible,
)
from src.openalpha.funnel_tracker import FunnelTracker, dropped_record, reason_from_candidate
from src.openalpha.market_context import build_candidate_market_snapshot
from src.openalpha.universe_filter import filter_universe_candidates, load_universe_rules
from src.data.fetcher import PriceFetcher
from src.notifier.alerts import notify_ai_selection_result
from src.candidate_validation import CandidateValidationStore
from src.dashboard.snapshots import write_dashboard_snapshot
from src.openalpha.selection_bundle import write_selection_bundle_atomic

PROJECT_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_DIR / "logs"
REPORTS_DIR = PROJECT_DIR / "reports"
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")
OPENALPHA_RUNTIME = load_runtime_config()
TOP_COUNT = max(1, int(OPENALPHA_RUNTIME.top_n))


def write_selection_filter_log(report: dict[str, object], now: datetime | None = None) -> Path:
    """Compatibility wrapper for selector filter logging.

    Tests monkeypatch ``LOG_DIR`` on this module, so we mirror that setting into
    the selector module before delegating to the real implementation.
    """

    original_log_dir = getattr(_selector_module, "LOG_DIR", None)
    try:
        _selector_module.LOG_DIR = LOG_DIR
        return _selector_module.write_selection_filter_log(report, now=now)
    finally:
        if original_log_dir is not None:
            _selector_module.LOG_DIR = original_log_dir


def write_selection_state(**kwargs):
    """Compatibility hook for historical tests.

    The actual atomic write happens through ``write_selection_bundle_atomic``.
    This hook keeps the old interception point alive without changing runtime
    behavior.
    """

    return dict(kwargs)


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


_OPENALPHA_FILE_CONFIG = _load_ai_selector_file_config()
ENTRY_PROXIMITY_ENABLED = bool(_OPENALPHA_FILE_CONFIG.get("entry_proximity_enabled", True))
ENTRY_PROXIMITY_WEIGHT = max(0.0, min(1.0, float(_OPENALPHA_FILE_CONFIG.get("entry_proximity_weight", 0.0) or 0.0)))
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

_EXPLANATION_ONLY_FIELDS = {
    "reason",
    "summary",
    "commentary",
    "explanation",
    "narrative",
    "analysis",
    "notes",
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

_VALIDATION_STAGE_BY_STATUS = {
    "AI_CANDIDATE": ("CLASSIFICATION", "候选分类"),
    "CLASSIFIED": ("BENCHMARK_ASSIGNMENT", "基准分配"),
    "BENCHMARK_ASSIGNED": ("STRATEGY_ASSIGNMENT", "策略分配"),
    "STRATEGY_ASSIGNED": ("DATA_VALIDATION", "数据验证"),
    "PENDING_DATA_VALIDATION": ("DATA_VALIDATION", "数据验证"),
    "DATA_VALID": ("BACKTEST_ADMISSION", "回测准入"),
    "PENDING_BACKTEST": ("BACKTEST", "回测"),
    "BACKTEST_COMPLETE": ("WALK_FORWARD_ADMISSION", "Walk-Forward 准入"),
    "PENDING_WALK_FORWARD": ("WALK_FORWARD", "Walk-Forward"),
    "WALK_FORWARD_COMPLETE": ("SHADOW_ADMISSION", "Shadow 准入"),
    "PENDING_SHADOW": ("SHADOW", "Shadow"),
    "SHADOW_OBSERVING": ("SHADOW_MONITORING", "Shadow 观察"),
    "SHADOW_COMPLETE": ("PAPER_ADMISSION", "Paper 准入"),
    "PAPER_ELIGIBLE": ("PAPER_READY", "Paper 就绪"),
    "LIVE_ELIGIBLE": ("LIVE_READY", "Live 就绪"),
}

_RESEARCH_TOP_CRITICAL_BLOCK_REASONS = {
    "critical_market_data_fallback",
    "critical_fallback_severity",
    "quote_missing",
    "missing_quote",
    "ohlcv_missing",
    "missing_ohlcv",
    "benchmark_invalid",
    "history_insufficient",
    "history_missing",
    "missing_history",
    "stale_data",
}


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _selection_date() -> str:
    return required_selection_date(_et_now())


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_ticker(value: str) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _next_validation_stage(status: str | None) -> dict:
    normalized = str(status or "").strip().upper()
    code, label = _VALIDATION_STAGE_BY_STATUS.get(normalized, ("UNKNOWN", "未知"))
    return {
        "next_validation_stage": code,
        "next_validation_stage_label": label,
    }


def _candidate_validation_records_by_symbol() -> dict[str, dict]:
    try:
        records = CandidateValidationStore().load_latest_candidates()
    except Exception:
        return {}
    by_symbol: dict[str, dict] = {}
    for record in records:
        try:
            row = record.summary_row()
        except Exception:
            continue
        symbol = _normalize_ticker(row.get("symbol") or getattr(record, "symbol", ""))
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = row
    return by_symbol


def _apply_validation_snapshot(row: dict, validation_records: dict[str, dict] | None = None) -> dict:
    item = dict(row or {})
    symbol = _normalize_ticker(item.get("ticker") or item.get("symbol"))
    snapshot = dict((validation_records or {}).get(symbol) or {})
    if snapshot:
        item.setdefault("candidate_id", snapshot.get("candidate_id"))
        item.setdefault("validation_status", snapshot.get("validation_status"))
        item.setdefault("current_validation_status", snapshot.get("current_validation_status") or snapshot.get("validation_status"))
        item.setdefault("trade_admission_status", snapshot.get("trade_admission_status"))
        item.setdefault("evidence_status", snapshot.get("evidence_status"))
        item.setdefault("profitability_status", snapshot.get("profitability_status"))
        item.setdefault("deployment_status", snapshot.get("deployment_status"))
    item["validation_status"] = str(item.get("current_validation_status") or item.get("validation_status") or "AI_CANDIDATE").strip().upper()
    item["current_validation_status"] = item["validation_status"]
    item["trade_admission_status"] = str(item.get("trade_admission_status") or item.get("trade_admission") or "NOT_TRADABLE").strip().upper()
    item.update(_next_validation_stage(item["validation_status"]))
    item["validation_record_resolved"] = bool(item.get("candidate_id"))
    return item


def _is_research_top_candidate(row: dict) -> bool:
    item = dict(row or {})
    if not _normalize_ticker(item.get("ticker") or item.get("symbol")):
        return False
    if is_formal_selection_eligible(item):
        return False
    market_data_sufficiency = str(item.get("market_data_sufficiency") or "").strip().upper()
    if market_data_sufficiency not in {"COMPLETE", "SUFFICIENT"}:
        return False
    if bool(item.get("formal_scoring_eligibility", item.get("scoring_eligible", False))) is not True:
        return False
    if str(item.get("score_type") or "").strip().upper() != "FORMAL":
        return False
    if item.get("score_is_current_run") is not True:
        return False
    score_value = _coalesce_float(
        item.get("formal_candidate_score"),
        item.get("candidate_score"),
        item.get("final_score"),
        item.get("score"),
        default=None,
    )
    if score_value is None:
        return False
    if str(item.get("fallback_scope") or "").strip().upper() == "CRITICAL_MARKET_DATA":
        return False
    if str(item.get("fallback_severity") or "").strip().upper() == "CRITICAL":
        return False
    blocking_reasons = item.get("blocking_reasons") or []
    if isinstance(blocking_reasons, (str, bytes)):
        blocking_reasons = [blocking_reasons]
    normalized_reasons = {str(reason or "").strip().lower() for reason in blocking_reasons}
    if normalized_reasons.intersection(_RESEARCH_TOP_CRITICAL_BLOCK_REASONS):
        return False
    return True


def _build_research_top_candidates(
    rows: list[dict],
    *,
    validation_records: dict[str, dict] | None = None,
    requested_top_n: int = TOP_COUNT,
) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()
    for raw in rows or []:
        item = _apply_validation_snapshot(dict(raw or {}), validation_records)
        if not _is_research_top_candidate(item):
            continue
        symbol = _normalize_ticker(item.get("ticker") or item.get("symbol"))
        if not symbol or symbol in seen:
            continue
        item["ticker"] = symbol
        item["research_top_eligible"] = True
        item["formal_top_selected"] = False
        item["paper_live_allowed"] = False
        item["validation_path_note"] = "可进入研究验证链，不可进入 Paper / Live"
        candidates.append(item)
        seen.add(symbol)
    candidates.sort(
        key=lambda item: (
            -float(_coalesce_float(item.get("formal_candidate_score"), item.get("candidate_score"), item.get("final_score"), item.get("score"), default=0.0) or 0.0),
            str(item.get("ticker") or ""),
        )
    )
    for idx, item in enumerate(candidates[:requested_top_n], start=1):
        item["research_rank"] = idx
    return candidates[:requested_top_n]


def _validation_pipeline_summary(research_top: list[dict], tradable_top: list[dict], *, requested_top_n: int = TOP_COUNT) -> dict:
    status_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    for item in research_top or []:
        status = str(item.get("validation_status") or item.get("current_validation_status") or "UNKNOWN").strip().upper() or "UNKNOWN"
        stage = str(item.get("next_validation_stage") or "UNKNOWN").strip().upper() or "UNKNOWN"
        status_counts[status] = status_counts.get(status, 0) + 1
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    first_stage = _next_validation_stage(research_top[0].get("validation_status") if research_top else None)
    return {
        "research_candidate_count": len(research_top or []),
        "tradable_candidate_count": len(tradable_top or []),
        "requested_top_n": int(requested_top_n),
        "next_validation_stage": first_stage["next_validation_stage"],
        "next_validation_stage_label": first_stage["next_validation_stage_label"],
        "validation_status_counts": status_counts,
        "next_validation_stage_counts": stage_counts,
        "paper_live_blocked": len(tradable_top or []) <= 0,
        "auto_validation_triggered": False,
    }


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

    if os.environ.get("OPENALPHA_DIRECT_HISTORY", "1") != "0":
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


def _candidate_fallback_metadata(candidate: dict, provider_outputs: dict[str, dict] | None) -> tuple[str, str, list[str]]:
    provider_outputs = dict(provider_outputs or {})
    candidate_fallback = bool(
        candidate.get("fallback_used")
        or candidate.get("quality_backfill")
        or candidate.get("fallback_history_incomplete")
        or candidate.get("data_status") in {"STALE", "INVALID"}
        or candidate.get("scoring_eligible") is False
    )
    affected_fields: set[str] = set()
    fallback_sources: set[str] = set()
    mock_sources: set[str] = set()
    has_critical_fields = False
    has_factor_fields = False
    has_explanation_fields = False
    ticker = _normalize_ticker(candidate.get("ticker"))
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
            fallback_sources.add(provider_name)
            candidate_fallback = True
        if is_mock:
            mock_sources.add(provider_name)
        fields = {
            str(key)
            for key, value in provider_row.items()
            if key not in {"ticker", "symbol", "source", "reason", "error_message", "error_code", "fallback", "mock", "mock_used", "fallback_used", "timed_out"} and value is not None
        }
        affected_fields.update(fields)
        if fields & _CRITICAL_MARKET_DATA_FIELDS:
            has_critical_fields = True
        elif fields & _NON_CRITICAL_FACTOR_FIELDS:
            has_factor_fields = True
        elif fields & _EXPLANATION_ONLY_FIELDS:
            has_explanation_fields = True
    if has_critical_fields:
        return "CRITICAL_MARKET_DATA", "CRITICAL", sorted(affected_fields)
    if has_factor_fields:
        return "NON_CRITICAL_FACTOR", "DEGRADED", sorted(affected_fields)
    if has_explanation_fields or candidate_fallback or mock_sources:
        return "EXPLANATION_ONLY", "INFO", sorted(affected_fields)
    return "", "", sorted(affected_fields)


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


def _attach_current_run_score_provenance(item: dict, *, generated_at: str | None = None) -> dict:
    payload = dict(item or {})
    if not any(payload.get(key) is not None for key in ("candidate_score", "final_score", "score", "ai_score")):
        return payload
    source = str(payload.get("score_source") or payload.get("source") or "").strip().lower()
    if source in {"stub", "mock", "fallback", "cache", "cached", "historical", "prior_bundle", "manual", "seed"}:
        payload.setdefault("score_source", source.upper() or "UNKNOWN")
        payload.setdefault("score_provider", str(payload.get("source") or "UNKNOWN"))
        payload.setdefault("score_is_current_run", False)
        return payload
    payload.setdefault("score_source", "current_run_candidate_ranking")
    payload.setdefault("score_provider", "local_factor_scoring")
    payload.setdefault("score_generated_at", generated_at or payload.get("generated_at") or "")
    payload.setdefault("score_is_current_run", True)
    return payload


def _apply_formal_score_semantics(item: dict) -> dict:
    payload = dict(item or {})
    # Match score_candidate() semantics: check score_is_formal (set by score_candidate)
    # and score_type first, then fall back to eligibility fields.  Default to
    # True — same as score_candidate() in candidate_ranking.py.
    is_formal = bool(
        payload.get("score_is_formal")
        or payload.get("score_type") == "FORMAL"
        or payload.get("formal_scoring_eligibility", payload.get("scoring_eligible", True))
    )
    score_value = _coalesce_float(
        payload.get("candidate_score"),
        payload.get("final_score"),
        payload.get("score"),
        payload.get("ai_score"),
        default=None,
    )
    if is_formal:
        if score_value is not None:
            payload["formal_candidate_score"] = float(score_value)
            payload["candidate_score"] = float(score_value)
            payload["score"] = float(score_value)
            payload["final_score"] = float(score_value)
        payload["score_type"] = "FORMAL"
        payload["score_is_formal"] = True
        return payload

    diagnostic_score = _coalesce_float(payload.get("diagnostic_score"), score_value, default=None)
    if diagnostic_score is not None:
        payload["diagnostic_score"] = float(diagnostic_score)
    payload["diagnostic_score_reason"] = str(payload.get("diagnostic_score_reason") or payload.get("score_reason") or "")
    factor_scores = payload.get("factor_scores")
    if isinstance(factor_scores, dict):
        payload["diagnostic_factor_scores"] = dict(factor_scores)
    payload["formal_candidate_score"] = None
    payload["candidate_score"] = None
    payload["score"] = None
    payload["final_score"] = None
    payload["score_type"] = "DIAGNOSTIC"
    payload["score_is_formal"] = False
    return payload


@lru_cache(maxsize=1024)
def _market_snapshot_for_ticker(ticker: str) -> dict:
    ticker = _normalize_ticker(ticker)
    if not ticker:
        return {}
    try:
        snapshot = build_candidate_market_snapshot(ticker)
    except Exception:
        return {}
    return dict(snapshot or {})


def _backfill_market_snapshot(item: dict) -> dict:
    payload = dict(item or {})
    ticker = _normalize_ticker(payload.get("ticker"))
    if not ticker:
        return payload
    snapshot = {}
    for nested_key in ("market_data", "trade_market_data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict) and nested:
            snapshot = dict(nested)
            break
    if not snapshot:
        has_market_state = any(
            str(payload.get(key) or "").strip()
            for key in (
                "quote_status",
                "ohlcv_status",
                "history_status",
                "benchmark_status",
                "benchmark_alignment_status",
                "data_status",
                "market_data_sufficiency",
                "quote_timestamp",
                "daily_data_status",
                "freshness_status",
            )
        )
        if has_market_state:
            return payload
        snapshot = _market_snapshot_for_ticker(ticker)
    if not snapshot:
        return payload
    payload.update(snapshot)
    for nested_key in ("market_data", "trade_market_data"):
        existing = payload.get(nested_key)
        merged = dict(existing or {})
        merged.update(snapshot)
        payload[nested_key] = merged
    return payload


def _enrich_candidate_quality_rows(
    rows: list[dict],
    *,
    provider_audit: dict[str, dict] | None = None,
    provider_outputs: dict[str, dict] | None = None,
    score_generated_at: str | None = None,
) -> list[dict]:
    enriched: list[dict] = []
    for raw in rows or []:
        payload = _backfill_market_snapshot(raw)
        for nested_key in ("market_data", "trade_market_data", "metrics"):
            nested = payload.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key, value in nested.items():
                if key not in payload or payload.get(key) is None or payload.get(key) == "":
                    payload[key] = value
        item = enrich_candidate_quality(
            payload,
            provider_audit=provider_audit,
            provider_outputs=provider_outputs,
        )
        item = _apply_formal_score_semantics(item)
        item["candidate_score"] = float(
            item.get("candidate_score")
            or item.get("final_score")
            or item.get("score")
            or 0.0
        ) if item.get("score_is_formal", False) else None
        item["confidence_score"] = float(
            item.get("confidence_score")
            or item.get("confidence")
            or 0.0
        )
        item["score_reason"] = str(item.get("score_reason") or item.get("reason") or "")
        item["why_selected"] = str(item.get("why_selected") or "")
        item = _attach_current_run_score_provenance(item, generated_at=score_generated_at)
        enriched.append(item)
    return enriched


def _attach_ranking_context(rows: list[dict], ranked_rows: list[dict]) -> list[dict]:
    ranked = [
        dict(item)
        for item in ranked_rows or []
        if str((item or {}).get("ticker") or "").strip()
    ]
    ranked.sort(
        key=lambda item: (
            -float(item.get("candidate_score") or item.get("final_score") or item.get("score") or 0.0),
            str(item.get("ticker") or ""),
        )
    )
    selected_tickers = {
        str(item.get("ticker") or "").strip().upper()
        for item in rows or []
        if str(item.get("ticker") or "").strip()
    }
    enhanced: list[dict] = []
    for raw in rows or []:
        item = dict(raw)
        ticker = str(item.get("ticker") or "").strip().upper()
        score = float(item.get("candidate_score") or item.get("final_score") or item.get("score") or 0.0)
        nearest_rejected = None
        best_margin = None
        for other in ranked:
            other_ticker = str(other.get("ticker") or "").strip().upper()
            if not other_ticker or other_ticker == ticker or other_ticker in selected_tickers:
                continue
            other_score = float(other.get("candidate_score") or other.get("final_score") or other.get("score") or 0.0)
            margin = round(score - other_score, 4)
            if nearest_rejected is None or other_score > float(nearest_rejected.get("candidate_score") or nearest_rejected.get("final_score") or nearest_rejected.get("score") or 0.0):
                nearest_rejected = {
                    "ticker": other_ticker,
                    "candidate_score": round(other_score, 4),
                    "data_status": str(other.get("data_status") or ""),
                    "reason": str(other.get("score_reason") or other.get("reason") or ""),
                }
                best_margin = margin
        if nearest_rejected is None:
            item["nearest_rejected_candidate"] = {}
            item["ranking_margin"] = None
        else:
            item["nearest_rejected_candidate"] = nearest_rejected
            item["ranking_margin"] = best_margin
        if not item.get("why_selected"):
            if item.get("rank", 0) == 1:
                item["why_selected"] = "candidate_score highest among eligible candidates"
            elif item.get("data_status") == "COMPLETE":
                item["why_selected"] = "critical data complete and score ranked within top cohort"
            else:
                item["why_selected"] = "candidate selected after quality filtering"
        enhanced.append(item)
    return enhanced


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


def _resolve_preflight_symbols(selector: AIStrategySelector) -> list[str] | None:
    """Resolve the symbol universe used for the authoritative wrapper preflight.

    The selector and wrapper must observe the same runtime universe source so the
    persisted preflight artifact reflects the actual run context rather than a
    zero-sample diagnostic scan.
    """
    source = str(os.environ.get("OPENALPHA_UNIVERSE", "managed") or "managed").strip().lower()
    if source == "managed":
        managed = _selector_module._load_managed_universe()
        if managed:
            return list(managed)
        return list(selector.universe._load_local_snapshot())
    if source == "sample":
        return list(selector.universe._load_local_snapshot())
    try:
        return list(selector.universe.build_universe(source=source))
    except Exception:
        return list(selector.universe._load_local_snapshot())


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


def _symbols_from_rows(rows: list[dict] | list[str] | tuple) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in rows or []:
        value = raw
        if isinstance(raw, dict):
            value = raw.get("ticker") or raw.get("symbol")
        symbol = _normalize_ticker(str(value or ""))
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols


def _funnel_stage_dict(
    stage: str,
    input_symbols: list[str],
    output_symbols: list[str],
    dropped: list[dict] | None = None,
) -> dict:
    input_list = _symbols_from_rows(input_symbols)
    output_list = _symbols_from_rows(output_symbols)
    output_set = set(output_list)
    dropped_by_symbol = {
        _normalize_ticker(str(item.get("symbol") or item.get("ticker") or "")): dict(item)
        for item in dropped or []
        if _normalize_ticker(str(item.get("symbol") or item.get("ticker") or ""))
    }
    dropped_symbols = [symbol for symbol in input_list if symbol not in output_set]
    dropped_rows = [
        dropped_by_symbol.get(symbol)
        or dropped_record(symbol, "post_filter_removed", "removed by final post-processing")
        for symbol in dropped_symbols
    ]
    reason_counts = Counter(
        str(item.get("reason_code") or "unknown")
        for item in dropped_rows
        if str(item.get("reason_code") or "").strip()
    )
    return {
        "stage": stage,
        "input_count": len(input_list),
        "output_count": len(output_list),
        "eliminated": max(0, len(input_list) - len(output_list)),
        "input_symbols": input_list,
        "output_symbols": output_list,
        "dropped_symbols": dropped_symbols,
        "dropped": dropped_rows,
        "drop_reason_counts": dict(sorted(reason_counts.items())),
        "status": "WARN" if len(output_list) > len(input_list) else "PASS",
    }


def _rejection_rows_by_symbol(*groups: list[dict]) -> dict[str, dict]:
    reasons: dict[str, dict] = {}
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, dict):
                continue
            symbol = _normalize_ticker(str(raw.get("ticker") or raw.get("symbol") or ""))
            if not symbol or symbol in reasons:
                continue
            reason = reason_from_candidate(raw)
            detail = str(
                raw.get("reason_detail")
                or raw.get("details")
                or raw.get("reason")
                or raw.get("reject_reason")
                or raw.get("composition_reject_reason")
                or ""
            )
            reasons[symbol] = dropped_record(symbol, reason, detail, stage="POST_FILTER")
    return reasons


def _post_filter_drop_records(
    input_symbols: list[str],
    output_symbols: list[str],
    diagnostic_candidates: list[dict],
    *,
    trade_rejected: list[dict],
    universe_rejected: list[dict],
    price_rejected: list[dict],
    composition_rejected: list[dict],
    entry_quality_rejected: list[dict],
) -> list[dict]:
    output_set = set(_symbols_from_rows(output_symbols))
    diagnostic_by_symbol = {
        _normalize_ticker(str(item.get("ticker") or item.get("symbol") or "")): dict(item)
        for item in diagnostic_candidates or []
        if _normalize_ticker(str(item.get("ticker") or item.get("symbol") or ""))
    }
    explicit_reasons = _rejection_rows_by_symbol(
        trade_rejected,
        universe_rejected,
        price_rejected,
        composition_rejected,
        entry_quality_rejected,
    )
    dropped: list[dict] = []
    for symbol in _symbols_from_rows(input_symbols):
        if symbol in output_set:
            continue
        item = diagnostic_by_symbol.get(symbol)
        if item is not None:
            reasons = formal_selection_ineligibility_reasons(item)
            reason_code = reasons[0] if reasons else "post_filter_removed"
            dropped.append(
                dropped_record(
                    symbol,
                    reason_code,
                    ";".join(reasons),
                    stage="POST_FILTER",
                    rejection_reason_codes=reasons,
                )
            )
            continue
        if symbol in explicit_reasons:
            dropped.append(explicit_reasons[symbol])
            continue
        dropped.append(dropped_record(symbol, "post_filter_removed", "removed by final post-processing", stage="POST_FILTER"))
    return dropped


def _append_final_selection_funnel_stages(
    funnel: dict,
    *,
    diagnostic_candidates: list[dict],
    post_filter_candidates: list[dict],
    final_selected_candidates: list[dict],
    trade_rejected: list[dict],
    universe_rejected: list[dict],
    price_rejected: list[dict],
    composition_rejected: list[dict],
    entry_quality_rejected: list[dict],
) -> dict:
    report = dict(funnel or {})
    stages = [dict(stage) for stage in (report.get("stages") or []) if isinstance(stage, dict)]
    formal_top_stage = next((stage for stage in reversed(stages) if stage.get("stage") == "FORMAL_TOP"), None)
    post_input_symbols = _symbols_from_rows(
        (formal_top_stage or {}).get("output_symbols") or diagnostic_candidates
    )
    post_output_symbols = _symbols_from_rows(post_filter_candidates)
    final_output_symbols = _symbols_from_rows(final_selected_candidates)
    post_stage = _funnel_stage_dict(
        "POST_FILTER",
        post_input_symbols,
        post_output_symbols,
        _post_filter_drop_records(
            post_input_symbols,
            post_output_symbols,
            diagnostic_candidates,
            trade_rejected=trade_rejected,
            universe_rejected=universe_rejected,
            price_rejected=price_rejected,
            composition_rejected=composition_rejected,
            entry_quality_rejected=entry_quality_rejected,
        ),
    )
    final_stage = _funnel_stage_dict(
        "FINAL_SELECTED",
        post_output_symbols,
        final_output_symbols,
        [
            dropped_record(symbol, "final_selection_limit", "outside final executable TOP slots", stage="FINAL_SELECTED")
            for symbol in post_output_symbols
            if symbol not in set(final_output_symbols)
        ],
    )
    stages.extend([post_stage, final_stage])

    reason_counts = Counter(report.get("rejection_reason_counts") or {})
    nearest = [dict(item) for item in (report.get("nearest_rejected_candidates") or []) if isinstance(item, dict)]
    for stage in (post_stage, final_stage):
        reason_counts.update(stage.get("drop_reason_counts") or {})
        for item in stage.get("dropped") or []:
            nearest.append(
                {
                    **dict(item),
                    "stage": stage["stage"],
                    "reason_code": item.get("reason_code") or "unknown",
                    "reason_detail": item.get("reason_detail") or "",
                    "blocking": bool(item.get("blocking", True)),
                }
            )

    warnings = [dict(item) for item in (report.get("warnings") or []) if isinstance(item, dict)]
    if post_stage["output_count"] > post_stage["input_count"]:
        warnings.append({"stage": "POST_FILTER", "check": "output_gt_input", "detail": f"input={post_stage['input_count']} output={post_stage['output_count']}"})
    if final_stage["output_count"] > final_stage["input_count"]:
        warnings.append({"stage": "FINAL_SELECTED", "check": "output_gt_input", "detail": f"input={final_stage['input_count']} output={final_stage['output_count']}"})
    if stages[-3:-2]:
        previous_output = _symbols_from_rows(stages[-3].get("output_symbols") or [])
        if post_input_symbols != previous_output:
            warnings.append({
                "stage": "POST_FILTER",
                "check": "chain_break",
                "detail": f"previous_stage={stages[-3].get('stage')} expected_input={len(previous_output)} actual_input={len(post_input_symbols)}",
            })

    first_input_count = int((stages[0] if stages else {}).get("input_count") or 0)
    report["stages"] = stages
    report["rejection_reason_counts"] = dict(sorted(reason_counts.items()))
    report["nearest_rejected_candidates"] = nearest[:20]
    report["pipeline_consistent"] = bool(report.get("pipeline_consistent", True)) and not warnings
    report["warnings"] = warnings
    report["pipeline_success_rate"] = round((len(final_output_symbols) / first_input_count), 4) if first_input_count else 0.0
    report["final_selected"] = len(final_output_symbols)
    report["final_selected_symbols"] = final_output_symbols
    report["post_filter_selected"] = len(post_output_symbols)
    report["post_filter_symbols"] = post_output_symbols
    return report


def _selector_run_mode(value: str | None = None) -> str:
    mode = str(value or os.environ.get("OPENALPHA_RUN_MODE") or "full").strip().lower()
    if mode not in {"fast_preliminary", "quality_refined", "full"}:
        return "full"
    return mode


def _apply_selector_run_mode(mode: str) -> None:
    normalized = _selector_run_mode(mode)
    if normalized == "fast_preliminary":
        os.environ["OPENALPHA_FAST_START_ONLY"] = "1"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "1"
    elif normalized == "quality_refined":
        os.environ["OPENALPHA_FAST_START_ONLY"] = "0"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
    else:
        os.environ["OPENALPHA_FAST_START_ONLY"] = "0"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "1"


def _rejection_reason_code(item: dict) -> str:
    reasons = item.get("rejection_reason")
    if isinstance(reasons, list) and reasons:
        return str(reasons[0]).strip() or "unknown"
    for key in ("reason_code", "reason", "reject_reason", "scoring_block_reason", "composition_reject_reason", "selection_penalty_reason", "stale_reason"):
        value = item.get(key)
        if isinstance(value, list):
            value = next((str(v).strip() for v in value if str(v).strip()), "")
        value = str(value or "").strip()
        if value:
            return value.split(":", 1)[0].split(";", 1)[0].strip() or "unknown"
    return "unknown"


def _build_rejection_trace(groups: list[tuple[str, list[dict]]]) -> tuple[list[dict], dict[str, int]]:
    trace: list[dict] = []
    counts: dict[str, int] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for stage, rows in groups:
        for raw in rows or []:
            item = dict(raw or {})
            ticker = _normalize_ticker(item.get("ticker") or item.get("symbol"))
            reason_code = _rejection_reason_code(item)
            reason_detail = str(
                item.get("reason_detail")
                or item.get("details")
                or item.get("reason")
                or item.get("reject_reason")
                or item.get("scoring_block_reason")
                or ""
            ).strip()
            key = (stage, ticker, reason_code, reason_detail)
            if not ticker or key in seen:
                continue
            seen.add(key)
            record = {
                "symbol": ticker,
                "stage": stage,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
                "asset_type": str(item.get("asset_type") or "").strip(),
                "data_status": str(item.get("data_status") or "").strip().upper(),
                "scoring_eligible": bool(item.get("scoring_eligible", item.get("trade_filter_passed", False))),
                "candidate_score": float(item.get("candidate_score") or item.get("score") or item.get("final_score") or 0.0),
                "fallback_scope": str(item.get("fallback_scope") or ""),
                "fallback_severity": str(item.get("fallback_severity") or ""),
            }
            trace.append(record)
            counts[reason_code] = counts.get(reason_code, 0) + 1
    return trace, counts


def _candidate_symbols(rows: list[dict] | tuple[dict, ...] | None) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for item in rows or []:
        symbol = _normalize_ticker((item or {}).get("ticker") or (item or {}).get("symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _relative_project_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(PROJECT_DIR.resolve()))
    except Exception:
        try:
            return str(Path(path).relative_to(PROJECT_DIR))
        except Exception:
            return str(path)


def _market_data_ready(item: dict) -> bool:
    status_fields = (
        str(item.get("quote_status") or "").strip().upper(),
        str(item.get("ohlcv_status") or "").strip().upper(),
    )
    if any(value in {"MISSING", "INVALID", "STALE"} for value in status_fields if value):
        return False
    if not str((item.get("data_quality") or {}).get("quote_as_of") or item.get("quote_timestamp") or "").strip():
        return False
    if not str((item.get("data_quality") or {}).get("ohlcv_as_of") or item.get("daily_data_as_of") or "").strip():
        return False
    return True


def _data_quality_ready(item: dict) -> bool:
    market_data_sufficiency = str(item.get("market_data_sufficiency") or "").strip().upper()
    if market_data_sufficiency and market_data_sufficiency not in {"COMPLETE", "SUFFICIENT"}:
        return False
    data_status = str(item.get("data_status") or "").strip().upper()
    if data_status not in {"COMPLETE", "VALID"}:
        return False
    benchmark_status = str(item.get("benchmark_status") or item.get("benchmark_alignment_status") or "").strip().upper()
    if benchmark_status in {"MISSING", "INVALID", "STALE"}:
        return False
    for key in ("history_status", "freshness_status"):
        value = str(item.get(key) or "").strip().upper()
        if value in {"MISSING", "INVALID", "STALE"}:
            return False
    return True


def _drop_records_for_stage(input_rows: list[dict], output_rows: list[dict], *, default_reason: str) -> list[dict]:
    output_symbols = set(_candidate_symbols(output_rows))
    records: list[dict] = []
    for item in input_rows or []:
        symbol = _normalize_ticker(item.get("ticker") or item.get("symbol"))
        if not symbol or symbol in output_symbols:
            continue
        reason = reason_from_candidate(item)
        if reason == "unknown":
            reason = default_reason
        records.append(dropped_record(symbol, reason, str(item.get("reason") or item.get("reject_reason") or item.get("scoring_block_reason") or "")))
    return records


def _formal_ineligibility_drop_records(input_rows: list[dict], output_rows: list[dict]) -> list[dict]:
    output_symbols = set(_candidate_symbols(output_rows))
    records: list[dict] = []
    for item in input_rows or []:
        symbol = _normalize_ticker(item.get("ticker") or item.get("symbol"))
        if not symbol or symbol in output_symbols:
            continue
        reasons = formal_selection_ineligibility_reasons(item)
        score_value = _coalesce_float(
            item.get("formal_candidate_score"),
            item.get("candidate_score"),
            item.get("final_score"),
            item.get("score"),
            item.get("diagnostic_score"),
            default=None,
        )
        records.append(
            dropped_record(
                symbol,
                reasons[0] if reasons else "unknown",
                ",".join(reasons),
                formal_rank=item.get("formal_rank"),
                research_rank=item.get("rank") or item.get("research_rank"),
                formal_candidate_score=item.get("formal_candidate_score"),
                diagnostic_score=item.get("diagnostic_score"),
                candidate_score=score_value,
                score_type=item.get("score_type"),
                market_data_sufficiency=item.get("market_data_sufficiency"),
                formal_scoring_eligibility=bool(item.get("formal_scoring_eligibility", item.get("scoring_eligible", False))),
                research_evidence_status=item.get("research_evidence_status"),
                trade_admission_status=item.get("trade_admission_status") or item.get("trade_admission"),
                rejection_stage="FORMAL_ELIGIBILITY",
                rejection_reason_codes=reasons,
            )
        )
    return records


def _build_selection_funnel_report(
    *,
    selection_run_id: str,
    selection_date: str,
    universe_symbols: list[str],
    universe_filtered: list[dict],
    market_data_candidates: list[dict],
    data_quality_candidates: list[dict],
    scoring_candidates: list[dict],
    base_ranked_candidates: list[dict],
    research_candidates: list[dict],
    refined_candidates: list[dict],
    formal_eligible_candidates: list[dict],
    composition_candidates: list[dict],
    formal_top_candidates: list[dict],
    universe_rejected_rows: list[dict],
    composition_rejected_rows: list[dict],
    refinement_rejected_rows: list[dict],
) -> tuple[dict, Path | None]:
    tracker = FunnelTracker(selection_run_id=selection_run_id, selection_date=selection_date, project_dir=PROJECT_DIR)
    tracker.add_stage("UNIVERSE", universe_symbols, universe_symbols)
    tracker.add_stage("UNIVERSE_FILTER", universe_symbols, universe_filtered, dropped=universe_rejected_rows)
    tracker.add_stage(
        "MARKET_DATA",
        universe_filtered,
        market_data_candidates,
        dropped=_drop_records_for_stage(universe_filtered, market_data_candidates, default_reason="quote_missing"),
    )
    tracker.add_stage(
        "DATA_QUALITY",
        market_data_candidates,
        data_quality_candidates,
        dropped=_drop_records_for_stage(market_data_candidates, data_quality_candidates, default_reason="history_insufficient"),
    )
    tracker.add_stage(
        "SCORING_ELIGIBLE",
        data_quality_candidates,
        scoring_candidates,
        dropped=_drop_records_for_stage(data_quality_candidates, scoring_candidates, default_reason="scoring_ineligible"),
    )
    tracker.add_stage("BASE_RANKING", scoring_candidates, base_ranked_candidates)
    tracker.add_stage("RESEARCH_PROVIDER", base_ranked_candidates, research_candidates)
    tracker.add_stage(
        "REFINEMENT",
        research_candidates,
        refined_candidates,
        dropped=refinement_rejected_rows or _drop_records_for_stage(research_candidates, refined_candidates, default_reason="refinement_rejected"),
    )
    tracker.add_stage(
        "FORMAL_ELIGIBILITY",
        refined_candidates,
        formal_eligible_candidates,
        dropped=_formal_ineligibility_drop_records(refined_candidates, formal_eligible_candidates),
    )
    tracker.add_stage(
        "COMPOSITION_FILTER",
        formal_eligible_candidates,
        composition_candidates,
        dropped=composition_rejected_rows or _drop_records_for_stage(formal_eligible_candidates, composition_candidates, default_reason="composition_limit"),
    )
    tracker.add_stage(
        "FORMAL_TOP",
        composition_candidates,
        formal_top_candidates,
        dropped=_drop_records_for_stage(composition_candidates, formal_top_candidates, default_reason="top_n_limit"),
    )
    report = tracker.to_dict()
    path: Path | None = None
    try:
        path = tracker.write_report()
        report["funnel_report_path"] = str(path.relative_to(PROJECT_DIR))
    except Exception as exc:
        report["funnel_report_error"] = str(exc)
    tracker.print_table()
    return report, path


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
    selected_symbols: list[str] | None = None,
    missing_slots: list[str] | None = None,
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
    if selected_symbols is not None and record.get("selected_symbols") is None:
        record["selected_symbols"] = [str(item).strip().upper() for item in selected_symbols if str(item).strip()]
    if missing_slots is not None and record.get("missing_slots") is None:
        record["missing_slots"] = [str(item).strip().upper() for item in missing_slots if str(item).strip()]
    if details and not record.get("details"):
        record["details"] = details
    record.setdefault("details", "")
    record.setdefault("requested_count", requested_count if requested_count is not None else None)
    record.setdefault("selected_count", selected_count if selected_count is not None else None)
    record.setdefault("missing_count", missing_count if missing_count is not None else None)
    record.setdefault("selected_symbols", [str(item).strip().upper() for item in selected_symbols or [] if str(item).strip()])
    record.setdefault("missing_slots", [str(item).strip().upper() for item in missing_slots or [] if str(item).strip()])
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
            tuple(normalized.get("selected_symbols") or normalized.get("symbols") or []),
            tuple(normalized.get("missing_slots") or []),
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
    selected_symbols = record.get("selected_symbols") or record.get("symbols") or []
    if selected_symbols:
        parts.append(f"selected_symbols={','.join(selected_symbols)}")
    missing_slots = record.get("missing_slots") or []
    if missing_slots:
        parts.append(f"missing_slots={','.join(missing_slots)}")
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
        "provider_budget_exhausted": 0,
        "provider_unavailable": 0,
        "provider_malformed_responses": 0,
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
        provider_record["budget_exhausted"] = int(provider_record.get("budget_exhausted", 0) or 0)
        provider_record["unavailable"] = int(provider_record.get("unavailable", 0) or 0)
        provider_record["malformed_response"] = int(provider_record.get("malformed_response", 0) or 0)
        provider_record["empty_response"] = int(provider_record.get("empty_response", 0) or 0)
        provider_record["fallback_used"] = int(provider_record.get("fallback_used", 0) or 0)
        provider_record["mock_used"] = int(provider_record.get("mock_used", 0) or 0)
        provider_record["contributed_fields"] = list(provider_record.get("contributed_fields") or [])
        summary["provider_attempts"] += provider_record["attempted"]
        summary["provider_successes"] += provider_record["success"]
        summary["provider_failures"] += provider_record["failure"]
        summary["provider_timeouts"] += provider_record["timed_out"]
        summary["provider_budget_exhausted"] += provider_record["budget_exhausted"]
        summary["provider_unavailable"] += provider_record["unavailable"]
        summary["provider_malformed_responses"] += provider_record["malformed_response"]
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
    formal_top_items = []
    for item in top_items:
        if not isinstance(item, dict):
            continue
        trade_admission = str(item.get("trade_admission") or item.get("trade_admission_status") or "").strip().upper()
        validation_status = str(item.get("current_validation_status") or item.get("validation_status") or "").strip().upper()
        if trade_admission == "TRADABLE" and validation_status not in {"AI_CANDIDATE", "REJECTED", "DATA_INVALID", "FAILED", "NOT_TRADABLE"}:
            formal_top_items.append(item)
    selected_count = len(formal_top_items)
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
                "selected_symbols": [str(item.get("ticker") or "").upper() for item in formal_top_items if str(item.get("ticker") or "").strip()],
                "missing_slots": [f"TOP{i}" for i in range(selected_count + 1, top_count + 1)],
                "details": "final TOP still below requested count",
            },
            stage="FINALIZED",
            requested_count=top_count,
            selected_count=selected_count,
            missing_count=missing_count,
            selected_symbols=[str(item.get("ticker") or "").upper() for item in formal_top_items if str(item.get("ticker") or "").strip()],
            missing_slots=[f"TOP{i}" for i in range(selected_count + 1, top_count + 1)],
            details="final TOP still below requested count",
        )
        warnings = [item for item in warnings if item.get("warning_code") != "top_n_not_filled"]
        warnings.append(top_n_warning)

    provider_audit_summary = _build_provider_audit_summary(provider_audit or {}, provider_outputs)
    fallback_used = bool(summary.get("fallback_used", False)) or bool(summary.get("provider_fallback_used", False))
    mock_used = bool(provider_audit_summary.get("provider_mocks", 0))
    provider_timeout_count = int(provider_audit_summary.get("provider_timeouts", 0) or 0)
    provider_budget_exhausted = int(provider_audit_summary.get("provider_budget_exhausted", 0) or 0)
    provider_unavailable = int(provider_audit_summary.get("provider_unavailable", 0) or 0)
    provider_malformed = int(provider_audit_summary.get("provider_malformed_responses", 0) or 0)
    provider_empty = int(provider_audit_summary.get("provider_empty_responses", 0) or 0)
    timed_out = bool((summary.get("quality_filter_report") or {}).get("timed_out", False)) or bool(provider_timeout_count)
    invalid_candidates = []
    degraded_reasons: set[str] = set()
    research_only_reasons: set[str] = set()
    for item in top_items:
        candidate = dict(item or {})
        fallback_scope, fallback_severity, affected_fields = _candidate_fallback_metadata(candidate, provider_outputs)
        data_quality = dict(candidate.get("data_quality") or {})
        dq_status = str(data_quality.get("data_status") or candidate.get("data_status") or "").strip().upper()
        dq_scoring = bool(data_quality.get("scoring_eligible", candidate.get("scoring_eligible", False)))
        dq_warnings = list(data_quality.get("quality_warnings") or [])
        candidate_fallback = bool(
            candidate.get("fallback_used")
            or candidate.get("quality_backfill")
            or candidate.get("fallback_history_incomplete")
            or dq_status in {"STALE", "INVALID"}
            or dq_scoring is False
        )
        fallback_sources: list[str] = []
        mock_sources: list[str] = []
        ticker = _normalize_ticker(candidate.get("ticker"))
        for provider_name, provider_rows in provider_outputs.items():
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
        candidate["fallback_scope"] = fallback_scope or ("CRITICAL_MARKET_DATA" if candidate.get("data_status") in {"STALE", "INVALID"} else "")
        candidate["fallback_severity"] = fallback_severity or ("CRITICAL" if candidate.get("data_status") in {"STALE", "INVALID"} else "")
        candidate["affected_fields"] = affected_fields
        candidate["data_quality"] = data_quality
        candidate["data_status"] = dq_status or candidate.get("data_status")
        candidate["scoring_eligible"] = dq_scoring
        candidate["quality_warnings"] = list(dict.fromkeys([*dq_warnings, *(candidate.get("quality_warnings") or [])]))
        candidate["data_quality_score"] = float(candidate.get("data_quality_score") or (0.0 if dq_status == "INVALID" else 35.0 if dq_status == "STALE" else 85.0 if dq_warnings else 100.0))
        candidate["critical_data_sources"] = list(dict.fromkeys(list(data_quality.get("critical_data_sources") or [])))
        candidate["noncritical_data_sources"] = list(dict.fromkeys(list(data_quality.get("noncritical_data_sources") or [])))
        candidate["provider_chain"] = list(dict.fromkeys(list(data_quality.get("provider_chain") or [])))
        critical_fallback = fallback_scope == "CRITICAL_MARKET_DATA" or fallback_severity == "CRITICAL"
        candidate["research_degraded"] = bool(candidate["candidate_fallback"] or candidate["mock_used"] or dq_warnings or candidate.get("data_status") == "STALE")
        candidate["degraded"] = bool(critical_fallback or dq_status in {"STALE", "INVALID"})
        if critical_fallback:
            degraded_reasons.add("critical_market_data_fallback")
        elif candidate["candidate_fallback"]:
            research_only_reasons.add("fallback_used")
        if candidate["mock_used"]:
            research_only_reasons.add("mock_used")
        if candidate.get("data_status") == "STALE":
            degraded_reasons.add("stale_data")
        if candidate.get("data_status") == "INVALID":
            invalid_candidates.append(ticker)
            degraded_reasons.add("invalid_data")
        if candidate.get("data_quality") and not dq_scoring and candidate.get("fallback_scope") == "CRITICAL_MARKET_DATA":
            invalid_candidates.append(ticker)
            degraded_reasons.add("critical_market_data_blocked")
        if dq_warnings:
            if any(str(warning).startswith(("critical_", "invalid_", "stale_")) for warning in dq_warnings):
                degraded_reasons.update(dq_warnings)
            else:
                research_only_reasons.update(dq_warnings)
        candidate["degradation_reasons"] = sorted(
            set(
                [
                    *(["critical_market_data_fallback"] if critical_fallback else []),
                    *(["stale_data"] if candidate.get("data_status") == "STALE" else []),
                    *(["invalid_data"] if candidate.get("data_status") == "INVALID" else []),
                ]
            )
        )
        candidate["research_only_reasons"] = sorted(
            set(
                [
                    *(["fallback_used"] if candidate["candidate_fallback"] and not critical_fallback else []),
                    *(["mock_used"] if candidate["mock_used"] else []),
                    *([str(warning) for warning in dq_warnings if str(warning)]),
                ]
            )
        )
        item.update(candidate)
    result_quality = "COMPLETE"
    if invalid_candidates:
        result_quality = "INVALID"
    elif missing_count > 0 or bool(degraded_reasons):
        result_quality = "DEGRADED"
    research_admission = "RESEARCH_READY"
    # Check whether candidates carry PAPER_ELIGIBLE type from execution_mode separation
    _top_candidate_types = {str(item.get("candidate_type") or "") for item in top_items if isinstance(item, dict)}
    if "PAPER_ELIGIBLE" in _top_candidate_types:
        research_admission = "PAPER_ELIGIBLE"
    elif "LIVE_TRADABLE" in _top_candidate_types:
        research_admission = "RESEARCH_READY"  # LIVE → ready for trading
    elif result_quality == "DEGRADED" or fallback_used or mock_used or timed_out or provider_budget_exhausted or provider_unavailable or provider_malformed or provider_empty or bool(research_only_reasons):
        research_admission = "RESEARCH_ONLY"
    elif result_quality == "INVALID":
        research_admission = "BLOCKED"
    execution_status = "COMPLETED"
    if invalid_candidates and not top_items:
        execution_status = "FAILED"
    if execution_status == "FAILED":
        selection_outcome = "FAILED"
    elif selected_count <= 0:
        selection_outcome = "NO_TRADABLE_SELECTION"
    elif selected_count < top_count:
        selection_outcome = "PARTIAL"
    else:
        selection_outcome = "SUCCESS"
    return {
        "pipeline_status": execution_status,
        "execution_status": execution_status,
        "selection_outcome": selection_outcome,
        "completed_with_selection": selection_outcome in {"SUCCESS", "PARTIAL"},
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
        "research_only_reasons": sorted(research_only_reasons),
        "invalid_candidates": invalid_candidates,
        "provider_timeouts": provider_timeout_count,
        "provider_budget_exhausted": provider_budget_exhausted,
        "provider_unavailable": provider_unavailable,
        "provider_malformed_responses": provider_malformed,
    }


def _enrich_selection_rows(
    rows: list[dict],
    *,
    provider_outputs: dict[str, dict] | None = None,
    score_generated_at: str | None = None,
) -> list[dict]:
    enriched: list[dict] = []
    provider_outputs = dict(provider_outputs or {})
    for raw in rows or []:
        item = _backfill_market_snapshot(raw)
        for nested_key in ("market_data", "trade_market_data", "metrics"):
            nested = item.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key, value in nested.items():
                if key not in item or item.get(key) is None or item.get(key) == "":
                    item[key] = value
        item = enrich_candidate_quality(item, provider_outputs=provider_outputs)
        ticker = _normalize_ticker(item.get("ticker"))
        fallback_scope, fallback_severity, affected_fields = _candidate_fallback_metadata(item, provider_outputs)
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
        item["fallback_scope"] = fallback_scope or ("CRITICAL_MARKET_DATA" if item.get("data_status") in {"STALE", "INVALID"} else "")
        item["fallback_severity"] = fallback_severity or ("CRITICAL" if item.get("data_status") in {"STALE", "INVALID"} else "")
        item["affected_fields"] = affected_fields
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
        existing_trade_admission = str(item.get("trade_admission") or item.get("trade_admission_status") or "").strip().upper()
        item["trade_admission_status"] = existing_trade_admission or "NOT_TRADABLE"
        if not existing_trade_admission and item["current_validation_status"] in {"TRADABLE", "PAPER_ELIGIBLE", "LIVE_ELIGIBLE"}:
            item["trade_admission_status"] = "TRADABLE"
        item = _attach_current_run_score_provenance(item, generated_at=score_generated_at)
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
    if os.environ.get("OPENALPHA_RESTART_TOP", "1") == "0":
        print("OPENALPHA_RESTART_TOP=0; skipping TOP engine restart.")
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
    if os.environ.get("OPENALPHA_BACKGROUND_REFINEMENT", "1") != "1":
        return
    refine_script = PROJECT_DIR / "scripts" / "refine_ai_selection_report.py"
    if not refine_script.exists():
        return
    env = os.environ.copy()
    env.setdefault("OPENALPHA_FETCH_NEWS", "0")
    env.setdefault("OPENALPHA_ALLOW_PROXY_MARKET", "0")
    env.setdefault("OPENALPHA_DIRECT_HISTORY", "1")
    env.setdefault("OPENALPHA_SKIP_YFINANCE_HISTORY", "0")
    env.setdefault("OPENALPHA_HTTP_TIMEOUT_SECONDS", "2")
    env.setdefault("OPENALPHA_FILTER_CANDIDATE_LIMIT", "20")
    env.setdefault("OPENALPHA_TOTAL_BUDGET_SECONDS", "30")
    env.setdefault("OPENALPHA_QUALITY_BUDGET_SECONDS", "20")
    env["OPENALPHA_EXPECTED_TIMESTAMP"] = expected_timestamp
    env["OPENALPHA_REFINEMENT_ONLY"] = "1"
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

def main(mode: str | None = None):
    run_mode = _selector_run_mode(mode)
    _apply_selector_run_mode(run_mode)
    load_local_ai_env()
    runtime_settings = load_runtime_settings()
    min_price, max_price = resolve_price_band(runtime_settings)
    universe_price_min = min(rule.price_min for rule in UNIVERSE_RULES.values())
    universe_price_max = max(rule.price_max for rule in UNIVERSE_RULES.values())
    market_context = market_session_context(_et_now())
    os.environ.setdefault("OPENALPHA_MIN_PRICE", str(min_price))
    os.environ.setdefault("OPENALPHA_MAX_PRICE", str(max_price))
    os.environ.setdefault(
        "OPENALPHA_AUTO_REFRESH_MINUTES",
        str(runtime_settings.get("auto_refresh_minutes", 5)),
    )
    configured_max_symbols = int(runtime_settings.get("max_symbols", 20) or 20)
    # Managed universe has 35+ symbols — don't cap it to the legacy 9-symbol limit.
    universe_source = os.environ.get("OPENALPHA_UNIVERSE", "managed")
    if universe_source == "managed":
        os.environ.setdefault("OPENALPHA_MAX_SYMBOLS", "50")
    else:
        os.environ.setdefault("OPENALPHA_MAX_SYMBOLS", str(max(5, min(configured_max_symbols, 20))))
    os.environ.setdefault("OPENALPHA_ALLOW_PROXY_MARKET", "0")
    os.environ.setdefault("OPENALPHA_DIRECT_HISTORY", "1")
    os.environ.setdefault("OPENALPHA_SKIP_YFINANCE_HISTORY", "0")
    os.environ.setdefault("OPENALPHA_HTTP_TIMEOUT_SECONDS", "3")
    selection_run_id = uuid.uuid4().hex
    live_positions = _live_equity_positions()
    if live_positions is None and _has_live_top_configs():
        print("Live position verification failed; refusing to run selection or replace TOP configs.")
        sys.exit(1)
    sel = AIStrategySelector()

    # ── Preflight: market state + data availability ────────────────────
    from src.openalpha.preflight import run_preflight as _run_preflight, print_preflight
    _pf = _run_preflight(
        symbols=_resolve_preflight_symbols(sel),
        max_scan_symbols=5,
        selection_run_id=selection_run_id,
    )
    print_preflight(_pf)

    integrated_ai = _run_integrated_ai_selector()
    preferred_symbols = integrated_ai.get("preferred_symbols") or None
    selection_symbols = _merged_selection_symbols(preferred_symbols)
    # Let selector choose universe source (managed/sample/sp500) unless
    # explicitly overridden via --universe-source CLI.
    out = sel.run_selection(write_configs=False, selection_run_id=selection_run_id)
    selected = out.get('top5') or out.get('top3') or []
    if not selected and selection_symbols:
        integrated_ai["fallback_used"] = True
        out = sel.run_selection(write_configs=False, selection_run_id=selection_run_id)
        selected = out.get('top5') or out.get('top3') or []
    selector_run_id = str(out.get("selection_run_id") or "").strip()
    funnel_run_id = str((out.get("selection_funnel") or {}).get("selection_run_id") or "").strip()
    if selector_run_id and selector_run_id != selection_run_id:
        print(f"selection_run_id_mismatch: expected {selection_run_id}, got {selector_run_id}")
        sys.exit(1)
    if funnel_run_id and funnel_run_id != selection_run_id:
        print(f"selection_funnel_run_id_mismatch: expected {selection_run_id}, got {funnel_run_id}")
        sys.exit(1)
    selector_run_mode = str(
        (out.get("selection_funnel") or {}).get("run_mode")
        or out.get("run_mode")
        or ""
    ).strip().upper()
    preflight_run_mode = str(getattr(_pf, "run_mode", "") or "").strip().upper()
    if preflight_run_mode and selector_run_mode and preflight_run_mode != selector_run_mode:
        print(
            "selection_run_mode_mismatch: "
            f"expected preflight={preflight_run_mode}, selector={selector_run_mode}"
        )
        sys.exit(1)

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
    processing_phase = str((out.get("settings") or {}).get("selection_stage") or "")
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

    if processing_phase != "fast_preliminary":
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
    provider_audit_snapshot = dict(integrated_ai.get("provider_audit") or {})
    provider_outputs_snapshot = dict(integrated_ai.get("provider_outputs") or {})
    provider_audit_summary = _build_provider_audit_summary(provider_audit_snapshot, provider_outputs_snapshot)
    selected = _enrich_candidate_quality_rows(
        selected,
        provider_audit=provider_audit_snapshot,
        provider_outputs=provider_outputs_snapshot,
    )
    report_top10 = _enrich_candidate_quality_rows(
        report_top10,
        provider_audit=provider_audit_snapshot,
        provider_outputs=provider_outputs_snapshot,
    )
    selected = _attach_ranking_context(selected, report_top10)
    report_top10 = _attach_ranking_context(report_top10, report_top10)
    diagnostic_selected = list(selected)
    selected = [item for item in selected if is_formal_selection_eligible(item)]
    post_filter_selected = list(selected)
    selection_stage = "FINALIZED"
    market_stage = selection_stage
    current_session = _selection_date()
    # Read actual universe from selector's own funnel stages (not legacy selection_symbols)
    _sel_funnel = out.get("selection_funnel") or {}
    _sel_stages = _sel_funnel.get("stages") or []
    _universe_stage = next((s for s in _sel_stages if s.get("stage") == "UNIVERSE"), {})
    universe_symbols_for_funnel = _universe_stage.get("input_symbols") or []
    if not universe_symbols_for_funnel:
        universe_symbols_for_funnel = [
            str(item).strip().upper()
            for item in (out.get("top10") or [])
            if str(item.get("ticker", "")).strip()
        ]
    market_data_candidates = [item for item in report_top10 if _market_data_ready(item)]
    data_quality_candidates = [item for item in market_data_candidates if _data_quality_ready(item)]
    scoring_candidates = [
        item
        for item in data_quality_candidates
        if bool(item.get("formal_scoring_eligibility", item.get("scoring_eligible", False)))
    ]
    base_ranked_candidates = list(scoring_candidates)
    research_candidates = list(base_ranked_candidates)
    formal_eligible_candidates = list(selected)
    composition_candidates = list(selected)
    formal_top_candidates = list(selected[:TOP_COUNT])
    # ── Read funnel from selector (single source of truth) ──
    funnel_report = dict(out.get("selection_funnel") or {})
    funnel_report_path = funnel_report.get("funnel_report_path") or None
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
    quality_report["diagnostic_selected_symbols"] = [
        str(item.get("ticker") or "").strip().upper()
        for item in diagnostic_selected
        if str(item.get("ticker") or "").strip()
    ]
    rejection_trace, rejection_reason_counts = _build_rejection_trace(
        [
            ("UNIVERSE", list(universe_rejected_rows or [])),
            ("PRICE_BAND", list(price_band_rejected_rows or [])),
            ("TRADE_FILTER", list(trade_filter_rejected or [])),
            ("COMPOSITION", list(composition_filter_report.get("rejected") or [])),
            ("ENTRY_QUALITY", list(low_quality_rejected or [])),
        ]
    )
    rejection_reason_counts = {
        **dict(funnel_report.get("rejection_reason_counts") or {}),
        **dict(rejection_reason_counts or {}),
    }
    quality_report["rejection_trace"] = list(rejection_trace)
    quality_report["rejection_reason_counts"] = dict(rejection_reason_counts)
    actual_universe_count = int(len(universe_symbols_for_funnel) or out.get("universe_symbol_count", 0) or 0)
    legacy_funnel_counts = {
        "universe_scanned": actual_universe_count,
        "universe_passed": int(len(report_top10 or [])),
        "quote_complete": int(sum(1 for item in (report_top10 or []) if str((item or {}).get("data_quality", {}).get("quote_as_of") or (item or {}).get("quote_timestamp") or "").strip())),
        "ohlcv_complete": int(sum(1 for item in (report_top10 or []) if str((item or {}).get("data_quality", {}).get("ohlcv_as_of") or (item or {}).get("daily_data_as_of") or "").strip())),
        "benchmark_complete": int(sum(1 for item in (report_top10 or []) if str(
            (item or {}).get("benchmark_status")
            or (item or {}).get("benchmark_alignment_status")
            or ((item or {}).get("market_data") or {}).get("benchmark_status")
            or ((item or {}).get("market_data") or {}).get("benchmark_alignment_status")
            or ""
        ).strip().upper() == "VALID")),
        "provider_empty_responses": int(provider_audit_summary.get("provider_empty_responses", 0) or 0),
        "provider_budget_exhausted": int(provider_audit_summary.get("provider_budget_exhausted", 0) or 0),
        "provider_unavailable": int(provider_audit_summary.get("provider_unavailable", 0) or 0),
        "provider_malformed_responses": int(provider_audit_summary.get("provider_malformed_responses", 0) or 0),
        "data_complete": int(sum(1 for item in (report_top10 or []) if str((item or {}).get("data_status") or "").strip().upper() == "COMPLETE")),
        "scoring_eligible": int(
            sum(
                1
                for item in (report_top10 or [])
                if bool(item.get("formal_scoring_eligibility", item.get("scoring_eligible", False)))
            )
        ),
        "formal_scoring_eligible": int(
            sum(
                1
                for item in (report_top10 or [])
                if bool(item.get("formal_scoring_eligibility", item.get("scoring_eligible", False)))
            )
        ),
        "ranked_candidates": int(len(report_top10 or [])),
        "quality_threshold_passed": int(sum(1 for item in (report_top10 or []) if item.get("trade_filter_passed", True))),
        "preliminary_selected": int(len(out.get("top3") or out.get("top5") or [])),
        "refined_selected": int(len(selected)),
        "final_selected": int(len(selected)),
        "provider_timeouts": int((provider_audit_summary.get("provider_timeouts", 0) if isinstance(provider_audit_summary, dict) else 0) or 0),
        "provider_failures": int(provider_audit_summary.get("provider_failures", 0) or 0),
        "total_budget_seconds": int(os.environ.get("OPENALPHA_TOTAL_BUDGET_SECONDS", "0") or 0),
        "budget_exhausted": bool((out.get("quality_filter_report") or {}).get("timed_out", False)),
        "run_mode": run_mode,
    }
    quality_report["selection_funnel"] = {
        # Selector's own funnel stages (authoritative — uses actual universe)
        **dict(_sel_funnel or {}),
        # Legacy flat counts as fallback only
        **legacy_funnel_counts,
        **dict(funnel_report or {}),
    }
    quality_report["nearest_rejected_candidates"] = list(funnel_report.get("nearest_rejected_candidates") or [])
    if funnel_report_path is not None:
        quality_report["funnel_report_path"] = _relative_project_path(funnel_report_path)
    out["quality_filter_report"] = quality_report
    out["top10"] = list(report_top10)
    out["settings"] = dict(out.get("settings") or {})
    out["settings"]["min_price"] = float(universe_price_min)
    out["settings"]["max_price"] = float(universe_price_max)
    out["settings"]["price_band"] = {"min": float(universe_price_min), "max": float(universe_price_max)}
    out["settings"]["universe_filter"] = _universe_settings_payload()
    out["settings"]["selection_stage"] = processing_phase
    out["settings"]["processing_phase"] = processing_phase
    out["settings"]["entry_proximity_enabled"] = bool(ENTRY_PROXIMITY_ENABLED)
    out["settings"]["entry_proximity_weight"] = float(ENTRY_PROXIMITY_WEIGHT)
    summary_top3_source = list(selected)
    if selected:
        for item in selected:
            item["selection_date"] = current_session
            item["protected_position"] = bool(item.get("protected_position") or item.get("existing_position"))
            item.update(_normalize_selection_metadata(item))
            item["fallback_used"] = bool(item.get("fallback_used", False))
            item["composition_filter_passed"] = bool(item.get("composition_filter_passed", True))
            item["composition_reject_reason"] = str(item.get("composition_reject_reason") or "")
            item["final_rank"] = int(item.get("final_rank") or 0)
    else:
        selected = []
    selected = list(selected[:TOP_COUNT])
    for idx, item in enumerate(selected, start=1):
        item["final_rank"] = idx
    if selected:
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
        out["top5"] = []
        out["top3"] = []
        out["report"] = []
    final_selected_symbols = [
        str(item.get("ticker") or "").strip().upper()
        for item in selected
        if str(item.get("ticker") or "").strip()
    ]
    quality_report["final_selected_symbols"] = list(final_selected_symbols)
    quality_report["selection_count"] = len(selected)
    quality_report["top_n_filled"] = len(selected) >= TOP_COUNT
    quality_report["missing_slots"] = max(0, TOP_COUNT - len(selected))
    quality_report["disabled_configs"] = [
        f"TOP{i}.yaml" for i in range(len(selected) + 1, TOP_COUNT + 1)
    ]
    quality_report["selection_funnel"] = _append_final_selection_funnel_stages(
        dict(quality_report.get("selection_funnel") or {}),
        diagnostic_candidates=list(diagnostic_selected),
        post_filter_candidates=list(post_filter_selected),
        final_selected_candidates=list(selected),
        trade_rejected=list(trade_filter_rejected or []),
        universe_rejected=list(universe_rejected_rows or []),
        price_rejected=list(price_band_rejected_rows or []),
        composition_rejected=list(composition_filter_report.get("rejected") or []),
        entry_quality_rejected=list(low_quality_rejected or []),
    )
    quality_report["rejection_reason_counts"] = dict(
        quality_report["selection_funnel"].get("rejection_reason_counts") or {}
    )
    quality_report["nearest_rejected_candidates"] = list(
        quality_report["selection_funnel"].get("nearest_rejected_candidates") or []
    )
    out["quality_filter_report"] = quality_report
    write_selection_filter_log(quality_report)
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

    top3_summary = [_normalize_entry_report_fields(item) for item in list(selected)]
    first_item = top3_summary[0] if top3_summary else {}
    summary = {
        'timestamp': timestamp,
        'generated_at': timestamp,
        'selection_date': current_session,
        'selection_stage': selection_stage,
        'processing_phase': processing_phase,
        'selection_run_id': selection_run_id,
        'top_sync_run_id': selection_run_id,
        'top_sync_status': 'OK',
        'top_sync_error': '',
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
    summary["top10"] = _enrich_selection_rows(list(summary.get("top10") or []), provider_outputs=summary["provider_outputs"], score_generated_at=timestamp)
    summary["top5"] = _enrich_selection_rows(list(summary.get("top5") or []), provider_outputs=summary["provider_outputs"], score_generated_at=timestamp)
    summary["top3"] = _enrich_selection_rows(list(summary.get("top3") or []), provider_outputs=summary["provider_outputs"], score_generated_at=timestamp)
    summary["report"] = _enrich_selection_rows(list(summary.get("report") or []), provider_outputs=summary["provider_outputs"], score_generated_at=timestamp)
    selected = _enrich_selection_rows(list(selected), provider_outputs=summary["provider_outputs"], score_generated_at=timestamp)
    validation_records = _candidate_validation_records_by_symbol()
    tradable_top_candidates = [_apply_validation_snapshot(dict(item or {}), validation_records) for item in selected]
    research_top_candidates = _build_research_top_candidates(
        list(summary.get("top10") or []),
        validation_records=validation_records,
        requested_top_n=TOP_COUNT,
    )
    validation_pipeline_summary = _validation_pipeline_summary(
        research_top_candidates,
        tradable_top_candidates,
        requested_top_n=TOP_COUNT,
    )
    summary["research_top_candidates"] = list(research_top_candidates)
    summary["research_selected_top_n"] = len(research_top_candidates)
    summary["research_requested_top_n"] = TOP_COUNT
    summary["tradable_top_candidates"] = list(tradable_top_candidates)
    summary["tradable_selected_top_n"] = len(tradable_top_candidates)
    summary["tradable_requested_top_n"] = TOP_COUNT
    summary["next_validation_stage"] = validation_pipeline_summary.get("next_validation_stage")
    summary["next_validation_stage_label"] = validation_pipeline_summary.get("next_validation_stage_label")
    summary["validation_pipeline_summary"] = dict(validation_pipeline_summary)
    if summary.get("warnings_structured"):
        summary["warnings_structured"] = _dedupe_warning_records(list(summary.get("warnings_structured") or []))
        summary["warnings"] = [_format_warning_record(item) for item in summary["warnings_structured"]]
    summary["rejection_trace"] = list(quality_report.get("rejection_trace") or [])
    summary["rejection_reason_counts"] = dict(quality_report.get("rejection_reason_counts") or {})
    summary["selection_funnel"] = dict(quality_report.get("selection_funnel") or {})
    summary["nearest_rejected_candidates"] = list(quality_report.get("nearest_rejected_candidates") or [])
    if quality_report.get("funnel_report_path"):
        summary["funnel_report_path"] = quality_report.get("funnel_report_path")
    summary["final_selected_symbols"] = [
        str(item.get("ticker") or "").strip().upper()
        for item in selected
        if str(item.get("ticker") or "").strip()
    ]

    selection_state_hook_payload = {
        "et_date": current_session,
        "generated_at": timestamp,
        "selected_symbols": [str(item.get("ticker") or "").strip().upper() for item in selected],
        "report_path": str(PROJECT_DIR / "reports" / "ai_selection_latest.json"),
        "selection_stage": selection_stage,
        "processing_phase": processing_phase,
        "result_quality": str(summary.get("result_quality") or ""),
        "research_admission": str(summary.get("research_admission") or ""),
        "selection_run_id": selection_run_id,
        "top_sync_run_id": selection_run_id,
        "top_sync_status": "OK",
        "top_sync_error": "",
        "selection_symbols": [str(item.get("ticker") or "").strip().upper() for item in selected],
        "configured_top_symbols": [str(item.get("ticker") or "").strip().upper() for item in selected],
        "disabled_slots": list(range(len(selected) + 1, TOP_COUNT + 1)),
        "synced_at": timestamp,
    }
    bundle_result = write_selection_bundle_atomic(
        summary=summary,
        selection_state_payload=selection_state_hook_payload,
        top_items=list(selected),
        selection_run_id=selection_run_id,
        selection_date=current_session,
        generated_at=timestamp,
        result_quality=str(summary.get("result_quality") or ""),
        research_admission=str(summary.get("research_admission") or ""),
        processing_phase=processing_phase,
        requested_top_n=int(summary.get("target_top_n") or TOP_COUNT),
        top_sync_status="OK",
        top_sync_error="",
    )
    if isinstance(bundle_result, dict):
        summary.update(bundle_result)

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
    from src.openalpha.selection_state import has_live_top_configs as _selection_has_live_top_configs

    return bool(_selection_has_live_top_configs(limit=max(TOP_COUNT, 3)))


def _publish_candidate_validation_records(summary: dict) -> None:
    try:
        store = CandidateValidationStore()
        candidate_rows = list(summary.get("top10") or [])
        if not candidate_rows:
            candidate_rows = list(summary.get("top3") or [])
        if not candidate_rows:
            candidate_rows = list(summary.get("report") or [])
        records = store.ingest_ai_selection_report(summary, candidate_rows)
    except Exception as exc:
        print(f"AI selection candidate validation warning: {exc}")
        return
    try:
        write_dashboard_snapshot(
            "candidate_validation",
            _candidate_validation_dashboard_snapshot_data(records, summary),
            source_run_id=str(summary.get("selection_run_id") or ""),
            generated_at=str(summary.get("generated_at") or summary.get("timestamp") or ""),
        )
    except Exception as exc:
        print(f"AI selection candidate validation snapshot warning: {exc}")


def _candidate_validation_dashboard_snapshot_data(records: list, summary: dict) -> dict[str, object]:
    rows = [
        record.to_dict() if hasattr(record, "to_dict") else dict(record)
        for record in (records or [])
        if hasattr(record, "to_dict") or isinstance(record, dict)
    ]
    latest = dict(rows[0]) if rows else {}
    metadata = dict(latest.get("metadata") or {}) if isinstance(latest.get("metadata"), dict) else {}
    updated_at = str(latest.get("updated_at") or summary.get("generated_at") or summary.get("timestamp") or "")
    validation_status = str(latest.get("validation_status") or metadata.get("current_validation_status") or "AI_CANDIDATE").strip().upper()
    status_issue = None
    state = "SAFE" if rows else "STALE"
    detail = "candidate validation ready" if rows else "candidate validation data unavailable"
    if validation_status == "REJECTED" and not latest.get("rejection_reason"):
        status_issue = "rejection_reason_missing"
        state = "UNSAFE"
        detail = "data_invalid"
    return {
        "available": bool(rows),
        "state": state,
        "status_label": state,
        "detail": detail,
        "title": "AI Candidate Validation",
        "candidate_count": len(rows),
        "history_count": 0,
        "latest_candidate": latest,
        "candidate_validation_rows": rows[:5],
        "performance": {
            "available": False,
            "state": "STALE",
            "status_label": "STALE",
            "detail": "candidate performance unavailable in selector snapshot",
            "title": "Candidate Ranking Performance",
            "candidate_count": 0,
            "average_score": None,
            "high_score_threshold": 80.0,
            "high_score_candidate_count": 0,
            "high_score_success_rate": None,
            "score_bucket_distribution": [],
            "performance_rows": [],
            "last_updated": None,
        },
        "research_report": {
            "available": False,
            "state": "STALE",
            "status_label": "STALE",
            "detail": "research report unavailable in selector snapshot",
            "title": "AI Candidate Daily Research Report",
            "display_title": "AI Research Report",
            "generated_at": None,
            "candidate_count": 0,
            "top_candidates": [],
            "final_selected": [],
            "final_selected_count": 0,
            "selection_outcome": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
            "actionable_candidate_status": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        },
        "last_updated": updated_at or None,
        "status_issue": status_issue,
        "validation_status": validation_status,
        "selection_stage": str(latest.get("selection_stage") or metadata.get("selection_stage") or metadata.get("market_selection_stage") or "PRELIMINARY").strip().upper(),
        "freshness_status": str(latest.get("freshness_status") or metadata.get("freshness_status") or "SAFE").strip().upper(),
        "stale_reason": str(latest.get("stale_reason") or metadata.get("stale_reason") or ""),
        "last_completed_session": str(latest.get("last_completed_session") or metadata.get("last_completed_session") or ""),
        "daily_data_as_of": str(latest.get("daily_data_as_of") or metadata.get("daily_data_as_of") or ""),
        "premarket_snapshot_at": str(latest.get("premarket_snapshot_at") or metadata.get("premarket_snapshot_at") or ""),
        "data_mode": str(latest.get("data_mode") or metadata.get("data_mode") or ""),
        "data_freshness": str(latest.get("data_freshness") or metadata.get("data_freshness") or ""),
        "data_status": str(latest.get("data_status") or metadata.get("data_status") or ""),
        "scoring_eligible": bool(latest.get("scoring_eligible") or metadata.get("scoring_eligible") or False),
        "scoring_block_reason": str(latest.get("scoring_block_reason") or metadata.get("scoring_block_reason") or ""),
        "trade_filter_passed": bool(latest.get("trade_filter_passed") or metadata.get("trade_filter_passed") or latest.get("scoring_eligible") or False),
        "missing_fields": list(latest.get("missing_fields") or metadata.get("missing_fields") or []),
        "candidate_fallback": bool(latest.get("candidate_fallback") or metadata.get("candidate_fallback") or False),
        "fallback_sources": list(latest.get("fallback_sources") or metadata.get("fallback_sources") or []),
        "mock_used": bool(latest.get("mock_used") or metadata.get("mock_used") or False),
        "mock_sources": list(latest.get("mock_sources") or metadata.get("mock_sources") or []),
        "degraded": bool(latest.get("degraded") or metadata.get("degraded") or False),
        "degradation_reasons": list(latest.get("degradation_reasons") or metadata.get("degradation_reasons") or []),
        "current_validation_status": str(latest.get("current_validation_status") or metadata.get("current_validation_status") or validation_status),
        "trade_admission_status": str(latest.get("trade_admission_status") or metadata.get("trade_admission_status") or "NOT_TRADABLE"),
    }


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
    parser = argparse.ArgumentParser(description="Run the AI selector")
    parser.add_argument(
        "--mode",
        choices=("fast_preliminary", "quality_refined", "full"),
        default=None,
        help="Selector run mode (default: full)",
    )
    parser.add_argument(
        "--universe-source",
        choices=("managed", "sample", "sp500"),
        default=None,
        help="Universe source (default: managed).  Overrides OPENALPHA_UNIVERSE env var.",
    )
    args = parser.parse_args()
    if args.universe_source:
        os.environ["OPENALPHA_UNIVERSE"] = args.universe_source
    main(mode=args.mode)
