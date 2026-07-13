from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (
    ALLOWED_SYMBOL_CLASSES,
    CandidateRecord,
    CandidateTransitionError,
    DeploymentStatus,
    EvidenceStatus,
    ProfitabilityStatus,
    ValidationStatus,
    assert_transition_allowed,
    default_candidate_for_symbol,
)
from .performance_tracker import CandidatePerformanceTracker

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))
CANDIDATE_ROOT = PROJECT_DIR / "artifacts" / "candidates"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


_normalize_text = _safe_text


def _ensure_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_text(path: Path, content: str) -> Path:
    _ensure_path(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


@dataclass(slots=True)
class CandidateValidationStore:
    root_dir: Path = CANDIDATE_ROOT

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @property
    def candidates_path(self) -> Path:
        return self.root_dir / "candidates.jsonl"

    @property
    def history_path(self) -> Path:
        return self.root_dir / "candidate_status_history.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.root_dir / "candidate_validation_summary.csv"

    @property
    def performance_path(self) -> Path:
        return self.root_dir / "candidate_performance.jsonl"

    @property
    def performance_summary_path(self) -> Path:
        return self.root_dir / "candidate_performance_summary.csv"

    def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
            else:
                raise ValueError(f"invalid_jsonl_record:{path.name}")
        return rows

    def _write_jsonl(self, path: Path, rows: Iterable[dict[str, Any]]) -> Path:
        text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
        if text:
            text += "\n"
        return _atomic_write_text(path, text)

    def _append_jsonl(self, path: Path, row: dict[str, Any]) -> Path:
        rows = self._load_jsonl(path)
        rows.append(dict(row))
        return self._write_jsonl(path, rows)

    def _write_summary_csv(self, records: list[CandidateRecord]) -> Path:
        columns = [
            "candidate_id",
            "symbol",
            "market",
            "selected_at",
            "source",
            "ai_score",
            "candidate_score",
            "liquidity_score",
            "trend_score",
            "volatility_score",
            "risk_score",
            "strategy_fit_score",
            "recommended_strategy",
            "score_reason",
            "ai_reason",
            "asset_type",
            "benchmarks",
            "strategy_family",
            "risk_profile",
            "timeframe",
            "validation_status",
            "evidence_status",
            "profitability_status",
            "deployment_status",
            "trading_enabled",
            "shadow_enabled",
            "paper_enabled",
            "live_enabled",
            "rejection_reason",
            "created_at",
            "updated_at",
            "selection_stage",
            "last_completed_session",
            "daily_data_as_of",
            "premarket_snapshot_at",
            "freshness_status",
            "stale_reason",
            "trading_eligible",
            "current_session",
            "previous_completed_session",
            "next_session",
            "is_market_holiday",
            "is_premarket",
            "is_regular_session",
            "is_after_hours",
            "quote_age_seconds",
            "benchmark_data_as_of",
            "premarket_change_pct",
            "gap_pct",
            "premarket_volume",
            "spread_pct",
            "daily_data_status",
        ]
        lines = [",".join(columns)]
        for record in records:
            row = record.summary_row()
            lines.append(
                ",".join(
                    _csv_cell(row.get(column)) for column in columns
                )
            )
        return _atomic_write_text(self.summary_path, "\n".join(lines) + "\n")

    def _sync_candidate_performance(self, records: list[CandidateRecord]) -> None:
        try:
            CandidatePerformanceTracker(self.root_dir).sync(records)
        except Exception:
            pass

    def load_latest_candidates(self) -> list[CandidateRecord]:
        rows = self._load_jsonl(self.candidates_path)
        latest: dict[str, CandidateRecord] = {}
        for row in rows:
            try:
                record = CandidateRecord.from_dict(row)
            except Exception:
                continue
            latest[record.candidate_id] = record
        records = list(latest.values())
        records.sort(key=lambda item: (item.updated_at or "", item.candidate_id), reverse=True)
        return records

    def load_latest_history(self) -> list[dict[str, Any]]:
        rows = self._load_jsonl(self.history_path)
        rows.sort(key=lambda item: (str(item.get("at") or ""), str(item.get("candidate_id") or "")), reverse=True)
        return rows

    def get_candidate(self, candidate_id: str) -> CandidateRecord | None:
        candidate_key = _safe_text(candidate_id)
        if not candidate_key:
            return None
        for record in self.load_latest_candidates():
            if record.candidate_id == candidate_key:
                return record
        return None

    def ingest_ai_selection_report(self, report: dict[str, Any], candidates: Iterable[dict[str, Any]] | None = None) -> list[CandidateRecord]:
        candidate_rows = list(candidates or [])
        if not candidate_rows and isinstance(report, dict):
            candidate_rows.extend(report.get("top10") or [])
            if not candidate_rows:
                candidate_rows.extend(report.get("top3") or [])
            if not candidate_rows:
                candidate_rows.extend(report.get("report") or [])
        selected_at = _safe_text(report.get("generated_at") or report.get("timestamp") or report.get("selection_date")) or _utc_now_iso()
        records: list[CandidateRecord] = []
        for raw in candidate_rows:
            if not isinstance(raw, dict):
                continue
            symbol = raw.get("symbol") or raw.get("ticker") or ""
            ai_score = raw.get("ai_score")
            if ai_score is None:
                ai_score = raw.get("base_score")
            if ai_score is None:
                ai_score = raw.get("final_score")
            if ai_score is None:
                ai_score = raw.get("score")
            candidate_score = raw.get("candidate_score")
            if candidate_score is None:
                candidate_score = raw.get("final_score")
            if candidate_score is None:
                candidate_score = raw.get("score")
            ai_reason = raw.get("ai_reason") or raw.get("reason") or raw.get("selection_penalty_reason") or ""
            score_reason = raw.get("score_reason") or raw.get("ranking_reason") or raw.get("selection_penalty_reason") or ""
            market_context = raw.get("market_context") if isinstance(raw.get("market_context"), dict) else {}
            selection_stage = str(
                raw.get("selection_stage")
                or market_context.get("selection_stage")
                or (report.get("settings") or {}).get("selection_stage")
                or ""
            )
            metadata = {
                "fallback_used": bool(raw.get("fallback_used", False)),
                "settings_selection_stage": str((report.get("settings") or {}).get("selection_stage") or ""),
                "selection_stage": selection_stage,
                "market_selection_stage": selection_stage,
                "top_n": int((report.get("settings") or {}).get("top_n") or len(candidate_rows) or 0),
                "daily_data_as_of": str(raw.get("daily_data_as_of") or report.get("daily_data_as_of") or market_context.get("daily_data_as_of") or ""),
                "premarket_snapshot_at": str(raw.get("premarket_snapshot_at") or report.get("premarket_snapshot_at") or market_context.get("premarket_snapshot_at") or ""),
                "last_completed_session": str(raw.get("last_completed_session") or report.get("last_completed_session") or market_context.get("last_completed_session") or ""),
                "freshness_status": str(raw.get("freshness_status") or report.get("freshness_status") or market_context.get("freshness_status") or ""),
                "stale_reason": str(raw.get("stale_reason") or report.get("stale_reason") or market_context.get("stale_reason") or ""),
                "trading_eligible": bool(raw.get("trading_eligible", report.get("trading_eligible", False))),
                "current_session": str(raw.get("current_session") or market_context.get("current_session") or ""),
                "previous_completed_session": str(raw.get("previous_completed_session") or market_context.get("previous_completed_session") or ""),
                "next_session": str(raw.get("next_session") or market_context.get("next_session") or ""),
                "is_market_holiday": bool(raw.get("is_market_holiday", market_context.get("is_market_holiday", False))),
                "is_premarket": bool(raw.get("is_premarket", market_context.get("is_premarket", False))),
                "is_regular_session": bool(raw.get("is_regular_session", market_context.get("is_regular_session", False))),
                "is_after_hours": bool(raw.get("is_after_hours", market_context.get("is_after_hours", False))),
                "quote_age_seconds": raw.get("quote_age_seconds") if raw.get("quote_age_seconds") is not None else report.get("quote_age_seconds") or market_context.get("quote_age_seconds"),
                "benchmark_data_as_of": raw.get("benchmark_data_as_of") or report.get("benchmark_data_as_of") or market_context.get("benchmark_data_as_of") or {},
                "premarket_change_pct": raw.get("premarket_change_pct") or report.get("premarket_change_pct") or market_context.get("premarket_change_pct"),
                "gap_pct": raw.get("gap_pct") or report.get("gap_pct") or market_context.get("gap_pct"),
                "premarket_volume": raw.get("premarket_volume") or report.get("premarket_volume") or market_context.get("premarket_volume"),
                "spread_pct": raw.get("spread_pct") or report.get("spread_pct") or market_context.get("spread_pct"),
                "daily_data_status": raw.get("daily_data_status") or report.get("daily_data_status") or market_context.get("daily_data_status") or "",
            }
            record = CandidateRecord.from_ai_candidate(
                symbol=symbol,
                selected_at=selected_at,
                source=str(raw.get("source") or "ai_selector"),
                ai_score=float(ai_score) if ai_score is not None else None,
                candidate_score=float(candidate_score) if candidate_score is not None else None,
                liquidity_score=raw.get("liquidity_score"),
                trend_score=raw.get("trend_score") or raw.get("trend_fit_score"),
                volatility_score=raw.get("volatility_score"),
                risk_score=raw.get("risk_score") or raw.get("drawdown_safety_score"),
                strategy_fit_score=raw.get("strategy_fit_score"),
                recommended_strategy=str(raw.get("recommended_strategy") or ""),
                score_reason=str(score_reason or ""),
                ai_reason=str(ai_reason or ""),
                asset_type=str(raw.get("asset_type") or raw.get("symbol_class") or "") or None,
                benchmarks=tuple(raw.get("benchmarks") or raw.get("benchmark_symbols") or ()),
                strategy_family=str(raw.get("strategy_family") or "") or None,
                risk_profile=str(raw.get("risk_profile") or "") or None,
                timeframe=str(raw.get("timeframe") or report.get("timeframe") or report.get("settings", {}).get("timeframe") or "15m"),
                market=str(raw.get("market") or "US"),
                metadata=metadata,
            )
            records.append(record)
        self.save_candidates(records)
        return records

    def save_candidates(self, records: Iterable[CandidateRecord]) -> list[CandidateRecord]:
        current = self.load_latest_candidates()
        latest_map = {record.candidate_id: record for record in current}
        updates: list[CandidateRecord] = []
        for record in records:
            if not isinstance(record, CandidateRecord):
                raise TypeError("record must be a CandidateRecord")
            self._validate_initial_record(record)
            record.touch()
            latest_map[record.candidate_id] = record
            updates.append(record)
            self._append_jsonl(self.history_path, self._history_event(record, event_type="candidate_saved", previous_status=None, reason="created"))
        self._write_jsonl(self.candidates_path, [item.to_dict() for item in latest_map.values()])
        self._write_summary_csv(self._sorted_records(latest_map.values()))
        self._sync_candidate_performance(self._sorted_records(latest_map.values()))
        return updates

    def transition(
        self,
        candidate_id: str,
        new_status: ValidationStatus | str,
        *,
        reason: str = "",
        evidence_status: EvidenceStatus | str | None = None,
        profitability_status: ProfitabilityStatus | str | None = None,
        deployment_status: DeploymentStatus | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CandidateRecord:
        latest = self._load_latest_map()
        candidate = latest.get(candidate_id)
        if candidate is None:
            raise CandidateTransitionError(f"candidate_not_found:{candidate_id}")
        previous_status = candidate.validation_status
        normalized_status = self._coerce_validation_status(new_status)
        assert_transition_allowed(previous_status, normalized_status.value)
        self._validate_transition_requirements(candidate, normalized_status, metadata or {})
        updated = candidate.clone(
            validation_status=normalized_status.value,
            rejection_reason=reason if normalized_status == ValidationStatus.REJECTED else candidate.rejection_reason,
        )
        merged_metadata = dict(candidate.metadata or {})
        merged_metadata.update(dict(metadata or {}))
        updated.metadata = merged_metadata
        if evidence_status is not None:
            updated.evidence_status = self._coerce_evidence_status(evidence_status).value
        if profitability_status is not None:
            updated.profitability_status = self._coerce_profitability_status(profitability_status).value
        if deployment_status is not None:
            updated.deployment_status = self._coerce_deployment_status(deployment_status).value
        if normalized_status == ValidationStatus.REJECTED:
            updated.deployment_status = DeploymentStatus.INELIGIBLE.value
        updated.touch()
        latest[candidate_id] = updated
        self._write_jsonl(self.candidates_path, [item.to_dict() for item in latest.values()])
        self._append_jsonl(self.history_path, self._history_event(updated, event_type="validation_transition", previous_status=previous_status, reason=reason, metadata=metadata or {}))
        self._write_summary_csv(self._sorted_records(latest.values()))
        self._sync_candidate_performance(self._sorted_records(latest.values()))
        return updated

    def reject(self, candidate_id: str, reason: str, *, metadata: dict[str, Any] | None = None) -> CandidateRecord:
        return self.transition(candidate_id, ValidationStatus.REJECTED, reason=reason, metadata=metadata)

    def _load_latest_map(self) -> dict[str, CandidateRecord]:
        latest: dict[str, CandidateRecord] = {}
        for record in self.load_latest_candidates():
            latest[record.candidate_id] = record
        return latest

    def _sorted_records(self, records: Iterable[CandidateRecord]) -> list[CandidateRecord]:
        return sorted(list(records), key=lambda item: (item.updated_at or "", item.candidate_id), reverse=True)

    def _validate_initial_record(self, record: CandidateRecord) -> None:
        errors = record.validate_initial_state()
        if errors:
            raise CandidateTransitionError(", ".join(errors))

    def _validate_transition_requirements(
        self,
        record: CandidateRecord,
        new_status: ValidationStatus,
        metadata: dict[str, Any],
    ) -> None:
        asset_type = record.asset_type
        benchmarks = record.benchmarks
        strategy_family = record.strategy_family
        risk_profile = record.risk_profile
        if new_status == ValidationStatus.CLASSIFIED:
            if asset_type not in ALLOWED_SYMBOL_CLASSES:
                raise CandidateTransitionError("asset_type_unclassified")
        elif new_status == ValidationStatus.BENCHMARK_ASSIGNED:
            if not asset_type:
                raise CandidateTransitionError("asset_type_required_for_benchmark_assignment")
            if not benchmarks:
                raise CandidateTransitionError("benchmark_required")
        elif new_status == ValidationStatus.STRATEGY_ASSIGNED:
            if not benchmarks:
                raise CandidateTransitionError("benchmark_required")
            if not strategy_family:
                raise CandidateTransitionError("strategy_family_required")
            if asset_type in {"leveraged_etf", "inverse_etf"} and risk_profile not in {"strict", "very_strict"}:
                raise CandidateTransitionError("risk_profile_required_for_leveraged_or_inverse")
        elif new_status == ValidationStatus.PENDING_DATA_VALIDATION:
            if not benchmarks:
                raise CandidateTransitionError("benchmark_required")
            if not strategy_family:
                raise CandidateTransitionError("strategy_family_required")
        elif new_status == ValidationStatus.DATA_VALID:
            if not bool(metadata.get("benchmark_valid", False)):
                raise CandidateTransitionError("benchmark_invalid")
            if not bool(metadata.get("eligible_for_backtest", False)):
                raise CandidateTransitionError("eligible_for_backtest_required")
            if bool(metadata.get("future_data_risk", True)):
                raise CandidateTransitionError("future_data_risk_must_be_false")
            if _normalize_text(metadata.get("reconciliation_status")).upper() != "OK":
                raise CandidateTransitionError("reconciliation_must_be_ok")
        elif new_status == ValidationStatus.PENDING_BACKTEST:
            if _normalize_text(metadata.get("data_status")).upper() != ValidationStatus.DATA_VALID.value:
                raise CandidateTransitionError("data_must_be_valid")
        elif new_status == ValidationStatus.BACKTEST_COMPLETE:
            if not bool(metadata.get("backtest_complete", False)):
                raise CandidateTransitionError("backtest_not_complete")
        elif new_status == ValidationStatus.PENDING_WALK_FORWARD:
            if _normalize_text(metadata.get("backtest_status")).upper() != ValidationStatus.BACKTEST_COMPLETE.value:
                raise CandidateTransitionError("backtest_must_complete")
        elif new_status == ValidationStatus.WALK_FORWARD_COMPLETE:
            if not bool(metadata.get("walk_forward_complete", False)):
                raise CandidateTransitionError("walk_forward_not_complete")
        elif new_status == ValidationStatus.PENDING_SHADOW:
            if _normalize_text(metadata.get("dataset_benchmark_status")).upper() != "VALID":
                raise CandidateTransitionError("dataset_benchmark_invalid")
            if not bool(metadata.get("eligible_for_backtest", False)):
                raise CandidateTransitionError("eligible_for_backtest_required")
            if bool(metadata.get("future_data_risk", True)):
                raise CandidateTransitionError("future_data_risk_must_be_false")
            if _normalize_text(metadata.get("reconciliation_status")).upper() != "OK":
                raise CandidateTransitionError("reconciliation_must_be_ok")
            if _normalize_text(metadata.get("evidence_status")).upper() not in {EvidenceStatus.ELIGIBLE.value, EvidenceStatus.INSUFFICIENT_EVIDENCE.value}:
                raise CandidateTransitionError("evidence_status_must_be_sufficient_or_eligible")
            if _normalize_text(metadata.get("backtest_status")).upper() != ValidationStatus.BACKTEST_COMPLETE.value:
                raise CandidateTransitionError("backtest_must_complete")
            if _normalize_text(metadata.get("walk_forward_status")).upper() != ValidationStatus.WALK_FORWARD_COMPLETE.value:
                raise CandidateTransitionError("walk_forward_must_complete")
        elif new_status == ValidationStatus.SHADOW_OBSERVING:
            if _normalize_text(metadata.get("shadow_state")).upper() not in {"SAFE", "STALE"}:
                raise CandidateTransitionError("shadow_state_invalid")
        elif new_status == ValidationStatus.SHADOW_COMPLETE:
            if _normalize_text(metadata.get("shadow_audit_status")).upper() != "OK":
                raise CandidateTransitionError("shadow_audit_failed")
        elif new_status == ValidationStatus.PAPER_ELIGIBLE:
            if _normalize_text(metadata.get("evidence_status")).upper() != EvidenceStatus.ELIGIBLE.value:
                raise CandidateTransitionError("evidence_not_eligible")
            if _normalize_text(metadata.get("profitability_status")).upper() != ProfitabilityStatus.ELIGIBLE.value:
                raise CandidateTransitionError("profitability_not_eligible")
            if _normalize_text(metadata.get("deployment_status")).upper() != DeploymentStatus.ELIGIBLE.value:
                raise CandidateTransitionError("deployment_not_eligible")
            if float(metadata.get("aggregate_oos_return", 0.0) or 0.0) <= 0:
                raise CandidateTransitionError("aggregate_oos_return_must_be_positive")
            if bool(metadata.get("benchmark_sensitive", False)):
                raise CandidateTransitionError("benchmark_sensitivity_too_high")
            if not bool(metadata.get("shadow_complete", False)):
                raise CandidateTransitionError("shadow_must_complete")
            if _normalize_text(metadata.get("shadow_reconciliation_status")).upper() != "OK":
                raise CandidateTransitionError("shadow_reconciliation_failed")
            if bool(metadata.get("broker_write_called", False)):
                raise CandidateTransitionError("broker_write_called")
            if bool(metadata.get("safety_alert", False)):
                raise CandidateTransitionError("safety_alert_present")
        elif new_status == ValidationStatus.LIVE_ELIGIBLE:
            if _normalize_text(metadata.get("paper_status")).upper() != ValidationStatus.PAPER_ELIGIBLE.value:
                raise CandidateTransitionError("paper_must_be_eligible")
            if not bool(metadata.get("paper_approved_by_human", False)):
                raise CandidateTransitionError("human_approval_required")
            if float(metadata.get("paper_total_return", 0.0) or 0.0) <= 0:
                raise CandidateTransitionError("paper_total_return_must_be_positive")
            if float(metadata.get("paper_max_drawdown", 1.0) or 1.0) >= float(metadata.get("paper_max_drawdown_limit", 0.2) or 0.2):
                raise CandidateTransitionError("paper_drawdown_too_high")
        elif new_status in {ValidationStatus.DATA_INVALID, ValidationStatus.BACKTEST_FAILED, ValidationStatus.WALK_FORWARD_FAILED, ValidationStatus.PAPER_INELIGIBLE, ValidationStatus.LIVE_INELIGIBLE}:
            if not _normalize_text(metadata.get("reason")) and not _normalize_text(metadata.get("rejection_reason")):
                raise CandidateTransitionError("rejection_reason_required")

    def _coerce_validation_status(self, value: ValidationStatus | str) -> ValidationStatus:
        if isinstance(value, ValidationStatus):
            return value
        return ValidationStatus(str(value))

    def _coerce_evidence_status(self, value: EvidenceStatus | str) -> EvidenceStatus:
        if isinstance(value, EvidenceStatus):
            return value
        return EvidenceStatus(str(value))

    def _coerce_profitability_status(self, value: ProfitabilityStatus | str) -> ProfitabilityStatus:
        if isinstance(value, ProfitabilityStatus):
            return value
        return ProfitabilityStatus(str(value))

    def _coerce_deployment_status(self, value: DeploymentStatus | str) -> DeploymentStatus:
        if isinstance(value, DeploymentStatus):
            return value
        return DeploymentStatus(str(value))

    def _history_event(
        self,
        candidate: CandidateRecord,
        *,
        event_type: str,
        previous_status: ValidationStatus | str | None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = candidate.to_dict()
        payload.update(
            {
                "event_type": event_type,
                "from_status": previous_status.value if isinstance(previous_status, ValidationStatus) else previous_status,
                "to_status": candidate.validation_status,
                "reason": reason,
                "at": _utc_now_iso(),
                "metadata": dict(metadata or {}),
            }
        )
        return payload


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace('"', '""')
    return f'"{text}"'
