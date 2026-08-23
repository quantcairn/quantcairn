#!/usr/bin/env python3
"""Generate a daily markdown + JSON trade audit report from logs/trades-YYYYMMDD.jsonl."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]

import sys

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.reports.trade_audit import summarize_trade_log
from src.config.runtime_paths import resolve_logs_dir, resolve_reports_dir


def _fmt_money(value: object) -> str:
    try:
        return f"${float(value):.2f}"
    except Exception:
        return "N/A"


def _build_markdown(report: dict[str, object], day: str) -> str:
    latest_execution = report.get("latest_execution") if isinstance(report, dict) else {}
    latest_line = report.get("latest_line", "None") if isinstance(report, dict) else "None"
    rows = [
        f"# Trade Audit Report {day}",
        "",
        f"- Execution Mode: `{report.get('execution_mode', 'paper')}`",
        f"- Reduce Only: `{report.get('reduce_only', False)}`",
        f"- New Entries: `{report.get('new_entries_allowed', True)}`",
        f"- Pause Reason: `{report.get('risk_pause_reason', '') or 'None'}`",
        f"- Decisions: `{report.get('decision_count', 0)}`",
        f"- Executions: `{report.get('execution_count', 0)}`",
        f"- Buy Orders: `{report.get('buy_count', 0)}`",
        f"- Sell Orders: `{report.get('sell_count', 0)}`",
        f"- Total Qty: `{report.get('order_qty', 0)}`",
        f"- Latest Trade: `{latest_line}`",
        "",
        "## Latest Execution",
    ]
    if isinstance(latest_execution, dict) and latest_execution:
        order = latest_execution.get("order", {}) if isinstance(latest_execution.get("order"), dict) else {}
        response = latest_execution.get("response", {}) if isinstance(latest_execution.get("response"), dict) else {}
        rows.extend([
            f"- Ticker: `{latest_execution.get('ticker', '')}`",
            f"- Strategy: `{latest_execution.get('strategy', '')}`",
            f"- Action: `{order.get('side', '')}`",
            f"- Quantity: `{order.get('qty', '')}`",
            f"- Response Status: `{response.get('status', response.get('ok', ''))}`",
        ])
    else:
        rows.append("- None")
    rows.extend([
        "",
        "## Tickers",
        f"- {', '.join(report.get('tickers', []) or []) or 'None'}",
    ])
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Target trading date in YYYYMMDD format. Defaults to today.")
    parser.add_argument("--log-dir", default=str(resolve_logs_dir(PROJECT_DIR)), help="Directory containing trades-YYYYMMDD.jsonl.")
    parser.add_argument("--output-dir", default=str(resolve_reports_dir(PROJECT_DIR)), help="Directory for report outputs.")
    args = parser.parse_args()

    day = args.date or datetime.now().strftime("%Y%m%d")
    report = summarize_trade_log(args.log_dir, day=day)
    report["date"] = day

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"trade-report-{day}.json"
    md_path = output_dir / f"trade-report-{day}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_build_markdown(report, day), encoding="utf-8")

    print(f"Generated: {json_path}")
    print(f"Generated: {md_path}")
    print(f"Latest trade: {report.get('latest_line', 'None')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
