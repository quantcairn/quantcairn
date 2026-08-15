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

from src.config.runtime_paths import resolve_artifacts_dir

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
    "POST_FILTER",
    "FINAL_SELECTED",
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
    "post_filter_removed", "final_selection_limit",
    # Quality filter reasons (DATA_QUALITY stage)
    "missing_market_data", "volume_filter", "spread_filter",
    "spread_unavailable", "volatility_filter",
    "existing_position_bypass", "special_etf_quote_override",
    # Market data diagnostic reasons (MARKET_DATA stage)
    "no_ohlcv_data_and_no_fallback",
    "no_ohlcv_data_fallback_blocked_universe",
    "insufficient_ohlcv_rows",
    "ohlcv_success_but_scoring_failed",
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
        self._quality_fallback = False
        self._quality_relaxed = False  # EOD mode: quality rejected all, relaxed path used
        self._preview_candidates: list[str] = []
        self._formal_candidates: list[str] = []

    def mark_quality_fallback(self, *, preview_symbols: list[str], formal_symbols: list[str]) -> None:
        """Record that quality gate rejected all candidates and preview pool was used.

        Preview candidates are research-only — never tradable.
        Formal TOP is empty — no candidate passed all gates.
        """
        self._quality_fallback = True
        self._preview_candidates = list(preview_symbols or [])
        self._formal_candidates = list(formal_symbols or [])

    def mark_quality_relaxed(self) -> None:
        """Record that EOD/relaxed mode used pre-quality pool after quality rejection."""
        self._quality_relaxed = True

    def set_formal_candidates(self, formal_symbols: list[str]) -> None:
        """Set formal candidates from the normal (non-fallback) path."""
        if not self._quality_fallback:
            self._formal_candidates = list(formal_symbols or [])

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
                    # ── Quality fallback / relaxed: DATA_QUALITY rejected all, next stage drew from preliminary pool ──
                    if (self._quality_fallback or self._quality_relaxed) and rec.stage in ("FORMAL_TOP", "COMPOSITION_FILTER") and prev.stage == "DATA_QUALITY":
                        continue  # Expected runtime path, not a consistency violation
                    if self._quality_relaxed and rec.stage == "FORMAL_TOP" and prev.stage == "COMPOSITION_FILTER":
                        continue  # EOD relaxed: FORMAL_TOP draws from pre-quality pool
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
            "quality_fallback_active": self._quality_fallback,
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
                if (self._quality_fallback or self._quality_relaxed) and rec.stage in ("FORMAL_TOP", "COMPOSITION_FILTER"):
                    chain_str = "  ← quality relaxed" if self._quality_relaxed else "  ← quality fallback"
                else:
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
        print(f"  Preview Candidates        {len(self._preview_candidates)}")
        print(f"  Formal Candidates         {len(self._formal_candidates)}")
        if self._quality_fallback:
            print(f"  Quality Fallback          ACTIVE (preview=research only, formal=none)")
        print(f"  Universe Loaded           {universe_in}")
        print(f"  Pipeline Success Rate     {success_rate:.2%}")
        print(f"  Overall Consistency       {'✅ PASS' if validation['consistent'] else '⚠️  WARNINGS'}")
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
            path = resolve_artifacts_dir(self.project_dir) / "selection" / "funnel_debug.json"
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
                "preview_candidates": len(self._preview_candidates),
                "formal_candidates": len(self._formal_candidates),
                "final_top": len(_symbols(last.output_symbols)) if last else 0,
                "fallback_used": self._quality_fallback,
                "fallback_reason": "QUALITY_GATE" if self._quality_fallback else "",
                "preview_symbols": self._preview_candidates,
                "formal_symbols": self._formal_candidates,
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
        path = (
            resolve_artifacts_dir(self.project_dir)
            / "selector_funnel"
            / self.selection_date
            / self.selection_run_id
            / "funnel_report.json"
        )
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

    # ── Diagnostic report ─────────────────────────────────────────────────

    REASON_LABELS: dict[str, str] = {
        "missing_market_data": "OHLCV/报价数据缺失",
        "volume_filter": "日均成交量不足 50 万股",
        "spread_filter": "买卖价差 ≥ 0.5%",
        "spread_unavailable": "无法获取买卖报价",
        "volatility_filter": "3 日波动超限",
        "existing_position_bypass": "已持仓标的放行(跳过质量检查)",
        "special_etf_quote_override": "特殊ETF报价覆盖",
        "ohlcv_missing": "OHLCV 历史数据缺失",
        "quote_missing": "实时报价缺失",
        "history_insufficient": "历史数据不足（<4 条 K 线）",
        "scoring_ineligible": "不具备评分资格",
        "formal_scoring_ineligible": "不具备正式评分资格",
        "composition_limit": "组合分散化上限",
        "market_data_sufficiency_failed": "市场数据不满足最低要求",
        # Market data diagnostic codes
        "no_ohlcv_data_and_no_fallback": "无OHLCV数据且无回退配置",
        "no_ohlcv_data_fallback_blocked_universe": "OHLCV缺失且回退被Universe拦截",
        "insufficient_ohlcv_rows": "OHLCV历史数据不足",
        "ohlcv_success_but_scoring_failed": "数据获取成功但评分管道失败",
        # Universe filter codes
        "price_out_of_range": "价格超范围",
        "price_missing": "价格缺失",
        "dollar_volume_missing": "成交额数据缺失",
        "low_dollar_volume": "成交额不足",
        "market_cap_missing": "市值数据缺失",
        "market_cap_too_small": "市值过小",
        "volatility_missing": "波动率数据缺失",
        "volatility_too_low": "波动率过低",
        "volatility_too_high": "波动率过高",
        "post_filter_removed": "最终后处理移除",
        "final_selection_limit": "最终入选数量限制",
        "unknown": "原因未记录",
    }

    def print_diagnostic_report(self) -> None:
        """Print a detailed per-stage diagnostic report.

        For each stage, shows every eliminated symbol with its elimination
        reason.  When FORMAL_TOP is empty, also produces a per-candidate
        trace showing exactly which stage rejected each symbol and why.
        """
        validation = self.validate()
        formal_top_empty = bool(
            self.records
            and _symbols(self.records[-1].output_symbols) == []
        )

        print()
        print("=" * 68)
        print("  PIPELINE DIAGNOSTIC REPORT")
        print("=" * 68)

        # ── 1. Per-stage breakdown ────────────────────────────────────────
        zero_stage: str | None = None
        for i, rec in enumerate(self.records):
            d = rec.to_dict()
            eliminated = d["eliminated"]
            label = d["stage"].replace("_", " ").title()
            dropped_items = d.get("dropped") or []

            # Mark first zero-output stage
            if d["output_count"] == 0 and zero_stage is None:
                zero_stage = d["stage"]

            print()
            print(f"  ┌─ Stage {i+1}: {label}")
            print(f"  │  Input:  {d['input_count']:>4d}  →  Output: {d['output_count']:>4d}  "
                  f" Eliminated: {eliminated:>4d}  [{d['duration_ms']}ms]")

            if eliminated == 0 and d["input_count"] > 0:
                print(f"  │  All {d['output_count']} passed — no eliminations.")

            for item in dropped_items:
                symbol = str(item.get("symbol") or "?").upper()
                code = str(item.get("reason_code") or "unknown")
                detail = str(item.get("reason_detail") or "")
                label_text = self.REASON_LABELS.get(code, code.replace("_", " ").title())
                if detail:
                    print(f"  │  ✗ {symbol:<6s}  {label_text:<40s} ({detail})")
                else:
                    print(f"  │  ✗ {symbol:<6s}  {label_text}")

        # ── 2. Zero-output diagnosis ──────────────────────────────────────
        if zero_stage:
            print()
            print(f"  ════  FIRST ZERO-OUTPUT STAGE: {zero_stage}  ════")
            print(f"  No candidates survived beyond this stage.")
            print(f"  Check data freshness, market hours, or filter thresholds.")

        # ── 3. FORMAL_TOP empty → full candidate trace ────────────────────
        if formal_top_empty:
            print()
            print("  ════  FORMAL TOP EMPTY — Full Candidate Trace  ════")
            self._print_candidate_trace()

        # ── 4. Summary ────────────────────────────────────────────────────
        print()
        print("-" * 68)
        print(f"  Universe Loaded     {_symbols(self.records[0].input_symbols) if self.records else []}")
        print(f"  Pipeline Rate       {self.pipeline_success_rate():.2%}")
        print(f"  Preview Candidates  {len(self._preview_candidates)} (research-only)")
        print(f"  Formal Candidates   {len(self._formal_candidates)}")
        if self._quality_fallback:
            print(f"  Quality Fallback    ACTIVE — no candidate passed all gates")
        if zero_stage:
            print(f"  First Empty Stage   {zero_stage}")
        if formal_top_empty:
            print(f"  FORMAL TOP          EMPTY (0 tradable candidates)")
        print(f"  Consistency         {'✅ PASS' if validation['consistent'] else '⚠️  WARNINGS'}")
        print("=" * 68)
        print()

    def _print_candidate_trace(self) -> None:
        """Trace every symbol from UNIVERSE through each stage: where did each one drop?"""
        if not self.records:
            print("  (no pipeline records)")
            return

        # Build a lookup: symbol → (stage, reason_code, reason_detail)
        first_stage = _symbols(self.records[0].input_symbols)
        if not first_stage:
            print("  Universe was empty — nothing to trace.")
            return

        # Track every symbol's fate
        fates: dict[str, dict] = {}
        for sym in first_stage:
            fates[sym] = {"symbol": sym, "eliminated_at": None, "reason_code": None, "reason_detail": None}

        for rec in self.records:
            d = rec.to_dict()
            stage_name = d["stage"]
            # Build a set of symbols that survived this stage
            survivors = set(d["output_symbols"])
            dropped_items = {str(item.get("symbol") or "").upper(): item for item in (d.get("dropped") or [])}

            for sym in first_stage:
                if fates[sym]["eliminated_at"] is not None:
                    continue  # already eliminated earlier
                if sym not in survivors:
                    item = dropped_items.get(sym, {})
                    fates[sym]["eliminated_at"] = stage_name
                    fates[sym]["reason_code"] = item.get("reason_code") or "unknown"
                    fates[sym]["reason_detail"] = item.get("reason_detail") or ""

        print()
        print(f"  {'Symbol':<6s}  {'Eliminated At':<28s}  {'Reason':<40s}  Detail")
        print(f"  {'------':<6s}  {'-------------':<28s}  {'------':<40s}  ------")
        for sym in sorted(first_stage):
            f = fates.get(sym, {})
            stage_name = f.get("eliminated_at") or "FINAL"
            code = f.get("reason_code") or "passed"
            label = self.REASON_LABELS.get(code, code.replace("_", " ").title())
            detail = f.get("reason_detail") or ""
            if stage_name == "FINAL":
                print(f"  {sym:<6s}  ✅ survived all stages — {label}")
            else:
                print(f"  {sym:<6s}  ✗ {stage_name:<26s}  {label:<40s}  {detail}")
