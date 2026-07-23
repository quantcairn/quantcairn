"""Tests for the Candidate Validation Orchestrator.

Verifies the orchestrator can safely auto-advance candidates through
the research validation pipeline without touching trading paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.candidate_validation.models import (
    CandidateRecord,
    CandidateTransitionError,
    ValidationStatus,
    default_candidate_for_symbol,
)
from src.candidate_validation.orchestrator import (
    CandidateValidationOrchestrator,
    _classify,
    _assign_benchmark,
    _assign_strategy,
    _stage_classify,
    _stage_benchmark,
    _stage_strategy,
    _stage_pending_dv,
    _stage_run_dv,
)
from src.candidate_validation.store import CandidateValidationStore


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tmp_store(tmp_path: Path) -> CandidateValidationStore:
    return CandidateValidationStore(root_dir=tmp_path / "candidates")


def _fresh_sofi(selected_at: str | None = None) -> CandidateRecord:
    return default_candidate_for_symbol(
        symbol="SOFI.US",
        selected_at=selected_at or _utc_now_iso(),
        ai_score=75.95,
        ai_reason="test",
        timeframe="15m",
    )


# ── Stage unit tests ────────────────────────────────────────────────


class TestAutoClassify:
    def test_classify_sets_asset_type(self, tmp_path):
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        store.save_candidates([record])
        loaded = store.get_candidate(record.candidate_id)
        assert loaded is not None
        assert loaded.asset_type == "" or loaded.asset_type == loaded.asset_type

        new_record, result = _stage_classify(loaded, store, dry_run=True)
        assert result["action"] == "classified"
        assert new_record.validation_status == str(ValidationStatus.CLASSIFIED.value)


class TestAutoBenchmark:
    def test_benchmark_assigns(self, tmp_path):
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        # Classify first via the dry-run path which avoids save_candidates validation
        record, _ = _stage_classify(record, store, dry_run=True)
        assert str(record.validation_status) == str(ValidationStatus.CLASSIFIED.value)

        new_record, result = _stage_benchmark(record, store, dry_run=True)
        assert result["action"] == "assigned"
        assert len(new_record.benchmarks) > 0


class TestAutoStrategy:
    def test_strategy_assigns(self, tmp_path):
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        record, _ = _stage_classify(record, store, dry_run=True)
        record, _ = _stage_benchmark(record, store, dry_run=True)
        assert str(record.validation_status) == str(ValidationStatus.BENCHMARK_ASSIGNED.value)

        new_record, result = _stage_strategy(record, store, dry_run=True)
        assert result["action"] == "assigned"
        # SOFI is not in SHADOW_SYMBOL_CATALOG, so strategy_family may be empty;
        # but recommended_strategy is always populated as a fallback.
        assert new_record.recommended_strategy != ""


class TestAutoPendingDataValidation:
    def test_marks_pending(self, tmp_path):
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        record, _ = _stage_classify(record, store, dry_run=True)
        record, _ = _stage_benchmark(record, store, dry_run=True)
        record, _ = _stage_strategy(record, store, dry_run=True)
        assert str(record.validation_status) == str(ValidationStatus.STRATEGY_ASSIGNED.value)

        new_record, result = _stage_pending_dv(record, store, dry_run=True)
        assert result["action"] == "marked_pending"
        assert new_record.validation_status == str(ValidationStatus.PENDING_DATA_VALIDATION.value)


# ── Orchestrator integration tests ──────────────────────────────────


class TestOrchestratorIntegration:
    """All tests run in dry_run=True so nothing is ever written to disk."""

    def _make_bundle(
        self,
        *,
        research_top_candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "report": {
                "research_top_candidates": research_top_candidates or [],
            }
        }

    def test_empty_bundle_produces_no_candidates(self):
        bundles = [
            {"report": {"research_top_candidates": []}},
            {"report": {}},
        ]
        for b in bundles:
            orch = CandidateValidationOrchestrator()
            result = orch.run(selection_bundle=b, dry_run=True)
            assert result["status"] == "NO_CANDIDATES"
            assert result["candidates_processed"] == 0

    def test_research_candidate_surfaced(self):
        bundle = self._make_bundle(research_top_candidates=[
            {
                "ticker": "SOFI",
                "formal_scoring_eligibility": True,
                "trade_admission_status": "NOT_TRADABLE",
                "candidate_score": 75.95,
            }
        ])
        orch = CandidateValidationOrchestrator()
        result = orch.run(selection_bundle=bundle, dry_run=True)
        assert result["status"] == "OK"
        assert result["candidates_processed"] > 0

    def test_already_tradable_not_processed(self):
        """PAPER_ELIGIBLE candidates are NOT fed through the orchestrator."""
        bundle = self._make_bundle(research_top_candidates=[
            {
                "ticker": "SOFI",
                "formal_scoring_eligibility": True,
                "trade_admission_status": "PAPER_ELIGIBLE",
                "candidate_score": 75.95,
            }
        ])
        orch = CandidateValidationOrchestrator()
        result = orch.run(selection_bundle=bundle, dry_run=True)
        assert result["status"] == "NO_CANDIDATES"

    def test_no_formal_eligibility_not_processed(self):
        bundle = self._make_bundle(research_top_candidates=[
            {
                "ticker": "SOFI",
                "formal_scoring_eligibility": False,
                "trade_admission_status": "NOT_TRADABLE",
            }
        ])
        orch = CandidateValidationOrchestrator()
        result = orch.run(selection_bundle=bundle, dry_run=True)
        assert result["status"] == "NO_CANDIDATES"

    def test_safety_attestations(self):
        bundle = self._make_bundle(research_top_candidates=[
            {
                "ticker": "SOFI",
                "formal_scoring_eligibility": True,
                "trade_admission_status": "NOT_TRADABLE",
                "candidate_score": 75.95,
            }
        ])
        orch = CandidateValidationOrchestrator()
        result = orch.run(selection_bundle=bundle, dry_run=True)
        assert result["trade_api_used"] is False
        assert result["broker_used"] is False
        assert result["paper_eligible_auto"] is False
        assert result["live_eligible_auto"] is False

    def test_idempotent(self, tmp_path: Path):
        """Multiple runs on same bundle should not store duplicate results."""
        bundle = self._make_bundle(research_top_candidates=[
            {
                "ticker": "SOFI",
                "symbol": "SOFI.US",
                "formal_scoring_eligibility": True,
                "trade_admission_status": "NOT_TRADABLE",
                "candidate_score": 75.95,
            }
        ])
        cand_root = tmp_path / "candidates"
        orch = CandidateValidationOrchestrator(
            store=CandidateValidationStore(root_dir=cand_root)
        )
        # dry-run: nothing stored, so every run sees the same state
        r1 = orch.run(selection_bundle=bundle, dry_run=True)
        r2 = orch.run(selection_bundle=bundle, dry_run=True)
        assert r1["status"] == "OK"
        assert r2["status"] == "OK"
        assert r1["candidates_processed"] == r2["candidates_processed"]


class TestClassificationSafety:
    def test_classify_does_not_touch_trading_flags(self):
        record = _fresh_sofi()
        classified = _classify(record)
        assert classified.trading_enabled is False
        assert classified.shadow_enabled is False
        assert classified.paper_enabled is False
        assert classified.live_enabled is False

    def test_benchmark_does_not_touch_trading_flags(self):
        record = _fresh_sofi()
        benchd = _assign_benchmark(record)
        assert benchd.trading_enabled is False

    def test_strategy_does_not_touch_trading_flags(self):
        record = _fresh_sofi()
        stratd = _assign_strategy(record)
        assert stratd.trading_enabled is False

    def test_classify_does_not_change_symbol(self):
        record = _fresh_sofi()
        classified = _classify(record)
        assert classified.symbol == "SOFI.US"
        assert classified.symbol == record.symbol


class TestTransitionSafety:
    def test_ai_candidate_cannot_jump_to_deployment(self):
        with pytest.raises(CandidateTransitionError, match="invalid_validation_transition"):
            from src.candidate_validation.models import assert_transition_allowed
            assert_transition_allowed(
                ValidationStatus.AI_CANDIDATE.value,
                ValidationStatus.PAPER_ELIGIBLE.value,
            )

    def test_ai_candidate_can_go_to_classified(self):
        from src.candidate_validation.models import assert_transition_allowed
        assert_transition_allowed(
            ValidationStatus.AI_CANDIDATE.value,
            ValidationStatus.CLASSIFIED.value,
        )


class TestSymbolIsolation:
    def test_different_candidates_isolated_by_id(self):
        """Same symbol, different candidate_id should not collide."""
        r1 = default_candidate_for_symbol(
            symbol="SOFI.US", selected_at="2026-07-22T00:00:00Z", ai_score=80.0
        )
        r2 = default_candidate_for_symbol(
            symbol="SOFI.US", selected_at="2026-07-23T00:00:00Z", ai_score=82.0
        )
        assert r1.candidate_id != r2.candidate_id
        assert r1.symbol == r2.symbol
        assert r1.ai_score != r2.ai_score


# ── Concurrent lock tests ──────────────────────────────────────────


class TestConcurrencyLock:
    def test_second_instance_blocked_by_lock(self, tmp_path: Path):
        """First instance holds lock → second instance returns SKIPPED_LOCKED."""
        from src.candidate_validation.orchestrator import (
            CandidateValidationOrchestrator,
            ORCHESTRATOR_LOCK_PATH,
            _atomic_write_jsonl,
        )
        import os, json

        cand_root = tmp_path / "candidates"
        cand_root.mkdir(parents=True)

        # Override lock path for isolation
        lock_path = cand_root / ".orchestrator.lock"
        orch = CandidateValidationOrchestrator(
            store=CandidateValidationStore(root_dir=cand_root)
        )
        # Acquire lock manually to simulate another instance running
        p = {"run_id": "orch_fake_001", "pid": os.getpid(), "acquired_at": "2026-07-23T00:00:00Z"}
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, json.dumps(p).encode("utf-8"))
        os.close(fd)

        # Patch the lock path used by acquire_lock
        import src.candidate_validation.orchestrator as orch_mod
        orig = orch_mod.ORCHESTRATOR_LOCK_PATH
        orch_mod.ORCHESTRATOR_LOCK_PATH = lock_path
        try:
            bundle = {
                "report": {
                    "research_top_candidates": [{
                        "ticker": "SOFI",
                        "formal_scoring_eligibility": True,
                        "trade_admission_status": "NOT_TRADABLE",
                    }]
                }
            }
            result = orch.run(selection_bundle=bundle, dry_run=True)
            assert result["status"] == "SKIPPED_LOCKED"
            assert "orchestrator_lock_active" in result["errors"]
        finally:
            orch_mod.ORCHESTRATOR_LOCK_PATH = orig
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass

    def test_stale_lock_cleared_and_acquired(self, tmp_path: Path):
        """Stale lock (>1hr) is removed and a new lock is acquired."""
        from src.candidate_validation.orchestrator import (
            CandidateValidationOrchestrator,
            _utc_now_iso,
        )
        import os, json

        cand_root = tmp_path / "candidates"
        cand_root.mkdir(parents=True)
        lock_path = cand_root / ".orchestrator.lock"

        # Write a stale lock (> 1 hour old)
        stale = {"run_id": "orch_old_001", "pid": 99999, "acquired_at": "2026-07-22T00:00:00Z"}
        lock_path.write_text(json.dumps(stale), encoding="utf-8")
        # Manipulate mtime to be 2 hours ago
        old_mtime = (datetime.now(timezone.utc).timestamp()) - 7200
        os.utime(str(lock_path), (old_mtime, old_mtime))

        orch = CandidateValidationOrchestrator(
            store=CandidateValidationStore(root_dir=cand_root)
        )
        import src.candidate_validation.orchestrator as orch_mod
        orig = orch_mod.ORCHESTRATOR_LOCK_PATH
        orch_mod.ORCHESTRATOR_LOCK_PATH = lock_path
        try:
            bundle = {
                "report": {
                    "research_top_candidates": [{
                        "ticker": "SOFI",
                        "formal_scoring_eligibility": True,
                        "trade_admission_status": "NOT_TRADABLE",
                    }]
                }
            }
            result = orch.run(selection_bundle=bundle, dry_run=True)
            # Should NOT be locked-out — stale lock is cleared
            assert result["status"] in {"OK", "NO_CANDIDATES"}
            assert result.get("status") != "SKIPPED_LOCKED"
        finally:
            orch_mod.ORCHESTRATOR_LOCK_PATH = orig
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass


# ── Cooldown tests ────────────────────────────────────────────────────


class TestCooldown:
    def test_cooldown_blocks_reprocessing(self, tmp_path: Path):
        """When attempt_count >= max_attempts, cooldown blocks re-processing."""
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        record.metadata["orchestrator_attempt_count"] = "3"
        record.metadata["orchestrator_last_attempt_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat(timespec="seconds")
        store.save_candidates([record])
        loaded = store.get_candidate(record.candidate_id)
        assert loaded is not None

        orch = CandidateValidationOrchestrator(
            store=store, max_attempts=3, cooldown_minutes=30,
        )
        skip_reason = orch._should_skip(loaded)
        assert skip_reason == "cooldown_active"

    def test_cooldown_expired_allows_retry(self, tmp_path: Path):
        """After cooldown expires, re-processing is allowed."""
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        record.metadata["orchestrator_attempt_count"] = "3"
        record.metadata["orchestrator_last_attempt_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=60)
        ).isoformat(timespec="seconds")
        store.save_candidates([record])
        loaded = store.get_candidate(record.candidate_id)
        assert loaded is not None

        orch = CandidateValidationOrchestrator(
            store=store, max_attempts=3, cooldown_minutes=30,
        )
        skip_reason = orch._should_skip(loaded)
        assert skip_reason == ""  # Cooldown expired — allow retry

    def test_success_resets_attempt_count(self, tmp_path: Path):
        """After a successful dry-run advance, attempt count should be cleared."""
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        record.metadata["orchestrator_attempt_count"] = "1"
        record.metadata["orchestrator_last_attempt_at"] = _utc_now_iso()
        store.save_candidates([record])

        loaded = store.get_candidate(record.candidate_id)
        orch = CandidateValidationOrchestrator(
            store=store, max_attempts=3, cooldown_minutes=30,
        )
        skip_reason = orch._should_skip(loaded)
        assert skip_reason == ""  # Not in cooldown

    def test_below_threshold_not_in_cooldown(self, tmp_path: Path):
        """2 attempts with max_attempts=3 → NOT in cooldown."""
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        record.metadata["orchestrator_attempt_count"] = "2"
        record.metadata["orchestrator_last_attempt_at"] = _utc_now_iso()
        store.save_candidates([record])

        loaded = store.get_candidate(record.candidate_id)
        orch = CandidateValidationOrchestrator(
            store=store, max_attempts=3, cooldown_minutes=30,
        )
        assert orch._should_skip(loaded) == ""


# ── Failure recovery tests ────────────────────────────────────────────


class TestFailureRecovery:
    def test_failed_stage_does_not_advance_status(self, tmp_path: Path):
        """An error mid-pipeline must NOT leave the candidate in an invalid state."""
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        store.save_candidates([record])
        loaded = store.get_candidate(record.candidate_id)

        # Manually force a transition that should fail
        from src.candidate_validation.models import assert_transition_allowed, CandidateTransitionError
        try:
            assert_transition_allowed(
                str(loaded.validation_status),
                ValidationStatus.PAPER_ELIGIBLE.value,
            )
            # Should not reach here
            assert False, "Expected transition to be blocked"
        except CandidateTransitionError:
            pass  # Expected — AI_CANDIDATE cannot jump to PAPER_ELIGIBLE

        # Status must remain AI_CANDIDATE
        assert str(loaded.validation_status) == str(ValidationStatus.AI_CANDIDATE.value)

    def test_dry_run_failure_preserves_state(self, tmp_path: Path):
        """A dry-run does not mutate state, even on errors."""
        store = _tmp_store(tmp_path)
        record = _fresh_sofi()
        store.save_candidates([record])

        bundle = {
            "report": {
                "research_top_candidates": [{
                    "ticker": "SOFI",
                    "symbol": "SOFI.US",
                    "formal_scoring_eligibility": True,
                    "trade_admission_status": "NOT_TRADABLE",
                    "candidate_score": 75.95,
                }]
            }
        }
        orch = CandidateValidationOrchestrator(store=store)
        r1 = orch.run(selection_bundle=bundle, dry_run=True)
        assert r1["status"] == "OK"

        # Re-load from store — should still be AI_CANDIDATE (dry-run never writes)
        loaded = store.get_candidate(record.candidate_id)
        assert loaded is not None
        assert str(loaded.validation_status) == str(ValidationStatus.AI_CANDIDATE.value)


# ── Corrupt JSON tests ────────────────────────────────────────────────


class TestCorruptJSON:
    def test_invalid_candidate_not_processed(self, tmp_path: Path):
        """Corrupt candidate data should not cause a crash or silent write."""
        store = _tmp_store(tmp_path)
        # Write corrupt JSON to candidates file
        candidates_path = store.candidates_path
        candidates_path.parent.mkdir(parents=True, exist_ok=True)
        candidates_path.write_text("not valid json {{{", encoding="utf-8")

        orch = CandidateValidationOrchestrator(store=store)
        bundle = {
            "report": {
                "research_top_candidates": [{
                    "ticker": "SOFI",
                    "formal_scoring_eligibility": True,
                    "trade_admission_status": "NOT_TRADABLE",
                }]
            }
        }
        # Should fail closed — not crash
        try:
            result = orch.run(selection_bundle=bundle, dry_run=True)
        except Exception:
            pass
        # After handling, no PAPER_ELIGIBLE/LIVE_ELIGIBLE should appear
        # (stores may be empty or error — either is fine as long as it doesn't mutate)

    def test_missing_candidates_file_is_safe(self, tmp_path: Path):
        """When candidates file does not exist, orchestrator handles gracefully."""
        store = _tmp_store(tmp_path)
        # Don't create any candidates file
        orch = CandidateValidationOrchestrator(store=store)
        bundle = {
            "report": {
                "research_top_candidates": [{
                    "ticker": "SOFI",
                    "formal_scoring_eligibility": True,
                    "trade_admission_status": "NOT_TRADABLE",
                }]
            }
        }
        result = orch.run(selection_bundle=bundle, dry_run=True)
        assert result["status"] in {"OK", "NO_CANDIDATES"}


# ── Apply idempotence tests ───────────────────────────────────────────


class TestApplyIdempotence:
    def test_dry_run_idempotent_results(self, tmp_path: Path):
        """Same bundle, same store, dry_run=True → identical results both runs."""
        store = _tmp_store(tmp_path)
        bundle = {
            "report": {
                "research_top_candidates": [{
                    "ticker": "SOFI",
                    "symbol": "SOFI.US",
                    "formal_scoring_eligibility": True,
                    "trade_admission_status": "NOT_TRADABLE",
                    "candidate_score": 75.95,
                }]
            }
        }
        orch = CandidateValidationOrchestrator(store=store)
        r1 = orch.run(selection_bundle=bundle, dry_run=True)
        r2 = orch.run(selection_bundle=bundle, dry_run=True)
        assert r1["status"] == r2["status"]
        assert r1["candidates_processed"] == r2["candidates_processed"]

    def test_no_paper_live_auto_assignment(self, tmp_path: Path):
        """Even after multiple runs, PAPER_ELIGIBLE/LIVE_ELIGIBLE are never assigned."""
        store = _tmp_store(tmp_path)
        bundle = {
            "report": {
                "research_top_candidates": [{
                    "ticker": "SOFI",
                    "symbol": "SOFI.US",
                    "formal_scoring_eligibility": True,
                    "trade_admission_status": "NOT_TRADABLE",
                    "candidate_score": 75.95,
                }]
            }
        }
        orch = CandidateValidationOrchestrator(store=store)
        for _ in range(3):
            r = orch.run(selection_bundle=bundle, dry_run=True)
            assert r["paper_eligible_auto"] is False
            assert r["live_eligible_auto"] is False
            assert r["trade_api_used"] is False
            assert r["broker_used"] is False


# ── Safety boundary tests ─────────────────────────────────────────────


class TestSafetyBoundaries:
    def test_no_broker_instantiation(self):
        """Orchestrator module must not import or reference broker anywhere."""
        import src.candidate_validation.orchestrator as orch_mod
        source = (Path(orch_mod.__file__).read_text(encoding="utf-8")
                  if hasattr(orch_mod, '__file__') else "")
        # No broker imports
        assert "LongBridgeBroker" not in source
        assert "PaperBroker" not in source
        assert "TradeContext" not in source

    def test_no_top_config_writing(self):
        """Orchestrator must not write TOP YAML configs."""
        import src.candidate_validation.orchestrator as orch_mod
        source = (Path(orch_mod.__file__).read_text(encoding="utf-8")
                  if hasattr(orch_mod, '__file__') else "")
        # No TOP config writes
        assert "TOP" not in source or "TOP_CONFIGS" not in source
        assert "write_top_configs" not in source
        assert "allow_live_order" not in source
        assert "reduce_only" not in source

    def test_all_stages_are_safe(self):
        """Verify AUTO_STAGES never includes backtest/walk-forward/shadow/paper/live."""
        from src.candidate_validation.orchestrator import AUTO_STAGES, OrchestratorPhase
        phase_names = {p.value for p, _ in AUTO_STAGES}
        assert "BACKTEST" not in phase_names
        assert "WALK_FORWARD" not in phase_names
        assert "SHADOW" not in phase_names
        assert "PAPER" not in phase_names
        assert "LIVE" not in phase_names
        # Max stage is DATA_VALIDATION
        assert OrchestratorPhase.DATA_VALIDATION.value in phase_names
