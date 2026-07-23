"""Authoritative FunnelTracker — single source of truth for pipeline statistics.

Records every stage transition, enforces input/output consistency,
tracks timing, and generates debug artifacts.  Never modifies
trading state or scores.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

FUNNEL_STAGES = [
    "UNIVERSE",
    "UNIVERSE_FILTER",
    "MARKET_DATA",
    "DATA_QUALITY",
    "SCORING_ELIGIBLE",
    "BASE_RANKING",
    "RESEARCH_PROVIDER",
    "REFINEMENT",
    "FORMAL_ELIGIBILITY",
    "COMPOSITION_FILTER",
    "FORMAL_TOP",
]

KNOWN_REASON_CODES = {
    "price_out_of_range", "low_dollar_volume", "market_cap_missing",
    "atr_missing", "quote_missing", "ohlcv_missing", "benchmark_invalid",
    "freshness_invalid", "market_data_sufficiency_failed",
    "history_insufficient", "scoring_ineligible", "formal_scoring_ineligible",
    "invalid_score_provenance", "research_evidence_failed",
    "trade_admission_not_tradable", "validation_status_ai_candidate",
    "provider_timeout", "provider_skipped_budget", "provider_unavailable",
    "provider_malformed_response", "refinement_rejected",
    "entry_quality_too_low", "leveraged_etf_limit_exceeded",
    "composition_limit", "top_n_limit", "top_n_not_filled", "unknown",
}


def _project_dir() -> Path:
    override = os.environ.get("SOXS_PROJECT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _symbol(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("ticker") or value.get("symbol")
    return str(value or "").strip().upper()


def _symbols(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        symbol = _symbol(raw)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def _reason_code(value: str | None) -> str:
    code = str(value or "").strip().lower()
    if not code:
        return "unknown"
    aliases = {
        "price_band": "price_out_of_range",
        "market_cap_too_small": "market_cap_missing",
        "missing_atr": "atr_missing",
        "missing_quote": "quote_missing",
        "missing_ohlcv": "ohlcv_missing",
        "benchmark_missing": "benchmark_invalid",
        "history_short": "history_insufficient",
        "scoring_eligible_false": "scoring_ineligible",
        "validation_status_ai_candidate": "validation_status_ai_candidate",
        "validation_status_not_tradable": "trade_admission_not_tradable",
        "formal_scoring_eligibility_false": "formal_scoring_ineligible",
        "market_data_sufficiency_failed": "market_data_sufficiency_failed",
        "market_data_sufficiency_degraded": "market_data_sufficiency_failed",
        "score_not_formal": "formal_scoring_ineligible",
        "diagnostic_score_not_formal": "formal_scoring_ineligible",
        "data_status_invalid": "ohlcv_missing",
        "quote_status_missing": "quote_missing",
        "ohlcv_status_missing": "ohlcv_missing",
        "history_status_missing": "history_insufficient",
        "benchmark_status_invalid": "benchmark_invalid",
        "fallback_used_blocked": "critical_fallback",
        "critical_market_data_fallback": "critical_fallback",
    }
    normalized = aliases.get(code, code)
    return normalized if normalized in KNOWN_REASON_CODES or normalized == "critical_fallback" else normalized


def reason_from_candidate(candidate: Mapping[str, Any]) -> str:
    reasons = candidate.get("rejection_reason")
    if isinstance(reasons, list) and reasons:
        return _reason_code(str(reasons[0]))
    for key in (
        "reason_code", "reject_reason", "scoring_block_reason",
        "composition_reject_reason", "selection_penalty_reason",
        "stale_reason", "reason",
    ):
        value = candidate.get(key)
        if isinstance(value, list):
            value = next((str(item).strip() for item in value if str(item).strip()), "")
        if str(value or "").strip():
            return _reason_code(str(value).split(":", 1)[0].split(";", 1)[0])
    return "unknown"


def dropped_record(symbol: str, reason_code: str = "unknown", reason_detail: str = "", *, blocking: bool = True, **extra: Any) -> dict[str, Any]:
    record = {
        "symbol": str(symbol or "").strip().upper(),
        "reason_code": _reason_code(reason_code),
        "reason_detail": str(reason_detail or ""),
        "blocking": bool(blocking),
    }
    record.update(extra)
    return record


# ═══════════════════════════════════════════════════════════════════════════════
# Stage record with timing
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FunnelStageRecord:
    stage: str
    input_symbols: list[str]
    output_symbols: list[str]
    dropped: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.completed_at <= self.started_at:
            self.completed_at = self.started_at

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.completed_at - self.started_at) * 1000))

    @property
    def input_count(self) -> int:
        return len(_symbols(self.input_symbols))

    @property
    def output_count(self) -> int:
        return len(_symbols(self.output_symbols))

    @property
    def eliminated_count(self) -> int:
        return max(0, self.input_count - self.output_count)

    @property
    def status(self) -> str:
        if self.output_count > self.input_count:
            return "WARN"
        return "PASS"

    def to_dict(self) -> dict[str, Any]:
        input_symbols = _symbols(self.input_symbols)
        output_symbols = _symbols(self.output_symbols)
        dropped_symbols = [symbol for symbol in input_symbols if symbol not in set(output_symbols)]
        dropped_by_symbol = {
            str(item.get("symbol") or "").upper(): dict(item)
            for item in self.dropped
            if str(item.get("symbol") or "").strip()
        }
        dropped = [
            dropped_by_symbol.get(symbol)
            or dropped_record(symbol, "unknown", "stage_removed_without_structured_reason")
            for symbol in dropped_symbols
        ]
        counts = Counter(
            str(item.get("reason_code") or "unknown")
            for item in dropped
            if not (
                str(item.get("reason_code") or "").lower() == "unknown"
                and str(item.get("reason_detail") or "") == "stage_removed_without_structured_reason"
            )
        )
        return {
            "stage": self.stage,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "eliminated": self.eliminated_count,
            "input_symbols": input_symbols,
            "output_symbols": output_symbols,
            "dropped_symbols": dropped_symbols,
            "dropped": dropped,
            "drop_reason_counts": dict(sorted(counts.items())),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# FunnelTracker
# ═══════════════════════════════════════════════════════════════════════════════

class FunnelTracker:
    def __init__(self, *, selection_run_id: str, selection_date: str, project_dir: Path | None = None) -> None:
        self.selection_run_id = str(selection_run_id)
        self.selection_date = str(selection_date)
        self.project_dir = Path(project_dir).expanduser().resolve() if project_dir else _project_dir()
        self.records: list[FunnelStageRecord] = []

    def add_stage(
        self,
        stage: str,
        input_items: Iterable[Any],
        output_items: Iterable[Any],
        *,
        dropped: Iterable[Mapping[str, Any]] | None = None,
        started_at: float | None = None,
        completed_at: float | None = None,
    ) -> None:
        input_symbols = _symbols(input_items)
        output_symbols = _symbols(output_items)
        dropped_records = [
            dict(item)
            for item in dropped or []
            if str(item.get("symbol") or item.get("ticker") or "").strip()
        ]
        normalized_dropped = []
        for item in dropped_records:
            symbol = _symbol(item)
            normalized_dropped.append(
                dropped_record(
                    symbol,
                    str(item.get("reason_code") or reason_from_candidate(item)),
                    str(item.get("reason_detail") or item.get("details") or item.get("reason") or item.get("reject_reason") or ""),
                    blocking=bool(item.get("blocking", True)),
                )
            )
        now = time.time()
        self.records.append(
            FunnelStageRecord(
                stage=str(stage or "UNKNOWN").strip().upper(),
                input_symbols=input_symbols,
                output_symbols=output_symbols,
                dropped=normalized_dropped,
                started_at=started_at if started_at is not None else now,
                completed_at=completed_at if completed_at is not None else now,
            )
        )

    # ── Consistency validation ──────────────────────────────────────────

    def validate(self) -> dict[str, Any]:
        """Check the full pipeline chain for consistency violations.

        Returns a dict with:
          - warnings: list of per-stage violation messages
          - consistent: True if zero violations
          - always continues execution (never raises).
        """
        warnings: list[dict[str, object]] = []
        for i, rec in enumerate(self.records):
            d = rec.to_dict()
            # output <= input
            if rec.output_count > rec.input_count:
                warnings.append({
                    "stage": rec.stage,
                    "check": "output_gt_input",
                    "detail": f"input={rec.input_count} output={rec.output_count}",
                })
            # input >= 0, output >= 0
            if rec.input_count < 0:
                warnings.append({"stage": rec.stage, "check": "negative_input", "detail": str(rec.input_count)})
            if rec.output_count < 0:
                warnings.append({"stage": rec.stage, "check": "negative_output", "detail": str(rec.output_count)})
            # next.input == previous.output
            if i > 0:
                prev = self.records[i - 1]
                prev_output = len(_symbols(prev.output_symbols))
                curr_input = len(_symbols(rec.input_symbols))
                if curr_input != prev_output:
                    warnings.append({
                        "stage": rec.stage,
                        "check": "chain_break",
                        "detail": (
                            f"previous_stage={prev.stage} "
                            f"expected_input={prev_output} "
                            f"actual_input={curr_input}"
                        ),
                    })
        return {
            "warnings": warnings,
            "consistent": len(warnings) == 0,
        }

    def pipeline_success_rate(self) -> float:
        """Universe → Formal Top conversion rate."""
        if not self.records:
            return 0.0
        first = _symbols(self.records[0].input_symbols)
        last = _symbols(self.records[-1].output_symbols) if self.records else []
        if not first:
            return 0.0
        return round(len(last) / len(first), 4)

    # ── Console report ──────────────────────────────────────────────────

    def print_consistency_report(self) -> None:
        """Print a formatted consistency report to stdout."""
        validation = self.validate()
        success_rate = self.pipeline_success_rate()
        print()
        print("=" * 50)
        print("  Funnel Consistency Report")
        print("=" * 50)
        prev_output = None
        for rec in self.records:
            d = rec.to_dict()
            chain_str = ""
            if prev_output is not None and rec.input_count != prev_output:
                chain_str = "  ⚠ chain break"
            elif rec.status == "WARN":
                chain_str = "  ⚠ warn"
            print(f"  {d['stage']:<24s}  {d['input_count']:>4d} → {d['output_count']:>4d}  "
                  f"({d['eliminated']:>2d} eliminated)  {d['duration_ms']:>5d}ms  "
                  f"{d['status']}{chain_str}")
            prev_output = rec.output_count

        first_stage = self.records[0] if self.records else None
        last_stage = self.records[-1] if self.records else None

        print("-" * 50)
        universe_in = len(_symbols(first_stage.input_symbols)) if first_stage else 0
        final_out = len(_symbols(last_stage.output_symbols)) if last_stage else 0
        print(f"  Universe Loaded          {universe_in}")
        print(f"  Final Candidates         {final_out}")
        print(f"  Pipeline Success Rate    {success_rate:.2%}")
        print(f"  Overall Consistency      {'✅ PASS' if validation['consistent'] else '⚠️  WARNINGS'}")
        if validation["warnings"]:
            print("-" * 50)
            print("  Consistency Warnings:")
            for w in validation["warnings"]:
                print(f"    [{w['stage']}] {w['check']}: {w['detail']}")
        print("=" * 50)
        print()

    # ── Debug artifact ──────────────────────────────────────────────────

    def write_debug_artifact(self) -> Path | None:
        """Write pipeline debug JSON to artifacts/selection/funnel_debug.json."""
        try:
            path = self.project_dir / "artifacts" / "selection" / "funnel_debug.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            validation = self.validate()
            first = self.records[0] if self.records else None
            last = self.records[-1] if self.records else None
            payload: dict[str, Any] = {
                "selection_run_id": self.selection_run_id,
                "selection_date": self.selection_date,
                "pipeline_consistent": validation["consistent"],
                "pipeline_success_rate": self.pipeline_success_rate(),
                "universe_loaded": len(_symbols(first.input_symbols)) if first else 0,
                "formal_candidates": len(_symbols(last.output_symbols)) if last else 0,
                "final_top": len(_symbols(last.output_symbols)) if last else 0,
                "stages": [rec.to_dict() for rec in self.records],
                "warnings": validation["warnings"],
            }
            tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
            return path
        except Exception:
            return None

    # ── Full report (stages + reasons + nearest) ────────────────────────

    def to_dict(self) -> dict[str, Any]:
        stages = [record.to_dict() for record in self.records]
        reason_counts: Counter[str] = Counter()
        nearest_rejected: list[dict[str, Any]] = []
        for stage in stages:
            reason_counts.update(stage.get("drop_reason_counts") or {})
            for item in stage.get("dropped") or []:
                if (
                    str(item.get("reason_code") or "").lower() == "unknown"
                    and str(item.get("reason_detail") or "") == "stage_removed_without_structured_reason"
                ):
                    continue
                nearest_rejected.append(dict(item, **{
                    "symbol": item.get("symbol"),
                    "stage": stage.get("stage"),
                    "reason_code": item.get("reason_code") or "unknown",
                    "reason_detail": item.get("reason_detail") or "",
                    "blocking": bool(item.get("blocking", True)),
                }))
        return {
            "selection_run_id": self.selection_run_id,
            "selection_date": self.selection_date,
            "stages": stages,
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
            "nearest_rejected_candidates": nearest_rejected[:20],
            "pipeline_consistent": self.validate()["consistent"],
            "pipeline_success_rate": self.pipeline_success_rate(),
        }

    def write_report(self) -> Path:
        path = self.project_dir / "artifacts" / "selector_funnel" / self.selection_date / self.selection_run_id / "funnel_report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    def print_table(self) -> None:
        rows = [record.to_dict() for record in self.records]
        print("")
        print(f"{'Stage':<24}{'In':>6}{'Out':>6}{'Drop':>7}")
        for row in rows:
            print(f"{row['stage'].replace('_', ' ').title():<24}{row['input_count']:>6}{row['output_count']:>6}{row['eliminated']:>7}")
        for row in rows:
            dropped = row.get("dropped") or []
            if not dropped:
                continue
            print(f"{row['stage'].replace('_', ' ').title()}:")
            for item in dropped[:20]:
                print(f"  {item.get('symbol')}  {item.get('reason_code')}")
