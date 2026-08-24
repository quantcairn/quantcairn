from __future__ import annotations

import importlib
import os
from pathlib import Path


def _roots(tmp_path: Path, name: str) -> dict[str, Path]:
    root = tmp_path / name
    return {
        "project": root / "code",
        "state": root / "state",
        "reports": root / "reports",
        "artifacts": root / "artifacts",
        "logs": root / "logs",
    }


def _set_roots(monkeypatch, roots: dict[str, Path]) -> None:
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(roots["project"]))
    monkeypatch.setenv("SOXS_STATE_DIR", str(roots["state"]))
    monkeypatch.setenv("SOXS_REPORTS_DIR", str(roots["reports"]))
    monkeypatch.setenv("SOXS_ARTIFACTS_DIR", str(roots["artifacts"]))
    monkeypatch.setenv("SOXS_LOG_DIR", str(roots["logs"]))
    monkeypatch.setenv("SOXS_LOGS_DIR", str(roots["logs"]))


def test_runtime_resolver_supports_env_before_import_and_a_to_b_switch(tmp_path, monkeypatch):
    roots_a = _roots(tmp_path, "a")
    roots_b = _roots(tmp_path, "b")
    _set_roots(monkeypatch, roots_a)
    module = importlib.import_module("src.config.runtime_paths")
    assert module.resolve_state_dir() == roots_a["state"].resolve()
    _set_roots(monkeypatch, roots_b)
    assert module.resolve_state_dir() == roots_b["state"].resolve()
    assert module.resolve_logs_dir() == roots_b["logs"].resolve()


def test_wrapper_log_and_state_paths_are_operation_time_resolved(tmp_path, monkeypatch):
    module = importlib.import_module("scripts.ai_selector_wrapper")
    roots_a = _roots(tmp_path, "a")
    roots_b = _roots(tmp_path, "b")
    _set_roots(monkeypatch, roots_a)
    assert module._runtime_log_paths()[0].parent == roots_a["logs"].resolve()
    assert module._runtime_state_dir() == roots_a["state"].resolve()
    _set_roots(monkeypatch, roots_b)
    assert module._runtime_log_paths()[0].parent == roots_b["logs"].resolve()
    assert module._runtime_state_dir() == roots_b["state"].resolve()


def test_preflight_universe_bundle_and_refinement_paths_switch_at_operation_time(tmp_path, monkeypatch):
    preflight = importlib.import_module("src.openalpha.preflight")
    manager_module = importlib.import_module("src.universe.manager")
    bundle_module = importlib.import_module("src.openalpha.selection_bundle")
    refinement = importlib.import_module("scripts.refine_ai_selection_report")
    roots_a = _roots(tmp_path, "a")
    roots_b = _roots(tmp_path, "b")
    _set_roots(monkeypatch, roots_a)
    bundle = bundle_module.SelectionBundle(
        summary={}, selection_state_payload={}, top_items=[],
        selection_run_id="run", selection_date="2026-01-01", generated_at="now",
        processing_phase="", selection_stage="", result_quality="", research_admission="",
    )
    manager = manager_module.UniverseManager()
    assert preflight._artifact_dir() == (roots_a["artifacts"] / "selection").resolve()
    assert manager.snapshot_path == (roots_a["artifacts"] / "universe" / "universe_snapshot.json").resolve()
    assert bundle.report_latest_path == (roots_a["reports"] / "ai_selection_latest.json").resolve()
    assert refinement._latest_report_path() == (roots_a["reports"] / "ai_selection_latest.json").resolve()
    _set_roots(monkeypatch, roots_b)
    assert preflight._artifact_dir() == (roots_b["artifacts"] / "selection").resolve()
    assert manager.snapshot_path == (roots_b["artifacts"] / "universe" / "universe_snapshot.json").resolve()
    assert manager_module.UniverseManager().snapshot_path == (roots_b["artifacts"] / "universe" / "universe_snapshot.json").resolve()
    assert bundle.report_latest_path == (roots_b["reports"] / "ai_selection_latest.json").resolve()
    assert refinement._current_manifest_path() == (roots_b["state"] / "selection_bundle_manifest.json").resolve()


def test_runtime_paths_are_cwd_independent_and_unset_defaults_are_external(tmp_path, monkeypatch):
    original_cwd = Path.cwd()
    monkeypatch.chdir(tmp_path)
    for key in ("SOXS_PROJECT_DIR", "SOXS_STATE_DIR", "SOXS_REPORTS_DIR", "SOXS_ARTIFACTS_DIR", "SOXS_LOG_DIR", "SOXS_LOGS_DIR"):
        monkeypatch.delenv(key, raising=False)
    module = importlib.import_module("src.config.runtime_paths")
    assert module.resolve_project_dir() == module.CODE_ROOT.resolve()
    assert module.resolve_logs_dir() == (Path.home() / ".quantcairn" / "runtime" / "logs").resolve()
    monkeypatch.chdir(original_cwd)


def test_selector_chain_log_and_report_consumers_switch_roots_without_reimport(tmp_path, monkeypatch):
    selector = importlib.import_module("src.openalpha.selector")
    runner = importlib.import_module("scripts.run_ai_selector")
    roots_a = _roots(tmp_path, "a")
    roots_b = _roots(tmp_path, "b")
    _set_roots(monkeypatch, roots_a)
    assert selector._runtime_log_dir() == roots_a["logs"].resolve()
    assert runner._runtime_reports_dir() == roots_a["reports"].resolve()
    runner._write_reports({"selection_run_id": "a"})
    _set_roots(monkeypatch, roots_b)
    assert selector._runtime_log_dir() == roots_b["logs"].resolve()
    assert runner._runtime_reports_dir() == roots_b["reports"].resolve()
    runner._write_reports({"selection_run_id": "b"})
    assert (roots_a["reports"] / "ai_selection_latest.json").exists()
    assert (roots_b["reports"] / "ai_selection_latest.json").exists()
