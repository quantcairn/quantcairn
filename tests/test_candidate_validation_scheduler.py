from __future__ import annotations

import json
from pathlib import Path

from src.candidate_validation.models import ValidationStatus
from src.candidate_validation.orchestrator import CandidateValidationOrchestrator
from src.candidate_validation.store import CandidateValidationStore


def _bundle() -> dict[str, object]:
    return {
        "report": {
            "research_top_candidates": [
                {
                    "ticker": "SOFI",
                    "symbol": "SOFI.US",
                    "formal_scoring_eligibility": True,
                    "trade_admission_status": "NOT_TRADABLE",
                    "candidate_score": 75.95,
                    "selection_run_id": "run-001",
                }
            ]
        }
    }


def _run_scheduler(monkeypatch, tmp_path: Path, *, apply: bool):
    import scripts.run_candidate_validation_scheduler as scheduler
    import src.candidate_validation.orchestrator as orch_mod

    store_root = tmp_path / "artifacts" / "candidates"
    store = CandidateValidationStore(root_dir=store_root)
    orchestrator = CandidateValidationOrchestrator(store=store, project_dir=tmp_path)

    monkeypatch.setattr(scheduler, "PROJECT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        orch_mod,
        "CANDIDATE_ROOT",
        store_root,
        raising=False,
    )
    monkeypatch.setattr(
        orch_mod,
        "VALIDATION_ROOT",
        store_root / "validation",
        raising=False,
    )
    monkeypatch.setattr(
        orch_mod,
        "ORCHESTRATOR_LOCK_PATH",
        store_root / ".orchestrator.lock",
        raising=False,
    )
    monkeypatch.setattr(
        orch_mod,
        "ORCHESTRATOR_AUDIT_PATH",
        store_root / "orchestrator_run_audit.jsonl",
        raising=False,
    )
    monkeypatch.setattr(
        orch_mod,
        "load_committed_selection_bundle",
        lambda project_dir: _bundle(),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "CandidateValidationOrchestrator",
        lambda: orchestrator,
        raising=False,
    )

    argv = ["run_candidate_validation_scheduler.py", "--json"]
    if apply:
        argv.insert(1, "--apply")
    monkeypatch.setattr(scheduler.sys, "argv", argv, raising=False)

    rc = scheduler.main()
    return rc, scheduler, store


def test_scheduler_dry_run_does_not_write_transition(tmp_path: Path, monkeypatch, capsys):
    rc, scheduler, store = _run_scheduler(monkeypatch, tmp_path, apply=False)
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert payload["paper_eligible_auto"] is False
    assert payload["live_eligible_auto"] is False

    assert not store.history_path.exists() or "validation_transition" not in store.history_path.read_text(encoding="utf-8")

    audit_path = scheduler.PROJECT_DIR / "artifacts" / "candidates" / "validation_scheduler_runs.jsonl"
    assert audit_path.exists()
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_rows[-1]["mode"] == "dry_run"
    assert audit_rows[-1]["candidates_scanned"] >= 1
    assert audit_rows[-1]["candidates_advanced"] >= 0


def test_scheduler_apply_writes_validation_transition_without_paper_live(tmp_path: Path, monkeypatch, capsys):
    rc, scheduler, store = _run_scheduler(monkeypatch, tmp_path, apply=True)
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert payload["applied"] is True
    assert payload["paper_eligible_auto"] is False
    assert payload["live_eligible_auto"] is False

    history = store.history_path.read_text(encoding="utf-8")
    assert "validation_transition" in history
    assert "PAPER_ELIGIBLE" not in history
    assert "LIVE_ELIGIBLE" not in history

    latest = store.load_latest_candidates()
    assert latest
    assert all(item.validation_status == ValidationStatus.DATA_INVALID.value for item in latest)

    audit_path = scheduler.PROJECT_DIR / "artifacts" / "candidates" / "validation_scheduler_runs.jsonl"
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_rows[-1]["mode"] == "apply"
    assert audit_rows[-1]["candidates_scanned"] >= 1
    assert audit_rows[-1]["candidates_advanced"] >= 1
    assert audit_rows[-1]["transition_events"]
    assert all(
        event["final_status"] == ValidationStatus.DATA_INVALID.value
        for event in audit_rows[-1]["transition_events"]
    )


def _write_committed_bundle(state_root: Path, *, run_id: str, selection_date: str, bundle_hash: str | None):
    bundle_root = state_root / "selection_bundles" / run_id / "selection_bundle_v1"
    bundle_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_version": "selection_bundle_v1",
        "selection_run_id": run_id,
        "selection_date": selection_date,
        "selection_bundle_hash": bundle_hash,
        "bundle_root": str(bundle_root),
        "paths": {"manifest": str(state_root / "selection_bundle_manifest.json")},
    }
    report = {
        "selection_run_id": run_id,
        "selection_date": selection_date,
        "research_top_candidates": [],
        "top3": [],
        "top5": [],
        "top10": [],
    }
    (state_root / "selection_bundle_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (bundle_root / "ai_selection_report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return bundle_root


def _run_scheduler_against_external_bundle(tmp_path: Path, monkeypatch, *, bundle_hash: str | None):
    import scripts.run_candidate_validation_scheduler as scheduler
    import src.candidate_validation.orchestrator as orch_mod

    state_root = tmp_path / "external-state"
    artifacts_root = tmp_path / "external-artifacts"
    _write_committed_bundle(
        state_root,
        run_id="selection-current",
        selection_date="2026-08-11",
        bundle_hash=bundle_hash,
    )
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_root))
    monkeypatch.setenv("SOXS_ARTIFACTS_DIR", str(artifacts_root))
    store_root = artifacts_root / "candidates"
    store = CandidateValidationStore(root_dir=store_root)
    orchestrator = CandidateValidationOrchestrator(store=store, project_dir=tmp_path)

    monkeypatch.setattr(scheduler, "PROJECT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(orch_mod, "CANDIDATE_ROOT", store_root, raising=False)
    monkeypatch.setattr(orch_mod, "VALIDATION_ROOT", store_root / "validation", raising=False)
    monkeypatch.setattr(orch_mod, "ORCHESTRATOR_LOCK_PATH", store_root / ".orchestrator.lock", raising=False)
    monkeypatch.setattr(orch_mod, "ORCHESTRATOR_AUDIT_PATH", store_root / "orchestrator_run_audit.jsonl", raising=False)
    monkeypatch.setattr(scheduler, "CandidateValidationOrchestrator", lambda: orchestrator, raising=False)
    monkeypatch.setattr(scheduler.sys, "argv", ["run_candidate_validation_scheduler.py", "--json"], raising=False)
    return scheduler.main(), scheduler, artifacts_root


def test_scheduler_audit_records_committed_bundle_identity_from_external_state(tmp_path, monkeypatch, capsys):
    rc, scheduler, artifacts_root = _run_scheduler_against_external_bundle(
        tmp_path, monkeypatch, bundle_hash="canonical-hash"
    )
    assert rc == 0
    json.loads(capsys.readouterr().out)
    audit_path = artifacts_root / "candidates" / "validation_scheduler_runs.jsonl"
    row = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["validation_run_id"] == row["run_id"]
    assert row["selection_run_id"] == "selection-current"
    assert row["selection_date"] == "2026-08-11"
    assert row["bundle_hash"] == "canonical-hash"
    assert row["bundle_source"] == "committed_bundle"
    assert row["candidate_input_count"] == 0
    assert not (tmp_path / "state").exists()


def test_scheduler_legacy_bundle_identity_is_explicit(tmp_path, monkeypatch, capsys):
    rc, scheduler, artifacts_root = _run_scheduler_against_external_bundle(
        tmp_path, monkeypatch, bundle_hash=None
    )
    assert rc == 0
    json.loads(capsys.readouterr().out)
    row = json.loads(
        (artifacts_root / "candidates" / "validation_scheduler_runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert row["selection_run_id"] == "selection-current"
    assert row["selection_date"] == "2026-08-11"
    assert row["bundle_hash"] is None
    assert row["bundle_source"] == "legacy_committed_bundle"


def test_scheduler_missing_bundle_identity_is_explicit(tmp_path, monkeypatch, capsys):
    import scripts.run_candidate_validation_scheduler as scheduler
    import src.candidate_validation.orchestrator as orch_mod

    store_root = tmp_path / "artifacts" / "candidates"
    orchestrator = CandidateValidationOrchestrator(
        store=CandidateValidationStore(root_dir=store_root), project_dir=tmp_path
    )
    monkeypatch.setattr(scheduler, "PROJECT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(orch_mod, "CANDIDATE_ROOT", store_root, raising=False)
    monkeypatch.setattr(orch_mod, "ORCHESTRATOR_LOCK_PATH", store_root / ".orchestrator.lock", raising=False)
    monkeypatch.setattr(orch_mod, "ORCHESTRATOR_AUDIT_PATH", store_root / "orchestrator_run_audit.jsonl", raising=False)
    monkeypatch.setattr(orch_mod, "load_committed_selection_bundle", lambda _project: None, raising=False)
    monkeypatch.setattr(scheduler, "CandidateValidationOrchestrator", lambda: orchestrator, raising=False)
    monkeypatch.setattr(scheduler.sys, "argv", ["run_candidate_validation_scheduler.py", "--json"], raising=False)

    assert scheduler.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NO_CANDIDATES"
    assert payload["selection_run_id"] is None
    assert payload["bundle_hash"] is None
    assert payload["bundle_source"] == "missing_bundle"
