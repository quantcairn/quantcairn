from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "release_smoke_test.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_release_smoke_test_module", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _fake_workspace(workspace_root: Path):
    yield workspace_root


def test_release_smoke_runs_phases_in_order(monkeypatch, tmp_path):
    module = _load_module()
    calls: list[str] = []
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    monkeypatch.setattr(module, "_smoke_workspace", lambda _repo_root: _fake_workspace(workspace_root))
    monkeypatch.setattr(
        module,
        "_run_selector_smoke",
        lambda *args, **kwargs: calls.append("selector")
        or {
            "report": {"selection_run_id": "run-1", "selected_symbols": ["NVDA"], "final_selected_symbols": ["NVDA"], "selected_top_n": 1},
            "top_configs": [{"slot": 1}],
            "selection_run_id": "run-1",
        },
    )
    monkeypatch.setattr(module, "_write_candidate_model_evaluation_snapshot", lambda *args, **kwargs: calls.append("candidate_model") or {"ok": True})
    monkeypatch.setattr(module, "_write_candidate_research_snapshot", lambda *args, **kwargs: calls.append("candidate_research") or {"ok": True})
    monkeypatch.setattr(module, "_write_shadow_snapshot", lambda *args, **kwargs: calls.append("shadow") or {"ok": True})
    monkeypatch.setattr(module, "_verify_telegram_mock", lambda *args, **kwargs: calls.append("telegram") or {"final_status": "SENT"})
    monkeypatch.setattr(module, "_verify_paper_broker", lambda *args, **kwargs: calls.append("paper") or {"positions": 0})
    monkeypatch.setattr(module, "_verify_system_health", lambda *args, **kwargs: calls.append("system_health") or {"sections": ["scheduler"]})
    monkeypatch.setattr(module, "_verify_dashboard_api_status", lambda *args, **kwargs: calls.append("dashboard") or {"status_code": 200})
    monkeypatch.setattr(module, "_scan_tracebacks", lambda *args, **kwargs: calls.append("tracebacks") or [])

    summary = module.run_release_smoke(repo_root=tmp_path)

    assert calls == [
        "selector",
        "candidate_model",
        "candidate_research",
        "shadow",
        "telegram",
        "paper",
        "system_health",
        "dashboard",
        "tracebacks",
    ]
    assert summary["selector"]["selection_run_id"] == "run-1"
    assert summary["telegram"]["final_status"] == "SENT"


def test_release_smoke_fails_fast(monkeypatch, tmp_path):
    module = _load_module()
    calls: list[str] = []
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    monkeypatch.setattr(module, "_smoke_workspace", lambda _repo_root: _fake_workspace(workspace_root))

    def _boom(*args, **kwargs):
        calls.append("selector")
        raise module.ReleaseSmokeError("selector failed")

    monkeypatch.setattr(module, "_run_selector_smoke", _boom)
    monkeypatch.setattr(module, "_write_candidate_model_evaluation_snapshot", lambda *args, **kwargs: calls.append("candidate_model") or {"ok": True})

    try:
        module.run_release_smoke(repo_root=tmp_path)
    except module.ReleaseSmokeError as exc:
        assert "selector failed" in str(exc)
    else:
        raise AssertionError("release smoke should fail fast when selector fails")

    assert calls == ["selector"]


def test_verify_telegram_mock_uses_workspace_subprocess(monkeypatch, tmp_path):
    module = _load_module()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    captured: dict[str, object] = {}

    def fake_run_python_json(received_workspace_root, code, *, timeout_seconds=240):
        captured["workspace_root"] = received_workspace_root
        captured["code"] = code
        ledger_path = received_workspace_root / "state" / "notifications" / "notification_ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(
            json.dumps(
                {
                    "status": "SENT",
                    "final_status": "SENT",
                    "public_channel_ok": True,
                    "admin_channel_ok": True,
                    "telegram_attempted": True,
                    "ledger_entries": 1,
                    "ledger_path": "state/notifications/notification_ledger.jsonl",
                    "elapsed_seconds": 0.123,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {
            "status": "SENT",
            "final_status": "SENT",
            "public_channel_ok": True,
            "admin_channel_ok": True,
            "telegram_attempted": True,
            "ledger_entries": 1,
            "ledger_path": "state/notifications/notification_ledger.jsonl",
            "elapsed_seconds": 0.123,
        }

    monkeypatch.setattr(module, "_run_python_json", fake_run_python_json)

    result = module._verify_telegram_mock(
        workspace_root,
        selection_report={"selection_run_id": "run-1", "selection_bundle_hash": "bundle-1", "selection_stage": "FINALIZED"},
        top_configs=[{"ticker": "SOXS"}],
    )

    assert captured["workspace_root"] == workspace_root
    assert "from src.notifier import alerts" in str(captured["code"])
    assert "notify_ai_selection_result" in str(captured["code"])
    assert "Path.cwd()" in str(captured["code"])
    assert result["status"] == "SENT"
    assert result["final_status"] == "SENT"
    assert result["ledger_entries"] == 1
    assert result["telegram_attempted"] is True


def test_verify_telegram_mock_fails_when_ledger_missing(monkeypatch, tmp_path):
    module = _load_module()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    monkeypatch.setattr(
        module,
        "_run_python_json",
        lambda *args, **kwargs: {
            "status": "SENT",
            "final_status": "SENT",
            "public_channel_ok": True,
            "admin_channel_ok": True,
            "telegram_attempted": True,
            "ledger_entries": 0,
            "ledger_path": "state/notifications/notification_ledger.jsonl",
            "elapsed_seconds": 0.123,
        },
    )

    with pytest.raises(module.ReleaseSmokeError, match="telegram smoke did not produce notification ledger"):
        module._verify_telegram_mock(
            workspace_root,
            selection_report={"selection_run_id": "run-1", "selection_bundle_hash": "bundle-1", "selection_stage": "FINALIZED"},
            top_configs=[{"ticker": "SOXS"}],
        )
