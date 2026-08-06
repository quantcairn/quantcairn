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
from src.openalpha.selector import AIStrategySelector
from src.openalpha.settings import load_runtime_settings, resolve_price_band


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


def _current_manifest_path() -> Path:
    return PROJECT_DIR / "state" / "selection_bundle_manifest.json"


def _load_current_manifest() -> dict:
    path = _current_manifest_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _bundle_signature(report: dict, manifest: dict) -> dict[str, str] | None:
    run_id = str(manifest.get("selection_run_id") or report.get("selection_run_id") or "").strip()
    bundle_version = str(manifest.get("bundle_version") or report.get("selection_bundle_version") or "").strip()
    bundle_hash = str(manifest.get("selection_bundle_hash") or report.get("selection_bundle_hash") or "").strip()
    selection_date = str(manifest.get("selection_date") or report.get("selection_date") or "").strip()
    if not run_id or not bundle_version or not bundle_hash or not selection_date:
        return None
    return {
        "selection_run_id": run_id,
        "bundle_version": bundle_version,
        "bundle_hash": bundle_hash,
        "selection_date": selection_date,
    }


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


def _final_refined_symbols(rows: list[dict] | None) -> list[str]:
    return [
        str(item.get("ticker") or "").strip().upper()
        for item in rows or []
        if str(item.get("ticker") or "").strip()
    ]


def _prepare_refined_bundle(preliminary: dict, refined_summary: dict, final_rows: list[dict], *, selector: AIStrategySelector) -> dict:
    merged = _merge_refinement_summary(preliminary, refined_summary)
    final_rows = [dict(item or {}) for item in final_rows or []]
    selected_symbols = _final_refined_symbols(final_rows)
    requested_top_n = int(
        merged.get("requested_top_n")
        or (merged.get("settings") or {}).get("top_n")
        or len(final_rows)
        or 3
    )
    requested_top_n = max(1, requested_top_n)
    final_rows = list(final_rows[:requested_top_n])
    selected_symbols = _final_refined_symbols(final_rows)

    merged["preliminary_top5"] = list(preliminary.get("top5") or [])
    merged["preliminary_top3"] = list(preliminary.get("top3") or [])
    merged["preliminary_top10"] = list(preliminary.get("top10") or [])
    merged["top10"] = list(refined_summary.get("top10") or merged.get("top10") or [])
    merged["top5"] = list(final_rows)
    merged["top3"] = list(final_rows[:3])
    merged["report"] = list(selector._format_report_rows(final_rows))
    merged["selection_count"] = len(final_rows)
    merged["candidate_count"] = max(int(merged.get("candidate_count") or 0), len(final_rows))
    merged["requested_top_n"] = requested_top_n
    merged["selected_top_n"] = len(final_rows)
    merged["top_n_missing_count"] = max(0, requested_top_n - len(final_rows))
    merged["top_n_filled"] = len(final_rows) >= requested_top_n
    merged["selection_symbols"] = list(selected_symbols)
    merged["selected_symbols"] = list(selected_symbols)
    merged["configured_top_symbols"] = list(selected_symbols)
    merged["final_selected_symbols"] = list(selected_symbols)
    merged["selection_stage"] = str(preliminary.get("selection_stage") or "FINALIZED")
    merged["top_sync_status"] = str(preliminary.get("top_sync_status") or refined_summary.get("top_sync_status") or "OK")
    merged["top_sync_error"] = str(preliminary.get("top_sync_error") or refined_summary.get("top_sync_error") or "")
    merged["source_bundle_hash"] = str(preliminary.get("selection_bundle_hash") or "")
    merged["source_bundle_version"] = str(preliminary.get("selection_bundle_version") or "")
    merged["source_selection_run_id"] = str(preliminary.get("selection_run_id") or "")
    merged["source_selection_date"] = str(preliminary.get("selection_date") or "")
    merged["requested_top_n"] = requested_top_n
    merged["selected_top_n"] = len(final_rows)
    merged["top_slot_count"] = requested_top_n
    return merged


def _persist_refined_bundle(summary: dict, top_items: list[dict], *, selection_date: str) -> dict:
    selection_run_id = str(summary.get("selection_run_id") or "").strip()
    if not selection_run_id:
        return {}
    generated_at = str(summary.get("refined_at") or datetime.now().isoformat())
    requested_top_n = max(1, int(summary.get("requested_top_n") or 3))
    top_items = [dict(item or {}) for item in list(top_items or [])[:requested_top_n]]
    slot_count = requested_top_n
    selection_state_payload = {
        "et_date": selection_date,
        "generated_at": generated_at,
        "selected_symbols": _final_refined_symbols(top_items),
        "selection_symbols": _final_refined_symbols(top_items),
        "configured_top_symbols": _final_refined_symbols(top_items),
        "selection_stage": str(summary.get("selection_stage") or ""),
        "processing_phase": str(summary.get("processing_phase") or ""),
        "result_quality": str(summary.get("result_quality") or ""),
        "research_admission": str(summary.get("research_admission") or ""),
        "selection_run_id": selection_run_id,
        "top_sync_run_id": selection_run_id,
        "top_sync_status": str(summary.get("top_sync_status") or "OK"),
        "top_sync_error": str(summary.get("top_sync_error") or ""),
        "disabled_slots": list(range(len(top_items) + 1, slot_count + 1)),
        "requested_top_n": requested_top_n,
        "selected_top_n": len(top_items),
        "top_slot_count": requested_top_n,
        "enabled_slots": list(range(1, len(top_items) + 1)),
        "synced_at": generated_at,
        "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
        "selection_bundle_version": str(summary.get("selection_bundle_version") or "selection_bundle_v1"),
        "selection_bundle_hash": "",
        "report_path": str(_latest_report_path()),
    }
    return selector_runner.write_selection_bundle_atomic(
        summary=summary,
        selection_state_payload=selection_state_payload,
        top_items=list(top_items),
        selection_run_id=selection_run_id,
        selection_date=selection_date,
        generated_at=generated_at,
        result_quality=str(summary.get("result_quality") or ""),
        research_admission=str(summary.get("research_admission") or ""),
        processing_phase=str(summary.get("processing_phase") or ""),
        requested_top_n=requested_top_n,
        top_sync_status=str(summary.get("top_sync_status") or "OK"),
        top_sync_error=str(summary.get("top_sync_error") or ""),
    )


def main() -> None:
    expected_timestamp = str(os.environ.get("OPENALPHA_EXPECTED_TIMESTAMP") or "").strip()
    latest = _load_latest_report()
    if not latest:
        return
    if expected_timestamp and str(latest.get("timestamp") or "").strip() != expected_timestamp:
        return
    source_manifest = _load_current_manifest()
    source_signature = _bundle_signature(latest, source_manifest)
    if not source_signature:
        return

    runtime_settings = load_runtime_settings()
    min_price, max_price = resolve_price_band(runtime_settings)
    os.environ.setdefault("OPENALPHA_MIN_PRICE", str(min_price))
    os.environ.setdefault("OPENALPHA_MAX_PRICE", str(max_price))
    os.environ.setdefault(
        "OPENALPHA_AUTO_REFRESH_MINUTES",
        str(runtime_settings.get("auto_refresh_minutes", 5)),
    )
    configured_max_symbols = int(runtime_settings.get("max_symbols", 20) or 20)
    os.environ.setdefault("OPENALPHA_MAX_SYMBOLS", str(max(5, min(configured_max_symbols, 20))))
    os.environ.setdefault("OPENALPHA_FETCH_NEWS", "0")
    os.environ.setdefault("OPENALPHA_ALLOW_PROXY_MARKET", "0")
    os.environ.setdefault("OPENALPHA_DIRECT_HISTORY", "1")
    os.environ.setdefault("OPENALPHA_SKIP_YFINANCE_HISTORY", "1")
    os.environ.setdefault("OPENALPHA_HTTP_TIMEOUT_SECONDS", "2")
    os.environ.setdefault("OPENALPHA_FILTER_CANDIDATE_LIMIT", "20")
    os.environ.setdefault("OPENALPHA_TOTAL_BUDGET_SECONDS", "60")
    os.environ.setdefault("OPENALPHA_QUALITY_BUDGET_SECONDS", "30")
    os.environ.pop("OPENALPHA_FAST_START_ONLY", None)

    selector = AIStrategySelector()
    requested_top_n = int(
        latest.get("requested_top_n")
        or (latest.get("settings") or {}).get("top_n")
        or selector.selection_size
        or 3
    )
    requested_top_n = max(1, requested_top_n)
    refined = selector.run_selection(write_configs=False)
    live_positions = selector_runner._live_equity_positions() or []
    refined["top10"] = selector_runner._merge_live_position_flags(list(refined.get("top10") or []), live_positions)
    refined_seed = list(refined.get("top5") or refined.get("top3") or [])
    if not refined_seed:
        return
    refined_selected = selector_runner._pin_live_positions(
        refined_seed,
        live_positions,
        limit=requested_top_n,
    )
    preliminary_selected = list(latest.get("top5") or latest.get("top3") or [])
    merged_selected = _merge_refined_candidates(preliminary_selected, refined_selected, limit=requested_top_n)
    merged_selected = [row for row in merged_selected if selector_runner.is_formal_selection_eligible(row)]
    current_signature = _bundle_signature(_load_latest_report(), _load_current_manifest())
    if current_signature != source_signature:
        return
    merged = _prepare_refined_bundle(latest, refined, merged_selected, selector=selector)
    _persist_refined_bundle(merged, merged_selected, selection_date=str(latest.get("selection_date") or ""))


if __name__ == "__main__":
    main()
