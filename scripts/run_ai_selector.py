#!/usr/bin/env python3
"""Daily AI stock selector runner

Usage: scripts/run_ai_selector.py
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.ai_selector.integration import AISelector
from src.ai_selector.selector import AIStrategySelector
from datetime import datetime
import os
import json
import requests
import subprocess
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config.local_env import load_local_ai_env
from src.ai_selector.settings import load_runtime_settings
from src.ai_selector.selector import write_selection_filter_log
from src.ai_selector.selection_state import write_selection_state
from src.ai_selector.config import load_runtime_config

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_DIR / "reports"
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")
TOP_COUNT = max(1, int(load_runtime_config().top_n))


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
        "fallback_used": bool(metadata.get("fallback_used", False)),
    }


def _annotate_with_ai_signals(rows: list[dict], signal_map: dict[str, dict]) -> list[dict]:
    annotated = []
    for raw in rows or []:
        item = dict(raw)
        ticker = _normalize_ticker(item.get("ticker"))
        ai_signal = dict(signal_map.get(ticker) or {})
        if ai_signal:
            item["ai_score"] = float(ai_signal.get("score") or 0.0)
            item["confidence"] = float(ai_signal.get("confidence") or item.get("confidence") or 0.0)
            item["reason"] = str(ai_signal.get("reason") or item.get("reason") or "")
            item["source"] = str(ai_signal.get("source") or item.get("source") or "ai_selector")
        annotated.append(item)
    return annotated


def _build_report_top10(
    selector_top10: list[dict],
    selected: list[dict],
    signal_map: dict[str, dict],
    live_positions: list[dict] | None,
) -> list[dict]:
    candidates = list(selector_top10 or [])
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
        ai_score = float((signal_map.get(ticker) or {}).get("score") or -1.0)
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
    env.setdefault("AI_SELECTOR_SKIP_YFINANCE_HISTORY", "1")
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

def main():
    load_local_ai_env()
    runtime_settings = load_runtime_settings()
    os.environ.setdefault("AI_SELECTOR_MIN_PRICE", str(runtime_settings.get("min_price", 10.0)))
    os.environ.setdefault("AI_SELECTOR_MAX_PRICE", str(runtime_settings.get("max_price", 200.0)))
    os.environ.setdefault(
        "AI_SELECTOR_AUTO_REFRESH_MINUTES",
        str(runtime_settings.get("auto_refresh_minutes", 5)),
    )
    configured_max_symbols = int(runtime_settings.get("max_symbols", 20) or 20)
    os.environ.setdefault("AI_SELECTOR_MAX_SYMBOLS", str(max(5, min(configured_max_symbols, 20))))
    os.environ.setdefault("AI_SELECTOR_ALLOW_PROXY_MARKET", "0")
    os.environ.setdefault("AI_SELECTOR_DIRECT_HISTORY", "1")
    os.environ.setdefault("AI_SELECTOR_SKIP_YFINANCE_HISTORY", "1")
    os.environ.setdefault("AI_SELECTOR_HTTP_TIMEOUT_SECONDS", "3")
    live_positions = _live_equity_positions()
    if live_positions is None and _has_live_top_configs():
        print("Live position verification failed; refusing to run selection or replace TOP configs.")
        sys.exit(1)

    integrated_ai = _run_integrated_ai_selector()
    preferred_symbols = integrated_ai.get("preferred_symbols") or None
    sel = AIStrategySelector()
    out = sel.run_selection(write_configs=False, symbols_override=preferred_symbols)
    selected = out.get('top3') or out.get('top5') or []
    if not selected and preferred_symbols:
        integrated_ai["fallback_used"] = True
        out = sel.run_selection(write_configs=False)
        selected = out.get('top3') or out.get('top5') or []

    selected = _annotate_with_ai_signals(list(selected or []), integrated_ai.get("signal_map") or {})[:TOP_COUNT]
    if integrated_ai.get("preferred_symbols"):
        selected = _prioritize_ai_rank(selected, integrated_ai.get("signal_map") or {})
    selected = _pin_live_positions(
        selected,
        live_positions or [],
        limit=min(sel.selection_size, TOP_COUNT),
    )
    preserved_positions = [
        str(item.get("ticker") or "").upper()
        for item in selected
        if item.get("existing_position")
    ]
    quality_report = dict(out.get("quality_filter_report") or {})
    quality_report["final_selected_symbols"] = [
        str(item.get("ticker") or "").upper() for item in selected
    ]
    quality_report["existing_real_positions_preserved"] = preserved_positions
    out["quality_filter_report"] = quality_report
    out["top10"] = _build_report_top10(
        list(out.get("top10") or []),
        list(selected),
        integrated_ai.get("signal_map") or {},
        live_positions or [],
    )
    write_selection_filter_log(quality_report)
    if not selected:
        print("AI selection produced no tradable symbols; aborting without updating TOP configs.")
        sys.exit(1)
    if selected:
        from src.ai_selector.config_writer import write_top_configs
        for item in selected:
            item["selection_date"] = _selection_date()
            item["protected_position"] = bool(item.get("protected_position") or item.get("existing_position"))
        write_top_configs(selected)
        selected = list(selected[:TOP_COUNT])
        out["top5"] = list(selected)
        out["top3"] = list(selected)
        out["report"] = sel._format_report_rows(selected)
    timestamp = datetime.now().isoformat()
    print(f"AI selection completed at {timestamp}")
    print("Top10:")
    for i, t in enumerate(out['top10'], start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")
    print("Top3:")
    for i, t in enumerate(selected, start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")

    # Send notifications: webhook (env AI_SELECTOR_WEBHOOK) and macOS notification
    webhook = os.environ.get('AI_SELECTOR_WEBHOOK')
    providers_used, providers_disabled, fmp_enabled = _provider_metadata(out, live_positions, integrated_ai)
    summary = {
        'timestamp': timestamp,
        'generated_at': timestamp,
        'selection_date': _selection_date(),
        'providers_used': providers_used,
        'providers_disabled': providers_disabled,
        'fmp_enabled': fmp_enabled,
        'top10': out.get('top10', []),
        'top5': list(selected),
        'top3': list(out.get('top3', [])),
        'protected_positions': [
            {
                "ticker": str(item.get("ticker") or "").upper(),
                "protected_position": True,
                "reduce_only": bool(item.get("reduce_only", False)),
            }
            for item in selected
            if item.get("protected_position") or item.get("existing_position")
        ],
        'fallback_used': bool(integrated_ai.get("fallback_used")) or any(
            bool(item.get("existing_position"))
            or bool(item.get("fallback_history_incomplete"))
            or str(item.get("selection_penalty_reason") or "").startswith("quality_filter_backfill")
            for item in selected
        ),
        'report': out.get('report', []),
        'settings': out.get('settings', {}),
        'quality_filter_report': out.get('quality_filter_report', {}),
    }

    latest_report_path, _ = _write_reports(summary)
    write_selection_state(
        et_date=_et_now().date().isoformat(),
        generated_at=timestamp,
        selected_symbols=[str(item.get("ticker") or "").strip().upper() for item in selected],
        report_path=str(latest_report_path),
    )

    restart_code = _restart_top_engines()
    if restart_code != 0:
        print(f"TOP restart failed with exit code {restart_code}.")
        sys.exit(restart_code)

    if str((summary.get("settings") or {}).get("selection_stage") or "") == "fast_preliminary":
        _spawn_background_refinement(timestamp)

    if webhook:
        try:
            requests.post(webhook, json=summary, timeout=5)
        except Exception:
            print('Failed to send webhook notification')

    # macOS notification is optional and must never block the selector.
    if os.environ.get("AI_SELECTOR_MAC_NOTIFY", "0") == "1":
        try:
            top3tickers = ', '.join([t['ticker'] for t in selected])
            msg = f"Top3: {top3tickers} (非成交提醒)"
            subprocess.run(
                ['osascript', '-e', f'display notification "{msg}" with title "AI 选股更新"'],
                check=False,
                timeout=2,
            )
        except Exception:
            pass


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

if __name__ == '__main__':
    main()
