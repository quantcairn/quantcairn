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
    "price_out_of_range",
    "low_dollar_volume",
    "market_cap_missing",
    "atr_missing",
    "quote_missing",
    "ohlcv_missing",
    "benchmark_invalid",
    "freshness_invalid",
    "history_insufficient",
    "scoring_ineligible",
    "invalid_score_provenance",
    "trade_admission_not_tradable",
    "provider_timeout",
    "provider_skipped_budget",
    "provider_unavailable",
    "provider_malformed_response",
    "refinement_rejected",
    "entry_quality_too_low",
    "leveraged_etf_limit_exceeded",
    "composition_limit",
    "top_n_limit",
    "top_n_not_filled",
    "unknown",
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
        "validation_status_ai_candidate": "trade_admission_not_tradable",
        "validation_status_not_tradable": "trade_admission_not_tradable",
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
        "reason_code",
        "reject_reason",
        "scoring_block_reason",
        "composition_reject_reason",
        "selection_penalty_reason",
        "stale_reason",
        "reason",
    ):
        value = candidate.get(key)
        if isinstance(value, list):
            value = next((str(item).strip() for item in value if str(item).strip()), "")
        if str(value or "").strip():
            return _reason_code(str(value).split(":", 1)[0].split(";", 1)[0])
    return "unknown"


def dropped_record(symbol: str, reason_code: str = "unknown", reason_detail: str = "", *, blocking: bool = True) -> dict[str, Any]:
    return {
        "symbol": str(symbol or "").strip().upper(),
        "reason_code": _reason_code(reason_code),
        "reason_detail": str(reason_detail or ""),
        "blocking": bool(blocking),
    }


@dataclass
class FunnelStageRecord:
    stage: str
    input_symbols: list[str]
    output_symbols: list[str]
    dropped: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        input_symbols = _symbols(self.input_symbols)
        output_symbols = _symbols(self.output_symbols)
        dropped_symbols = [symbol for symbol in input_symbols if symbol not in set(output_symbols)]
        dropped_by_symbol = {str(item.get("symbol") or "").upper(): dict(item) for item in self.dropped if str(item.get("symbol") or "").strip()}
        dropped = [
            dropped_by_symbol.get(symbol)
            or dropped_record(symbol, "unknown", "stage_removed_without_structured_reason")
            for symbol in dropped_symbols
        ]
        counts = Counter(str(item.get("reason_code") or "unknown") for item in dropped)
        return {
            "stage": self.stage,
            "input_count": len(input_symbols),
            "output_count": len(output_symbols),
            "input_symbols": input_symbols,
            "output_symbols": output_symbols,
            "dropped_symbols": dropped_symbols,
            "dropped": dropped,
            "drop_reason_counts": dict(sorted(counts.items())),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": max(0, int((self.completed_at - self.started_at) * 1000)),
        }


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
    ) -> None:
        input_symbols = _symbols(input_items)
        output_symbols = _symbols(output_items)
        dropped_records = [dict(item) for item in dropped or [] if str(item.get("symbol") or item.get("ticker") or "").strip()]
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
                started_at=now,
                completed_at=now,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        stages = [record.to_dict() for record in self.records]
        reason_counts: Counter[str] = Counter()
        nearest_rejected: list[dict[str, Any]] = []
        for stage in stages:
            reason_counts.update(stage.get("drop_reason_counts") or {})
            for item in stage.get("dropped") or []:
                nearest_rejected.append(
                    {
                        "symbol": item.get("symbol"),
                        "stage": stage.get("stage"),
                        "reason_code": item.get("reason_code") or "unknown",
                        "reason_detail": item.get("reason_detail") or "",
                        "blocking": bool(item.get("blocking", True)),
                    }
                )
        return {
            "selection_run_id": self.selection_run_id,
            "selection_date": self.selection_date,
            "stages": stages,
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
            "nearest_rejected_candidates": nearest_rejected[:20],
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
            print(f"{row['stage'].replace('_', ' ').title():<24}{row['input_count']:>6}{row['output_count']:>6}{len(row['dropped_symbols']):>7}")
        for row in rows:
            dropped = row.get("dropped") or []
            if not dropped:
                continue
            print(f"{row['stage'].replace('_', ' ').title()}:")
            for item in dropped[:20]:
                print(f"  {item.get('symbol')}  {item.get('reason_code')}")
