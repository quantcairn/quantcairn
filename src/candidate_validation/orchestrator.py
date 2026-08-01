"""Candidate Validation Orchestrator

Drives research-candidate validation from AI_CANDIDATE through
DATA_VALID entirely offline.  All writes use _apply_transition /
_save_field_updates which bypass the store's initial-state validation.

No trading flags are set, no broker is used, no TOP config is written.
PAPER_ELIGIBLE / LIVE_ELIGIBLE are never auto-assigned.
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .models import (
    CandidateRecord,
    CandidateTransitionError,
    ValidationStatus,
    assert_transition_allowed,
    default_candidate_for_symbol,
)
from .store import CandidateValidationStore
from ..openalpha.selection_bundle import load_committed_selection_bundle


PROJECT_DIR = Path(
    os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2]))
).resolve()
CANDIDATE_ROOT = PROJECT_DIR / "artifacts" / "candidates"
VALIDATION_ROOT = PROJECT_DIR / "artifacts" / "candidates" / "validation"
ORCHESTRATOR_LOCK_PATH = CANDIDATE_ROOT / ".orchestrator.lock"
ORCHESTRATOR_AUDIT_PATH = CANDIDATE_ROOT / "orchestrator_run_audit.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        try:
            return _jsonable(value.value)
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _atomic_write_jsonl(path: Path, row: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    line = json.dumps(_jsonable(row), ensure_ascii=False, default=str)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(existing)
            h.write(line + "\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


# ── store helpers (bypass validate_initial_state) ────────────────────


def _save_field_updates(store: CandidateValidationStore, record: CandidateRecord) -> None:
    """Update a record's fields in the store without initial-state validation."""
    candidates = store.load_latest_candidates()
    updated: dict[str, CandidateRecord] = {}
    for r in candidates:
        updated[r.candidate_id] = r
    if record.candidate_id not in updated:
        return
    updated[record.candidate_id] = record
    store._write_jsonl(
        store.candidates_path,
        [r.to_dict() for r in store._sorted_records(updated.values())],
    )
    store._append_jsonl(
        store.history_path,
        store._history_event(
            record, event_type="field_update",
            previous_status=str(record.validation_status), reason="orchestrator",
        ),
    )
    store._write_summary_csv(store._sorted_records(updated.values()))


def _apply_transition(
    store: CandidateValidationStore,
    candidate_id: str,
    new_status: ValidationStatus,
    *,
    reason: str = "",
) -> CandidateRecord:
    """Transition a candidate status bypassing the store's initial-state check."""
    return store.transition(candidate_id, new_status, reason=reason, metadata={})


def _dry_advance(record: CandidateRecord, status: ValidationStatus) -> CandidateRecord:
    """Return a clone with a new status for dry-run evaluation."""
    return CandidateRecord.from_dict({
        **record.to_dict(), "validation_status": status.value,
    })


# ── classification / assignment ─────────────────────────────────────


def _classify(record: CandidateRecord) -> CandidateRecord:
    """Assign asset_type from Shadow catalog."""
    if record.asset_type:
        return record
    from ..shadow.universe import SHADOW_SYMBOL_CATALOG, symbol_class_for
    at = str(
        SHADOW_SYMBOL_CATALOG.get(record.symbol, {}).get("symbol_class", "")
        or symbol_class_for(record.symbol)
        or ""
    ).strip()
    record.metadata["orchestrator_classified_at"] = _utc_now_iso()
    record.metadata["orchestrator_classified_source"] = "shadow_universe"
    if at:
        return CandidateRecord.from_dict({**record.to_dict(), "asset_type": at})
    return record


def _assign_benchmark(record: CandidateRecord) -> CandidateRecord:
    """Assign default benchmarks from Shadow catalog."""
    if record.benchmarks:
        return record
    from ..shadow.universe import default_benchmarks_for
    bm = default_benchmarks_for(record.symbol)
    record.metadata["orchestrator_benchmark_assigned_at"] = _utc_now_iso()
    record.metadata["orchestrator_benchmark_source"] = "shadow_default"
    if bm:
        return CandidateRecord.from_dict({**record.to_dict(), "benchmarks": list(bm)})
    return record


def _assign_strategy(record: CandidateRecord) -> CandidateRecord:
    """Assign strategy_family and risk_profile — safe, no API."""
    from ..shadow.universe import strategy_family_for
    payload = dict(record.to_dict())
    changed = False
    if not record.strategy_family:
        sf = strategy_family_for(record.symbol)
        record.metadata["orchestrator_strategy_assigned_at"] = _utc_now_iso()
        record.metadata["orchestrator_strategy_source"] = "shadow_default"
        at = str(payload.get("asset_type", "")).lower()
        fallback = {
            "common_stock": "equity_mean_reversion",
            "leveraged_etf": "leveraged_range",
            "inverse_etf": "inverse_range",
            "index_etf": "index_follow",
        }
        payload["strategy_family"] = sf or fallback.get(at, "mean_reversion")
        changed = True
    if not record.recommended_strategy:
        payload["recommended_strategy"] = payload.get("strategy_family") or "mean_reversion"
        changed = True
    if not record.risk_profile:
        rp = "strict" if record.is_leveraged_or_inverse() else "balanced"
        payload["risk_profile"] = rp
        changed = True
    if changed:
        return CandidateRecord.from_dict(payload)
    return record


# ── auto-progression stages ──────────────────────────────────────────


class OrchestratorPhase(str, Enum):
    CLASSIFY = "CLASSIFY"
    BENCHMARK = "BENCHMARK"
    STRATEGY = "STRATEGY"
    DATA_VALIDATION = "DATA_VALIDATION"


def _stage_classify(record, store, *, dry_run=False):
    result: dict[str, Any] = {"stage": "CLASSIFY", "action": "classified"}
    record = _classify(record)
    assert_transition_allowed(record.validation_status, ValidationStatus.CLASSIFIED)
    if not dry_run:
        _save_field_updates(store, record)
        record = _apply_transition(store, record.candidate_id, ValidationStatus.CLASSIFIED, reason="orchestrator_auto_classify")
    else:
        record = _dry_advance(record, ValidationStatus.CLASSIFIED)
    result["asset_type"] = record.asset_type
    return record, result


def _stage_benchmark(record, store, *, dry_run=False):
    result: dict[str, Any] = {"stage": "BENCHMARK_ASSIGN", "action": "assigned"}
    record = _assign_benchmark(record)
    assert_transition_allowed(record.validation_status, ValidationStatus.BENCHMARK_ASSIGNED)
    if not dry_run:
        _save_field_updates(store, record)
        record = _apply_transition(store, record.candidate_id, ValidationStatus.BENCHMARK_ASSIGNED, reason="orchestrator_auto_benchmark")
    else:
        record = _dry_advance(record, ValidationStatus.BENCHMARK_ASSIGNED)
    result["benchmarks"] = list(record.benchmarks)
    return record, result


def _stage_strategy(record, store, *, dry_run=False):
    result: dict[str, Any] = {"stage": "STRATEGY_ASSIGN", "action": "assigned"}
    record = _assign_strategy(record)
    assert_transition_allowed(record.validation_status, ValidationStatus.STRATEGY_ASSIGNED)
    if not dry_run:
        _save_field_updates(store, record)
        record = _apply_transition(store, record.candidate_id, ValidationStatus.STRATEGY_ASSIGNED, reason="orchestrator_auto_strategy")
    else:
        record = _dry_advance(record, ValidationStatus.STRATEGY_ASSIGNED)
    result["strategy_family"] = record.strategy_family
    result["risk_profile"] = record.risk_profile
    return record, result


def _stage_pending_dv(record, store, *, dry_run=False):
    result: dict[str, Any] = {"stage": "PENDING_DATA_VALIDATION", "action": "marked_pending"}
    assert_transition_allowed(record.validation_status, ValidationStatus.PENDING_DATA_VALIDATION)
    if not dry_run:
        record = _apply_transition(store, record.candidate_id, ValidationStatus.PENDING_DATA_VALIDATION, reason="orchestrator_auto_pending_dv")
    else:
        record = _dry_advance(record, ValidationStatus.PENDING_DATA_VALIDATION)
    return record, result


def _stage_run_dv(record, store, *, dry_run=False):
    """Run dataset validation and transition to DATA_VALID / DATA_INVALID."""
    from .dataset_validation_runner import validate_candidate_dataset

    result: dict[str, Any] = {"stage": "DATA_VALIDATION", "action": "validation_ran"}
    if record.validation_status not in {
        ValidationStatus.PENDING_DATA_VALIDATION.value,
        ValidationStatus.STRATEGY_ASSIGNED.value,
    }:
        raise CandidateTransitionError(f"data_validation_requires_pending:got_{record.validation_status}")
    try:
        vr = validate_candidate_dataset(
            candidate_id=record.candidate_id,
            candidate_store=CANDIDATE_ROOT,
            output_dir=VALIDATION_ROOT,
            download_missing=False,
            dry_run=dry_run,
        )
    except CandidateTransitionError:
        raise
    except Exception as exc:
        raise CandidateTransitionError(f"data_validation_error:{type(exc).__name__}:{exc}") from exc
    result["validation"] = dict(vr or {})
    ov = (vr or {}).get("overall") or {}
    st = str(ov.get("status") or "").strip().upper()
    if st == ValidationStatus.DATA_VALID.value and not dry_run:
        record = _apply_transition(store, record.candidate_id, ValidationStatus.DATA_VALID, reason="orchestrator_auto_data_valid")
        result["action"] = "data_valid"
    elif st == ValidationStatus.DATA_INVALID.value and not dry_run:
        record = _apply_transition(store, record.candidate_id, ValidationStatus.DATA_INVALID, reason="orchestrator_data_invalid")
        result["action"] = "data_invalid"
    elif dry_run:
        result["action"] = "dry_run"
    else:
        raise CandidateTransitionError(f"data_validation_unexpected_status:{st}")
    return record, result


AUTO_STAGES: list[tuple[OrchestratorPhase, callable]] = [
    (OrchestratorPhase.CLASSIFY, _stage_classify),
    (OrchestratorPhase.BENCHMARK, _stage_benchmark),
    (OrchestratorPhase.STRATEGY, _stage_strategy),
    (OrchestratorPhase.DATA_VALIDATION, _stage_pending_dv),
]


# ── orchestrator ─────────────────────────────────────────────────────


@dataclass(slots=True)
class CandidateValidationOrchestrator:
    store: CandidateValidationStore = field(
        default_factory=lambda: CandidateValidationStore(CANDIDATE_ROOT)
    )
    project_dir: Path = PROJECT_DIR
    max_attempts: int = 3
    cooldown_minutes: int = 30

    def run(
        self,
        *,
        selection_bundle: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        rid = f"orch_{_utc_now().strftime('%Y%m%dT%H%M%S')}_{os.urandom(4).hex()}"
        started_at = _utc_now_iso()
        rep: dict[str, Any] = {
            "run_id": rid, "started_at": started_at, "dry_run": dry_run,
            "candidates_processed": 0, "candidates_advanced": 0,
            "candidates_skipped": 0, "candidates_failed": 0,
            "phases_executed": [], "candidate_results": [], "errors": [],
            "status": "OK", "applied": not dry_run,
            "trade_api_used": False, "broker_used": False, "order_path_used": False,
            "paper_eligible_auto": False, "live_eligible_auto": False,
        }
        if not self._acquire_lock(rid):
            rep["status"] = "SKIPPED_LOCKED"
            rep["errors"].append("orchestrator_lock_active")
            return rep
        try:
            bundle = selection_bundle or load_committed_selection_bundle(self.project_dir)
            candidates = self._extract(bundle or {})
            if not candidates:
                rep["status"] = "NO_CANDIDATES"
                return rep
            records = self._ingest(candidates, dry_run=dry_run)
            for rec in records:
                try:
                    cr = self._process(rec, dry_run=dry_run)
                    rep["candidate_results"].append(cr)
                    rep["candidates_processed"] += 1
                    if cr.get("advanced"):
                        rep["candidates_advanced"] += 1
                    else:
                        rep["candidates_skipped"] += 1
                except Exception as exc:
                    rep["candidates_failed"] += 1
                    rep["candidate_results"].append({
                        "candidate_id": getattr(rec, "candidate_id", "?"),
                        "symbol": getattr(rec, "symbol", "?"),
                        "error": f"{type(exc).__name__}:{exc}",
                        "traceback": traceback.format_exc()[-2000:],
                    })
                    rep["errors"].append(f"{getattr(rec,'symbol','?')}:{type(exc).__name__}:{exc}")
            rep["phases_executed"] = sorted({
                ph for cr in rep.get("candidate_results", []) or []
                for ph in (cr.get("phases_executed") or [])
            })
            rep["completed_at"] = _utc_now_iso()
            return rep
        finally:
            self._release_lock(rid)
            _atomic_write_jsonl(ORCHESTRATOR_AUDIT_PATH, rep)

    # ── extraction ───────────────────────────────────────────────

    def _extract(self, bundle: dict[str, Any]) -> list[dict[str, Any]]:
        res: list[dict[str, Any]] = []
        if not isinstance(bundle, dict):
            return res
        seen: set[str] = set()
        for key in ("research_top_candidates", "top3", "top5", "top10"):
            for container in (bundle.get("report") or {}, bundle):
                src = (container or {}).get(key)
                if not isinstance(src, (list, tuple)):
                    continue
                for item in src:
                    if not isinstance(item, dict):
                        continue
                    ticker = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
                    if not ticker or ticker in seen:
                        continue
                    seen.add(ticker)
                    if not item.get("formal_scoring_eligibility"):
                        continue
                    adm = str(item.get("trade_admission_status") or item.get("validation_status") or "").strip().upper()
                    if adm in {"PAPER_ELIGIBLE", "LIVE_ELIGIBLE", "TRADABLE"}:
                        continue
                    res.append(dict(item))
        return res

    def _ingest(self, candidates: list[dict[str, Any]], *, dry_run: bool) -> list[CandidateRecord]:
        cur = {r.candidate_id: r for r in self.store.load_latest_candidates()}
        by_sym: dict[str, list[CandidateRecord]] = {}
        for r in cur.values():
            by_sym.setdefault(r.symbol.upper(), []).append(r)
        records: list[CandidateRecord] = []
        for item in candidates:
            sym = _safe_text(item.get("symbol") or item.get("ticker")).upper()
            if not sym:
                continue
            sym_us = sym if "." in sym else f"{sym}.US"
            existing = by_sym.get(sym_us, [])
            if existing:
                rec = existing[0]
                rec.metadata["last_research_seen_at"] = _utc_now_iso()
                rec.metadata["research_selection_run_id"] = str(item.get("selection_run_id") or "")
            else:
                rec = default_candidate_for_symbol(
                    symbol=sym_us, selected_at=_utc_now_iso(), source="ai_selector",
                    ai_score=item.get("ai_score") or item.get("score"),
                    ai_reason=str(item.get("ai_reason") or ""), timeframe="15m",
                )
                rec.metadata["orchestrator_created_at"] = _utc_now_iso()
                rec.metadata["research_selection_run_id"] = str(item.get("selection_run_id") or "")
                if not dry_run:
                    self.store.save_candidates([rec])
            records.append(rec)
        return records

    # ── processing ────────────────────────────────────────────────

    def _should_skip(self, record: CandidateRecord) -> str:
        if record.trading_enabled or record.paper_enabled or record.live_enabled:
            return "trading_flags_active"
        if record.validation_status in {
            ValidationStatus.PAPER_ELIGIBLE.value, ValidationStatus.LIVE_ELIGIBLE.value,
            ValidationStatus.REJECTED.value, ValidationStatus.DATA_INVALID.value,
            ValidationStatus.BACKTEST_FAILED.value, ValidationStatus.WALK_FORWARD_FAILED.value,
            ValidationStatus.SHADOW_OBSERVING.value, ValidationStatus.SHADOW_COMPLETE.value,
        }:
            return f"terminal_status:{record.validation_status}"
        n = int(record.metadata.get("orchestrator_attempt_count", 0) or 0)
        la = record.metadata.get("orchestrator_last_attempt_at") or ""
        if n >= self.max_attempts and la:
            try:
                t = datetime.fromisoformat(str(la))
                if _utc_now() - t < timedelta(minutes=self.cooldown_minutes):
                    return "cooldown_active"
            except (ValueError, TypeError):
                pass
        return ""

    def _process(self, record: CandidateRecord, *, dry_run: bool) -> dict[str, Any]:
        r = {
            "candidate_id": record.candidate_id, "symbol": record.symbol,
            "started_at": _utc_now_iso(), "advanced": False,
            "phases_executed": [], "final_status": str(record.validation_status),
        }
        if (sk := self._should_skip(record)):
            r["skipped"] = True
            r["skip_reason"] = sk
            return r

        # Record attempt before processing
        if not dry_run:
            n = int(record.metadata.get("orchestrator_attempt_count", 0) or 0)
            record.metadata["orchestrator_attempt_count"] = str(n + 1)
            record.metadata["orchestrator_last_attempt_at"] = _utc_now_iso()
            _save_field_updates(self.store, record)

        st = str(record.validation_status)
        # Determine start index
        start = 0
        if st == ValidationStatus.CLASSIFIED.value:
            start = 1
        elif st == ValidationStatus.BENCHMARK_ASSIGNED.value:
            start = 2
        elif st == ValidationStatus.STRATEGY_ASSIGNED.value:
            start = 3

        try:
            if st == ValidationStatus.PENDING_DATA_VALIDATION.value:
                try:
                    record, dv = _stage_run_dv(record, self.store, dry_run=dry_run)
                    r["phases_executed"].append("DATA_VALIDATION")
                    r["advanced"] = True
                except CandidateTransitionError as exc:
                    r["data_validation_error"] = str(exc)
            elif st == ValidationStatus.DATA_VALID.value:
                r["note"] = "data_valid_ready_for_manual_backtest_admission"
            elif st in {
                ValidationStatus.AI_CANDIDATE.value,
                ValidationStatus.CLASSIFIED.value,
                ValidationStatus.BENCHMARK_ASSIGNED.value,
                ValidationStatus.STRATEGY_ASSIGNED.value,
            }:
                for idx in range(start, len(AUTO_STAGES)):
                    phase, fn = AUTO_STAGES[idx]
                    record, sr = fn(record, self.store, dry_run=dry_run)
                    r["phases_executed"].append(phase.value)
                    r["advanced"] = True
                    if phase == OrchestratorPhase.DATA_VALIDATION and not dry_run:
                        if record.validation_status in {
                            ValidationStatus.PENDING_DATA_VALIDATION.value,
                            ValidationStatus.STRATEGY_ASSIGNED.value,
                        }:
                            try:
                                record, dv = _stage_run_dv(record, self.store, dry_run=dry_run)
                                sr["data_validation"] = dv
                            except CandidateTransitionError as exc:
                                r["data_validation_error"] = str(exc)
                                break
                    if sr.get("action") in {"data_invalid", "data_validation_failed"}:
                        break
            else:
                r["skipped"] = True
                r["skip_reason"] = f"unhandled_status:{st}"
        except CandidateTransitionError:
            # On failure, keep attempt metadata so cooldown can engage
            raise
        except Exception:
            # Preserve attempt count on unexpected errors too
            raise
        else:
            # On success, clear attempt count in dry_run or in the record
            if not dry_run and r.get("advanced"):
                record.metadata["orchestrator_attempt_count"] = "0"
                _save_field_updates(self.store, record)

        r["final_status"] = str(record.validation_status) if dry_run else str(
            (self.store.get_candidate(record.candidate_id) or record).validation_status
        )
        r["completed_at"] = _utc_now_iso()
        return r

    # ── lock ──────────────────────────────────────────────────────

    def _acquire_lock(self, rid: str) -> bool:
        ORCHESTRATOR_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        p = {"run_id": rid, "pid": os.getpid(), "acquired_at": _utc_now_iso()}
        try:
            fd = os.open(str(ORCHESTRATOR_LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                if (_utc_now().timestamp() - ORCHESTRATOR_LOCK_PATH.stat().st_mtime) > 3600:
                    ORCHESTRATOR_LOCK_PATH.unlink(missing_ok=True)
                    fd = os.open(str(ORCHESTRATOR_LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                else:
                    return False
            except (OSError, ValueError):
                return False
        try:
            os.write(fd, json.dumps(p, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _release_lock(self, rid: str) -> None:
        try:
            if ORCHESTRATOR_LOCK_PATH.exists():
                data = json.loads(ORCHESTRATOR_LOCK_PATH.read_text(encoding="utf-8"))
                if str(data.get("run_id") or "") == rid:
                    ORCHESTRATOR_LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass
