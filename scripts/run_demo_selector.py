#!/usr/bin/env python3
"""Run the QuantCairn selection pipeline in DEMO mode.

Uses deterministic synthetic OHLCV data — no API keys, no network,
no broker connection required.  Results are fully reproducible.

Usage::

    .venv/bin/python scripts/run_demo_selector.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# Banner
# ═══════════════════════════════════════════════════════════════════════════════

DEMO_BANNER = r"""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   ██████╗ ██╗   ██╗ █████╗ ███╗   ██╗████████╗ ██████╗       ║
║  ██╔═══██╗██║   ██║██╔══██╗████╗  ██║╚══██╔══╝██╔════╝       ║
║  ██║   ██║██║   ██║███████║██╔██╗ ██║   ██║   ██║            ║
║  ██║▄▄ ██║██║   ██║██╔══██║██║╚██╗██║   ██║   ██║            ║
║  ╚██████╔╝╚██████╔╝██║  ██║██║ ╚████║   ██║   ╚██████╗       ║
║   ╚══▀▀═╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝       ║
║                                                               ║
║      ██████╗ █████╗ ██╗██████╗ ███╗   ██╗                     ║
║     ██╔════╝██╔══██╗██║██╔══██╗████╗  ██║                     ║
║     ██║     ███████║██║██████╔╝██╔██╗ ██║                     ║
║     ██║     ██╔══██║██║██╔══██╗██║╚██╗██║                     ║
║     ╚██████╗██║  ██║██║██║  ██║██║ ╚████║                     ║
║      ╚═════╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝                     ║
║                                                               ║
║               AI Research Pipeline — DEMO MODE                 ║
╚═══════════════════════════════════════════════════════════════╝
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _status_icon(ok: bool) -> str:
    return "✅ PASS" if ok else "⚠️  WARN"


def _stage_passed(tracker, stage_name: str) -> bool:
    for rec in tracker.records:
        if rec.stage == stage_name:
            return rec.output_count > 0
    return False


def _safe_score(item: dict[str, Any]) -> float:
    val = item.get("score") or item.get("base_score") or item.get("candidate_score") or 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _candidate_type_label(item: dict[str, Any]) -> str:
    """Return the research status label for a candidate."""
    ct = str(item.get("candidate_type") or "RESEARCH_ONLY")
    return ct


# ═══════════════════════════════════════════════════════════════════════════════
# Artifact generation
# ═══════════════════════════════════════════════════════════════════════════════

def _write_demo_artifacts(
    result: dict[str, Any],
    tracker,
    artifacts_dir: Path,
) -> tuple[Path, Path]:
    """Write demo selection artifacts.  Never overwrites production artifacts."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = result.get("selection_run_id", "demo-unknown")

    # ── JSON artifact ─────────────────────────────────────────────────────
    json_payload = {
        "mode": "DEMO",
        "generated_at": timestamp,
        "selection_run_id": run_id,
        "universe": result.get("universe_source", "demo"),
        "universe_symbol_count": result.get("universe_symbol_count", 5),
        "pipeline_rate": result.get("selection_funnel", {}).get("pipeline_success_rate", 0),
        "run_mode": result.get("run_mode", "DEMO"),
        "candidate_type": result.get("candidate_type", "RESEARCH_ONLY"),
        "quality_fallback_active": result.get("quality_fallback_active", False),
        "formal_candidates": result.get("formal_candidates", []),
        "preview_candidates": result.get("preview_candidates", []),
        "top_candidates": [
            {
                "rank": i,
                "ticker": item.get("ticker", "?"),
                "score": _safe_score(item),
                "sector": item.get("sector", "Unknown"),
                "candidate_type": item.get("candidate_type", "RESEARCH_ONLY"),
                "data_source": item.get("data_source", "demo"),
                "recommended_strategy": item.get("recommended_strategy"),
            }
            for i, item in enumerate(result.get("top5", []), 1)
        ],
        "funnel_stages": [
            {
                "stage": rec.stage,
                "input_count": rec.input_count,
                "output_count": rec.output_count,
                "eliminated": rec.eliminated_count,
                "status": rec.status,
            }
            for rec in tracker.records
        ],
        "safety": {
            "execution": "DISABLED",
            "trading": "NOT AVAILABLE IN DEMO MODE",
            "broker": "NOT CONNECTED",
            "allow_live_order": False,
            "reduce_only": True,
        },
    }
    json_path = artifacts_dir / "demo_selection.json"
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # ── Markdown report ───────────────────────────────────────────────────
    md_lines = [
        f"# QuantCairn Demo — AI Research Pipeline",
        f"",
        f"> Generated: {timestamp}  ",
        f"> Run ID: `{run_id}`  ",
        f"> Mode: **DEMO**  ",
        f"",
        f"## Pipeline Summary",
        f"",
        f"| Stage | Input | Output | Eliminated | Status |",
        f"|-------|-------|--------|-----------|--------|",
    ]
    for rec in tracker.records:
        icon = "✅" if rec.status == "PASS" else "⚠️"
        md_lines.append(
            f"| {rec.stage} | {rec.input_count} | {rec.output_count} "
            f"| {rec.eliminated_count} | {icon} {rec.status} |"
        )

    md_lines.extend([
        f"",
        f"## TOP Candidates",
        f"",
    ])
    for i, item in enumerate(result.get("top5", []), 1):
        ticker = item.get("ticker", "?")
        score = _safe_score(item)
        sector = item.get("sector", "Unknown")
        reason = item.get("score_reason", "")
        ct = _candidate_type_label(item)
        md_lines.extend([
            f"### {i}. {ticker}",
            f"- **Score**: {score:.1f}",
            f"- **Sector**: {sector}",
            f"- **Research Status**: {ct}",
            f"- **Data Source**: {item.get('data_source', 'demo')}",
        ])
        if reason:
            md_lines.append(f"- **Reason**: {reason}")
        md_lines.append("")

    md_lines.extend([
        f"## Safety",
        f"",
        f"- **Execution**: DISABLED  ",
        f"- **Trading**: NOT AVAILABLE IN DEMO MODE  ",
        f"- **Broker**: NOT CONNECTED  ",
        f"",
        f"---",
        f"*Generated by QuantCairn Demo Pipeline. No API keys required.*",
    ])
    md_path = artifacts_dir / "demo_report.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    return json_path, md_path


# ═══════════════════════════════════════════════════════════════════════════════
# Printing
# ═══════════════════════════════════════════════════════════════════════════════

def _print_pipeline_status(tracker) -> None:
    """Print a compact pipeline stage status table."""
    print()
    print("─" * 55)
    print(f"  {'Stage':<28s} {'Status':>10s}")
    print("─" * 55)
    for rec in tracker.records:
        icon = _status_icon(rec.output_count > 0)
        print(f"  {rec.stage.replace('_', ' ').title():<28s} {icon:>10s}")
    print("─" * 55)


def _print_candidates(result: dict[str, Any]) -> None:
    """Print formatted candidate table with research status."""
    top_items = result.get("top5", [])
    print()
    print("─" * 55)
    print(f"  {'Rank':<5s} {'Symbol':<7s} {'Score':>7s}  {'Research Status':<20s}")
    print("─" * 55)

    if not top_items:
        print("  (no candidates produced — check pipeline diagnostics)")
        return

    for i, item in enumerate(top_items, 1):
        ticker = item.get("ticker", "?")
        score = _safe_score(item)
        ct = _candidate_type_label(item)
        print(f"  {i:<5d} {ticker:<7s} {score:>7.1f}  {ct:<20s}")

    print("─" * 55)
    print(f"  Formal Candidates:   {len(result.get('formal_candidates', []))}")
    print(f"  Pipeline Rate:       {result.get('selection_funnel', {}).get('pipeline_success_rate', 0):.1%}")
    print(f"  Run Mode:            {result.get('run_mode', 'DEMO')}")
    print(f"  Quality Fallback:    {result.get('quality_fallback_active', False)}")


def _print_safety_section() -> None:
    """Print the safety status block — always DISABLED in demo."""
    print()
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║                      SAFETY STATUS                        ║")
    print("╠═══════════════════════════════════════════════════════════╣")
    print("║  Execution:    DISABLED                                   ║")
    print("║  Trading:      NOT AVAILABLE IN DEMO MODE                  ║")
    print("║  Broker:       NOT CONNECTED                               ║")
    print("║  allow_live:   false (forced)                              ║")
    print("║  reduce_only:  true (forced)                               ║")
    print("╚═══════════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(DEMO_BANNER)

    # Force demo run_mode so quality filters stay relaxed
    os.environ["OPENALPHA_LIVE_DATA"] = "0"
    os.environ["OPENALPHA_UNIVERSE"] = "sample"
    os.environ["OPENALPHA_TOP_K"] = "5"
    os.environ["OPENALPHA_MAX_SYMBOLS"] = "10"
    os.environ["OPENALPHA_FETCH_NEWS"] = "0"
    os.environ["OPENALPHA_FAST_START_ONLY"] = "1"

    from src.openalpha.demo_data import get_demo_provider, DemoDataProvider

    provider = get_demo_provider()
    demo_symbols = provider.symbols

    print(f"  Mode:      DEMO")
    print(f"  Universe:  {len(demo_symbols)} symbols ({', '.join(demo_symbols)})")
    print(f"  Data:      {252} rows per symbol (seeded random walk, deterministic)")
    print(f"  Network:   DISABLED")
    print(f"  API Keys:  NOT REQUIRED")

    # ── Monkey-patch preflight to return DEMO mode ───────────────────────
    import src.openalpha.preflight as preflight_module
    _original_run_preflight = preflight_module.run_preflight

    def _demo_preflight(dry_run: bool = True):
        return type("DemoPF", (), {
            "to_dict": lambda self: {
                "market_state": "DEMO",
                "run_mode": "DEMO",
                "is_trading_day": True,
                "session_label": "DEMO",
                "session_reason": "demo_mode",
                "current_session_date": "2026-07-25",
                "previous_session_date": "2026-07-24",
                "symbols_checked": 5,
                "quotes_available": 5,
                "ohlcv_available": 5,
                "quote_coverage_pct": 100.0,
                "ohlcv_coverage_pct": 100.0,
            },
        })()

    preflight_module.run_preflight = _demo_preflight  # type: ignore[attr-access]

    # ── Patch quality context for demo ───────────────────────────────────
    import src.openalpha.selector as selector_module

    class DemoQualityContext:
        def __init__(self):
            pass
        def history_metrics(self, symbol: str):
            upper = str(symbol).strip().upper()
            df = provider.get_ohlcv(upper) if upper in provider.symbols else None
            if df is not None and len(df) >= 10:
                avg_vol = float(df["Volume"].tail(10).mean())
                closes = df["Close"].values
                chg = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0 if len(closes) >= 4 and closes[-4] > 0 else 0.0
                return (avg_vol, chg, float(closes[-1]))
            return (2_000_000, 0.0, 50.0)
        def quote_metrics(self, symbol: str):
            upper = str(symbol).strip().upper()
            price = provider.price_at(upper) if upper in provider.symbols else None
            price = price or 50.0
            spread = price * 0.0002
            return (price, price - spread, price + spread, True)
        def close(self):
            pass

    original_qfc = selector_module._QualityFilterContext
    selector_module._QualityFilterContext = DemoQualityContext

    from src.openalpha.selector import AIStrategySelector

    selector = AIStrategySelector()
    selector.selection_size = 5
    selector.universe._load_local_snapshot = lambda: list(demo_symbols)
    selector.news.collect_for_symbols = lambda symbols: {s: [] for s in symbols}

    # Patch scorer to use demo data
    import pandas as pd
    def _demo_load_history(symbol: str) -> pd.DataFrame:
        try:
            return provider.get_ohlcv(str(symbol).strip().upper())
        except KeyError:
            return pd.DataFrame()
    selector.scorer._load_history = _demo_load_history

    # ── Run pipeline ─────────────────────────────────────────────────────
    result = selector.run_selection(write_configs=False)
    from src.openalpha.funnel_tracker import FunnelTracker
    tracker = selector_module.FunnelTracker(
        selection_run_id="demo-run",
        selection_date="2026-07-25",
    )

    # ── Output ───────────────────────────────────────────────────────────
    print()
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║             9-Stage Pipeline Execution                    ║")
    print("╚═══════════════════════════════════════════════════════════╝")

    _print_pipeline_status_from_result(result)

    print()
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║             Research Candidates                           ║")
    print("╚═══════════════════════════════════════════════════════════╝")

    _print_candidates_from_result(result)

    _print_safety_section()

    # ── Artifacts ────────────────────────────────────────────────────────
    artifacts_dir = PROJECT_DIR / "artifacts" / "demo"
    json_path, md_path = _write_demo_artifacts_from_result(result, artifacts_dir)

    print()
    print(f"  Demo artifacts written:")
    print(f"    JSON:   {json_path}")
    print(f"    Report: {md_path}")
    print()
    print("═" * 55)
    print("  Demo pipeline complete. No API keys required.")
    print("═" * 55)

    # ── Cleanup ──────────────────────────────────────────────────────────
    preflight_module.run_preflight = _original_run_preflight  # type: ignore[attr-access]
    selector_module._QualityFilterContext = original_qfc


def _print_pipeline_status_from_result(result: dict[str, Any]) -> None:
    funnel = result.get("selection_funnel", {})
    stages = funnel.get("stages", [])
    print()
    print("─" * 50)
    print(f"  {'Stage':<28s} {'Status':>10s}")
    print("─" * 50)
    for s in stages:
        name = s.get("stage", "?").replace("_", " ").title()
        ok = s.get("output_count", 0) > 0
        print(f"  {name:<28s} {_status_icon(ok):>10s}")
    print("─" * 50)


def _print_candidates_from_result(result: dict[str, Any]) -> None:
    top_items = result.get("top5", [])
    print()
    print("─" * 55)
    print(f"  {'Rank':<5s} {'Symbol':<7s} {'Score':>7s}  {'Research Status':<20s}")
    print("─" * 55)

    if not top_items:
        print("  (no candidates produced — check pipeline diagnostics)")
    else:
        for i, item in enumerate(top_items, 1):
            ticker = item.get("ticker", "?")
            score = _safe_score(item)
            ct = _candidate_type_label(item)
            print(f"  {i:<5d} {ticker:<7s} {score:>7.1f}  {ct:<20s}")

    print("─" * 55)
    print(f"  Formal Candidates:   {len(result.get('formal_candidates', []))}")
    print(f"  Pipeline Rate:       {result.get('selection_funnel', {}).get('pipeline_success_rate', 0):.1%}")


def _write_demo_artifacts_from_result(
    result: dict[str, Any], artifacts_dir: Path
) -> tuple[Path, Path]:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = result.get("selection_run_id", "demo-unknown")
    funnel = result.get("selection_funnel", {})

    json_payload = {
        "mode": "DEMO",
        "generated_at": timestamp,
        "selection_run_id": run_id,
        "universe_source": result.get("universe_source", "demo"),
        "pipeline_rate": funnel.get("pipeline_success_rate", 0),
        "run_mode": result.get("run_mode", "DEMO"),
        "candidate_type": result.get("candidate_type", "RESEARCH_ONLY"),
        "quality_fallback_active": result.get("quality_fallback_active", False),
        "formal_candidates": result.get("formal_candidates", []),
        "preview_candidates": result.get("preview_candidates", []),
        "top_candidates": [
            {
                "rank": i,
                "ticker": item.get("ticker", "?"),
                "score": _safe_score(item),
                "sector": item.get("sector", "Unknown"),
                "candidate_type": item.get("candidate_type", "RESEARCH_ONLY"),
                "data_source": item.get("data_source", "demo"),
            }
            for i, item in enumerate(result.get("top5", []), 1)
        ],
        "funnel_stages": [
            {
                "stage": s.get("stage", "?"),
                "input_count": s.get("input_count", 0),
                "output_count": s.get("output_count", 0),
                "eliminated": s.get("eliminated", 0),
                "status": s.get("status", "PASS"),
            }
            for s in funnel.get("stages", [])
        ],
        "safety": {
            "execution": "DISABLED",
            "trading": "NOT AVAILABLE IN DEMO MODE",
            "broker": "NOT CONNECTED",
            "allow_live_order": False,
            "reduce_only": True,
        },
    }
    json_path = artifacts_dir / "demo_selection.json"
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    md_lines = [
        f"# QuantCairn Demo — AI Research Pipeline",
        f"",
        f"> Generated: {timestamp}  ",
        f"> Run ID: `{run_id}`  ",
        f"> Mode: **DEMO**  ",
        f"",
        f"## Pipeline Summary",
        f"",
        f"| Stage | Input | Output | Eliminated | Status |",
        f"|-------|-------|--------|-----------|--------|",
    ]
    for s in funnel.get("stages", []):
        icon = "✅" if s.get("status") == "PASS" else "⚠️"
        md_lines.append(
            f"| {s.get('stage','?')} | {s.get('input_count',0)} "
            f"| {s.get('output_count',0)} | {s.get('eliminated',0)} "
            f"| {icon} {s.get('status','PASS')} |"
        )
    md_lines.extend(["", "## TOP Candidates", ""])
    for i, item in enumerate(result.get("top5", []), 1):
        ticker = item.get("ticker", "?")
        score = _safe_score(item)
        ct = _candidate_type_label(item)
        md_lines.extend([
            f"### {i}. {ticker}",
            f"- **Score**: {score:.1f}",
            f"- **Sector**: {item.get('sector', 'Unknown')}",
            f"- **Research Status**: {ct}",
            f"- **Data Source**: {item.get('data_source', 'demo')}",
            "",
        ])
    md_lines.extend([
        "## Safety",
        "",
        "- **Execution**: DISABLED",
        "- **Trading**: NOT AVAILABLE IN DEMO MODE",
        "- **Broker**: NOT CONNECTED",
        "",
        "---",
        "*Generated by QuantCairn Demo Pipeline. No API keys required.*",
    ])
    md_path = artifacts_dir / "demo_report.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    return json_path, md_path


if __name__ == "__main__":
    main()
