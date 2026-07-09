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
from src.ai_selector.composition_filter import CompositionFilter
from src.ai_selector.selector import AIStrategySelector
from src.ai_selector.range_score import RangeFitnessScorer
from src.ai_selector.trade_filter import TradeEligibilityFilter
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
from src.data.fetcher import PriceFetcher
from src.notifier.alerts import notify_ai_selection_result

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
    return _et_now().date().isoformat()


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
        "providers_used": list(metadata.get("providers_used") or []),
        "providers_disabled": list(metadata.get("providers_disabled") or []),
        "fmp_enabled": bool(metadata.get("fmp_enabled", False)),
        "provider_fallback_used": bool(metadata.get("provider_fallback_used", False)),
        "fallback_used": bool(metadata.get("fallback_used", False)),
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
    selected = _apply_range_scores(selected)
    price_band_rejected_rows: list[dict] = []
    report_top10 = _apply_range_scores(_build_report_top10(
        list(out.get("top10") or []),
        list(selected),
        integrated_ai.get("signal_map") or {},
        live_positions or [],
    ))
    report_top10, report_price_band_rejected = _finalize_price_band(report_top10, min_price, max_price)
    price_band_rejected_rows.extend(report_price_band_rejected)
    candidate_pool = _annotate_with_ai_signals(list(report_top10 or []), integrated_ai.get("signal_map") or {})
    if integrated_ai.get("preferred_symbols"):
        candidate_pool = _prioritize_ai_rank(candidate_pool, integrated_ai.get("signal_map") or {})
    selected, protected_positions = _split_selected_and_protected_positions(
        candidate_pool,
        live_positions or [],
        limit=min(sel.selection_size, TOP_COUNT),
    )
    selection_stage = str((out.get("settings") or {}).get("selection_stage") or "")
    min_price, max_price = resolve_price_band(runtime_settings)
    fallback_pool_used = False
    trade_filter_report: dict = {"rejected": [], "fallback_used": False}
    fallback_trade_report: dict = {"rejected": [], "fallback_used": False}
    composition_filter_report: dict = {"rejected": [], "warnings": []}

    selected, initial_price_band_rejected = _finalize_price_band(selected, min_price, max_price)
    price_band_rejected_rows.extend(initial_price_band_rejected)
    selected = _apply_range_scores(_annotate_with_ai_signals(selected, integrated_ai.get("signal_map") or {}))

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
                fallback_price_band = f"${min_price:.2f}-${max_price:.2f}"
                fallback_candidates = _apply_range_scores(fallback_candidates)
                fallback_candidates, fallback_trade_report = _apply_trade_filter(fallback_candidates)
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
                    if item_price is None or item_price < min_price or item_price > max_price:
                        reason = "price_missing" if item_price is None else "price_out_of_range"
                        fallback_trade_report.setdefault("rejected", []).append(
                            {
                                "ticker": ticker,
                                "reason": reason,
                                "price": round(float(item_price), 4) if item_price is not None else None,
                                "min_price": float(min_price),
                                "max_price": float(max_price),
                                "allowed_range": fallback_price_band,
                                "source": "conservative_fallback_pool",
                            }
                        )
                        price_band_rejected_rows.append(
                            {
                                "ticker": ticker,
                                "reason": reason,
                                "price": round(float(item_price), 4) if item_price is not None else None,
                                "min_price": float(min_price),
                                "max_price": float(max_price),
                                "allowed_range": fallback_price_band,
                                "source": "conservative_fallback_pool",
                            }
                        )
                        continue
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
                    selected.append(item)
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

    selected, final_price_band_rejected = _finalize_price_band(selected, min_price, max_price)
    price_band_rejected_rows.extend(final_price_band_rejected)
    selected = [_normalize_entry_report_fields(item) for item in selected]
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
    quality_report["trade_filter_passed"] = [bool(item.get("trade_filter_passed", False)) for item in selected]
    quality_report["reject_reason"] = [str(item.get("reject_reason") or "") for item in selected]
    trade_filter_rejected = list(trade_filter_report.get("rejected") or [])
    trade_filter_rejected.extend(list(fallback_trade_report.get("rejected") or []) if fallback_pool_used else [])
    quality_report["trade_filter_rejected"] = trade_filter_rejected
    quality_report["fallback_used"] = bool(trade_filter_report.get("fallback_used", False)) or bool(integrated_ai.get("fallback_used")) or bool(fallback_pool_used)
    quality_report["fallback_pool_used"] = bool(fallback_pool_used)
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
    out["settings"]["min_price"] = float(min_price)
    out["settings"]["max_price"] = float(max_price)
    out["settings"]["price_band"] = {"min": float(min_price), "max": float(max_price)}
    out["settings"]["selection_stage"] = selection_stage
    out["settings"]["entry_proximity_enabled"] = bool(ENTRY_PROXIMITY_ENABLED)
    out["settings"]["entry_proximity_weight"] = float(ENTRY_PROXIMITY_WEIGHT)
    write_selection_filter_log(quality_report)
    if not selected:
        print("AI selection produced no tradable symbols; aborting without updating TOP configs.")
        sys.exit(1)
    if selected:
        from src.ai_selector.config_writer import write_top_configs
        for item in selected:
            item["selection_date"] = _selection_date()
            item["protected_position"] = bool(item.get("protected_position") or item.get("existing_position"))
            item["trade_filter_passed"] = bool(item.get("trade_filter_passed", False))
            item["reject_reason"] = str(item.get("reject_reason") or "")
            item["fallback_used"] = bool(item.get("fallback_used", False)) or bool(trade_filter_report.get("fallback_used", False))
            item["leveraged_etf"] = bool(item.get("leveraged_etf", False))
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
    timestamp = datetime.now().isoformat()
    print(f"AI selection completed at {timestamp}")
    print("Top10:")
    for i, t in enumerate(out['top10'], start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")
    print("Top3:")
    for i, t in enumerate(selected, start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")

    providers_used, providers_disabled, fmp_enabled = _provider_metadata(out, live_positions, integrated_ai)
    summary = {
        'timestamp': timestamp,
        'generated_at': timestamp,
        'selection_date': _selection_date(),
        'providers_used': providers_used,
        'providers_disabled': providers_disabled,
        'fmp_enabled': fmp_enabled,
        'provider_fallback_used': bool(integrated_ai.get("provider_fallback_used", False)),
        'top10': out.get('top10', []),
        'top5': list(selected),
        'top3': list(out.get('top3', [])),
        'selection_count': len(selected),
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
        'fallback_used': bool(integrated_ai.get("fallback_used")) or bool(fallback_pool_used) or any(
            bool(item.get("fallback_history_incomplete"))
            or str(item.get("selection_penalty_reason") or "").startswith("quality_filter_backfill")
            for item in selected
        ) or bool(trade_filter_report.get("fallback_used", False)),
        'report': out.get('report', []),
        'settings': out.get('settings', {}),
        'quality_filter_report': out.get('quality_filter_report', {}),
        'composition_filter': {
            'max_leveraged_etf_in_top3': 1,
            'rejected': list(composition_filter_report.get("rejected") or []),
            'warnings': list(composition_filter_report.get("warnings") or []),
        },
    }

    latest_report_path, _ = _write_reports(summary)
    write_selection_state(
        et_date=_et_now().date().isoformat(),
        generated_at=timestamp,
        selected_symbols=[str(item.get("ticker") or "").strip().upper() for item in selected],
        report_path=str(latest_report_path),
    )

    final_top_configs = _load_final_top_configs(TOP_COUNT)
    _notify_selection_result(summary, final_top_configs or selected)

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
