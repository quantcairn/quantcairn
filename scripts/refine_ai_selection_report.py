#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts import run_ai_selector as selector_runner
from src.ai_selector.selector import AIStrategySelector
from src.ai_selector.settings import load_runtime_settings, resolve_price_band


def _latest_report_path() -> Path:
    return PROJECT_DIR / "reports" / "ai_selection_latest.json"


def _load_latest_report() -> dict:
    path = _latest_report_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_refined_report(summary: dict) -> None:
    selector_runner._write_reports(summary)


def _merge_refined_candidates(preliminary_rows: list[dict] | None, refined_rows: list[dict] | None, *, limit: int) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()

    def _append_rows(rows: list[dict] | None) -> None:
        for row in rows or []:
            if len(merged) >= limit:
                return
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker or ticker in seen:
                continue
            merged.append(dict(row))
            seen.add(ticker)

    _append_rows(refined_rows)
    _append_rows(preliminary_rows)
    return merged


def _merge_refinement_summary(preliminary: dict, refined_summary: dict) -> dict:
    merged = dict(preliminary)
    refinement_stage = str((refined_summary.get("settings") or {}).get("selection_stage") or "")
    refinement_status = "quality_refined" if refinement_stage and refinement_stage != "fast_preliminary" else "background_fast_preliminary"
    merged["refinement_status"] = refinement_status
    merged["refined_at"] = datetime.now().isoformat()
    merged["refined_top5"] = list(refined_summary.get("top5") or [])
    merged["refined_top3"] = list(refined_summary.get("top3") or [])
    merged["refined_report"] = list(refined_summary.get("report") or [])
    merged["refined_top10"] = list(refined_summary.get("top10") or [])
    merged["refined_quality_filter_report"] = dict(refined_summary.get("quality_filter_report") or {})
    merged["refinement_selection_stage"] = refinement_stage
    return merged


def main() -> None:
    expected_timestamp = str(os.environ.get("AI_SELECTOR_EXPECTED_TIMESTAMP") or "").strip()
    latest = _load_latest_report()
    if not latest:
        return
    if expected_timestamp and str(latest.get("timestamp") or "").strip() != expected_timestamp:
        return

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
    os.environ.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    os.environ.setdefault("AI_SELECTOR_ALLOW_PROXY_MARKET", "0")
    os.environ.setdefault("AI_SELECTOR_DIRECT_HISTORY", "1")
    os.environ.setdefault("AI_SELECTOR_SKIP_YFINANCE_HISTORY", "1")
    os.environ.setdefault("AI_SELECTOR_HTTP_TIMEOUT_SECONDS", "2")
    os.environ.setdefault("AI_SELECTOR_FILTER_CANDIDATE_LIMIT", "20")
    os.environ.setdefault("AI_SELECTOR_TOTAL_BUDGET_SECONDS", "30")
    os.environ.setdefault("AI_SELECTOR_QUALITY_BUDGET_SECONDS", "20")
    os.environ.pop("AI_SELECTOR_FAST_START_ONLY", None)

    selector = AIStrategySelector()
    refined = selector.run_selection(write_configs=False)
    live_positions = selector_runner._live_equity_positions() or []
    refined["top10"] = selector_runner._merge_live_position_flags(list(refined.get("top10") or []), live_positions)
    refined_selected = selector_runner._pin_live_positions(
        refined.get("top5") or refined.get("top3") or [],
        live_positions,
        limit=selector.selection_size,
    )
    preliminary_selected = list(latest.get("top5") or latest.get("top3") or [])
    merged_selected = _merge_refined_candidates(preliminary_selected, refined_selected, limit=selector.selection_size)
    if not merged_selected:
        return
    refined["top5"] = merged_selected
    refined["top3"] = merged_selected[:3]
    refined["report"] = selector._format_report_rows(merged_selected)

    merged = _merge_refinement_summary(latest, refined)
    _write_refined_report(merged)


if __name__ == "__main__":
    main()
