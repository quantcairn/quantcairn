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
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_DIR / "reports"


def _write_reports(summary: dict):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    latest_json = REPORTS_DIR / "ai_selection_latest.json"
    dated_json = REPORTS_DIR / f"ai_selection_{datetime.now().strftime('%Y%m%d')}.json"
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    latest_json.write_text(payload, encoding="utf-8")
    dated_json.write_text(payload, encoding="utf-8")

def main():
    sel = AIStrategySelector()
    out = sel.run_selection()
    timestamp = datetime.now().isoformat()
    print(f"AI selection completed at {timestamp}")
    print("Top10:")
    for i, t in enumerate(out['top10'], start=1):
        print(f"{i}. {t['ticker']} — {t['score']}")
    selected = out.get('top5') or out.get('top3') or []
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
    }

    _write_reports(summary)

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

if __name__ == '__main__':
    main()
