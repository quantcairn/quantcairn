from __future__ import annotations

import importlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

status = importlib.import_module("scripts.status")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _configure_runtime(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(status, "PROJECT_DIR", root)
    monkeypatch.setattr(status, "STATE_DIR", root / "state")
    monkeypatch.setattr(status, "ARTIFACTS_SEL", root / "artifacts" / "selection")


def _render_status() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


def test_status_prefers_committed_bundle_over_diagnostic_preflight(tmp_path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    committed_root = tmp_path / "state" / "selection_bundles" / "run-bundle" / "v1"
    _write_json(
        tmp_path / "state" / "selection_bundle_manifest.json",
        {
            "bundle_root": "state/selection_bundles/run-bundle/v1",
            "bundle_version": "v1",
            "selection_bundle_hash": "hash-bundle",
            "selection_run_id": "run-bundle",
        },
    )
    _write_json(
        committed_root / "ai_selection_report.json",
        {
            "selection_run_id": "run-bundle",
            "selection_outcome": "NO_TRADABLE_SELECTION",
            "selected_top_n": 0,
            "final_selected_symbols": [],
            "market_state": "MARKET_OPEN",
            "run_mode": "FULL",
            "data_mode": "LIVE",
            "generated_at": "2026-07-29T23:08:24.586018",
        },
    )
    _write_json(
        tmp_path / "reports" / "ai_selection_latest.json",
        {
            "selection_run_id": "run-latest",
            "selection_outcome": "SUCCESS",
            "selected_top_n": 3,
            "final_selected_symbols": ["ZZZ"],
            "market_state": "AFTER_HOURS",
            "run_mode": "DEGRADED",
            "data_mode": "EOD_ONLY",
            "generated_at": "2026-07-29T20:00:00Z",
        },
    )
    _write_json(
        tmp_path / "artifacts" / "selection" / "preflight.json",
        {
            "selection_run_id": "diag-run",
            "diagnostic_preflight": True,
            "generated_at": "2026-07-29T15:32:33+00:00",
            "market_state": "MARKET_OPEN",
            "run_mode": "FULL",
            "data_mode": "LIVE",
        },
    )

    output = _render_status()
    committed_block = output.split("Diagnostic Preflight Snapshot")[0]

    assert "Committed Selection Run" in output
    assert "Source:        committed bundle" in output
    assert "selection_run_id: run-bundle" in committed_block
    assert "selection_outcome: NO_TRADABLE_SELECTION" in committed_block
    assert "selected_top_n: 0" in committed_block
    assert "final_selected_symbols: []" in committed_block
    assert "market_state: MARKET_OPEN" in committed_block
    assert "run_mode: FULL" in committed_block
    assert "data_mode: LIVE" in committed_block
    assert "generated_at: 2026-07-29T23:08:24.586018" in committed_block
    assert "diag-run" not in committed_block
    assert "Diagnostic Preflight Snapshot" in output
    assert "diagnostic selection_run_id: diag-run" in output
    assert "This snapshot is diagnostic only and is not the committed selection state." in output
    assert "Diagnostic snapshot is not bound to the current committed selection run." in output


def test_status_uses_latest_report_fallback_when_committed_bundle_missing(tmp_path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    _write_json(
        tmp_path / "reports" / "ai_selection_latest.json",
        {
            "selection_run_id": "run-latest",
            "selection_outcome": "SUCCESS",
            "selected_top_n": 2,
            "final_selected_symbols": ["AAA", "BBB"],
            "market_state": "AFTER_HOURS",
            "run_mode": "DEGRADED",
            "data_mode": "EOD_ONLY",
            "generated_at": "2026-07-29T20:00:00Z",
        },
    )

    output = _render_status()

    assert "Source:        latest report fallback" in output
    assert "selection_run_id: run-latest" in output
    assert "selection_outcome: SUCCESS" in output
    assert "selected_top_n: 2" in output
    assert "final_selected_symbols: ['AAA', 'BBB']" in output
    assert "market_state: AFTER_HOURS" in output
    assert "run_mode: DEGRADED" in output
    assert "data_mode: EOD_ONLY" in output


def test_status_does_not_promote_diagnostic_preflight_when_formal_state_missing(tmp_path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    _write_json(
        tmp_path / "artifacts" / "selection" / "preflight.json",
        {
            "selection_run_id": "diag-only",
            "diagnostic_preflight": False,
            "artifact_role": "diagnostic_preflight",
            "authoritative": False,
            "generated_at": "2026-07-29T15:32:33+00:00",
            "market_state": "MARKET_OPEN",
            "run_mode": "FULL",
            "data_mode": "LIVE",
        },
    )

    output = _render_status()

    assert "Source:        none" in output
    assert "Selection run: no committed selection run" in output
    assert "diagnostic selection_run_id: diag-only" in output
    assert "Diagnostic snapshot is not bound to the current committed selection run." not in output


def test_status_handles_malformed_or_missing_diagnostic_preflight(tmp_path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    _write_json(
        tmp_path / "reports" / "ai_selection_latest.json",
        {
            "selection_run_id": "run-latest",
            "selection_outcome": "SUCCESS",
            "selected_top_n": 1,
            "final_selected_symbols": ["AAA"],
            "market_state": "MARKET_OPEN",
            "run_mode": "FULL",
            "data_mode": "LIVE",
            "generated_at": "2026-07-29T23:08:24.586018",
        },
    )
    malformed = tmp_path / "artifacts" / "selection" / "preflight.json"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("{not-json", encoding="utf-8")

    output = _render_status()

    assert "Source:        latest report fallback" in output
    assert "diagnostic selection_run_id: UNKNOWN" in output
    assert "Diagnostic snapshot: unavailable" in output


def test_status_handles_missing_committed_bundle_fields_safely(tmp_path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    committed_root = tmp_path / "state" / "selection_bundles" / "run-minimal" / "v1"
    _write_json(
        tmp_path / "state" / "selection_bundle_manifest.json",
        {
            "bundle_root": "state/selection_bundles/run-minimal/v1",
            "bundle_version": "v1",
            "selection_run_id": "run-minimal",
        },
    )
    _write_json(
        committed_root / "ai_selection_report.json",
        {
            "selection_run_id": "run-minimal",
        },
    )

    output = _render_status()

    assert "Source:        committed bundle" in output
    assert "selection_run_id: run-minimal" in output
    assert "selection_outcome: UNKNOWN" in output
    assert "selected_top_n: 0" in output
    assert "final_selected_symbols: []" in output
    assert "market_state: UNKNOWN" in output
    assert "run_mode: UNKNOWN" in output
    assert "data_mode: UNKNOWN" in output
