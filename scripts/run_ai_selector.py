#!/usr/bin/env python3
"""Daily AI stock selector runner

Usage: scripts/run_ai_selector.py
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_DIR / "reports"
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _selection_date() -> str:
    return _et_now().date().isoformat()


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
    """Return current long equity positions; options are managed outside TOP5."""
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


def _pin_live_positions(selected: list[dict], positions: list[dict], limit: int = 5) -> list[dict]:
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

    sel = AIStrategySelector()
    out = sel.run_selection(write_configs=False)
    out["top10"] = _merge_live_position_flags(list(out.get("top10") or []), live_positions or [])
    selected = out.get('top5') or out.get('top3') or []
    selected = _pin_live_positions(
        selected,
        live_positions or [],
        limit=sel.selection_size,
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
        out["top5"] = selected
        out["top3"] = selected[:3]
        out["report"] = sel._format_report_rows(selected)
    timestamp = datetime.now().isoformat()
    print(f"AI selection completed at {timestamp}")
    print("Top10:")
    for i, t in enumerate(out['top10'], start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")
    print("Top5:")
    for i, t in enumerate(selected, start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")

    # Send notifications: webhook (env AI_SELECTOR_WEBHOOK) and macOS notification
    webhook = os.environ.get('AI_SELECTOR_WEBHOOK')
    summary = {
        'timestamp': timestamp,
        'generated_at': timestamp,
        'selection_date': _selection_date(),
        'providers_used': ["selector_core", "yfinance"],
        'providers_disabled': ["fmp"],
        'fmp_enabled': False,
        'top10': out.get('top10', []),
        'top5': selected,
        'top3': out.get('top3', []),
        'protected_positions': [
            {
                "ticker": str(item.get("ticker") or "").upper(),
                "protected_position": True,
                "reduce_only": bool(item.get("reduce_only", False)),
            }
            for item in selected
            if item.get("protected_position") or item.get("existing_position")
        ],
        'fallback_used': any(
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
            top5tickers = ', '.join([t['ticker'] for t in selected])
            msg = f"Top5: {top5tickers} (非成交提醒)"
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
