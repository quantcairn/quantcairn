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

from src.ai_selector.settings import load_runtime_settings

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_DIR / "reports"
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")


def _write_reports(summary: dict):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    latest_json = REPORTS_DIR / "ai_selection_latest.json"
    dated_json = REPORTS_DIR / f"ai_selection_{datetime.now().strftime('%Y%m%d')}.json"
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    latest_json.write_text(payload, encoding="utf-8")
    dated_json.write_text(payload, encoding="utf-8")


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
    runtime_settings = load_runtime_settings()
    os.environ.setdefault("AI_SELECTOR_MIN_PRICE", str(runtime_settings.get("min_price", 10.0)))
    os.environ.setdefault("AI_SELECTOR_MAX_PRICE", str(runtime_settings.get("max_price", 200.0)))
    os.environ.setdefault(
        "AI_SELECTOR_AUTO_REFRESH_MINUTES",
        str(runtime_settings.get("auto_refresh_minutes", 5)),
    )
    live_positions = _live_equity_positions()
    if live_positions is None and _has_live_top_configs():
        print("Live position verification failed; refusing to run selection or replace TOP configs.")
        sys.exit(1)

    sel = AIStrategySelector()
    out = sel.run_selection(write_configs=False)
    selected = out.get('top5') or out.get('top3') or []
    selected = _pin_live_positions(
        selected,
        live_positions or [],
        limit=sel.selection_size,
    )
    if selected:
        from src.ai_selector.config_writer import write_top_configs

        write_top_configs(selected)
        out["top5"] = selected
        out["top3"] = selected[:3]
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
        'top10': out.get('top10', []),
        'top5': selected,
        'top3': out.get('top3', []),
        'report': out.get('report', []),
        'settings': out.get('settings', {}),
    }

    _write_reports(summary)

    restart_code = _restart_top_engines()
    if restart_code != 0:
        print(f"TOP restart failed with exit code {restart_code}.")
        sys.exit(restart_code)

    if webhook:
        try:
            requests.post(webhook, json=summary, timeout=5)
        except Exception:
            print('Failed to send webhook notification')

    # macOS notification (optional)
    try:
        top5tickers = ', '.join([t['ticker'] for t in selected])
        msg = f"Top5: {top5tickers} (非成交提醒)"
        subprocess.run(['osascript', '-e', f'display notification "{msg}" with title "AI 选股更新"' ], check=False)
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
