from __future__ import annotations

import json

from src.openalpha.top_restart import load_restart_status, record_restart_status


def test_restart_status_is_separate_from_selection_identity(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state))
    path = record_restart_status(
        status="PENDING",
        selection_run_id="selection-1",
        selection_bundle_hash="bundle-1",
        project_dir=tmp_path,
    )
    assert path == state / "top_restart_status.json"
    assert load_restart_status(tmp_path)["status"] == "PENDING"
    assert json.loads(path.read_text(encoding="utf-8"))["selection_bundle_hash"] == "bundle-1"


def test_restart_status_records_failure_without_rewriting_bundle(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state))
    bundle = state / "selection_bundle_manifest.json"
    state.mkdir(parents=True)
    bundle.write_text('{"selection_bundle_hash":"bundle-1"}', encoding="utf-8")
    record_restart_status(
        status="FAILED",
        selection_run_id="selection-1",
        selection_bundle_hash="bundle-1",
        error="restart_exit_6",
        project_dir=tmp_path,
    )
    assert bundle.read_text(encoding="utf-8") == '{"selection_bundle_hash":"bundle-1"}'
    assert load_restart_status(tmp_path)["error"] == "restart_exit_6"
