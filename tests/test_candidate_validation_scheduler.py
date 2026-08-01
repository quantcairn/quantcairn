from __future__ import annotations

import json
from pathlib import Path

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

    audit_path = scheduler.PROJECT_DIR / "artifacts" / "candidates" / "validation_scheduler_runs.jsonl"
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert audit_rows[-1]["mode"] == "apply"
    assert audit_rows[-1]["candidates_scanned"] >= 1
    assert audit_rows[-1]["candidates_advanced"] >= 1
    assert audit_rows[-1]["transition_events"]
