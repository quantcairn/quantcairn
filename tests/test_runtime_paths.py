from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _runtime_env(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    code_root = tmp_path / "code"
    roots = {
        "project": code_root,
        "state": tmp_path / "runtime" / "state",
        "reports": tmp_path / "runtime" / "reports",
        "artifacts": tmp_path / "runtime" / "artifacts",
        "logs": tmp_path / "runtime" / "logs",
    }
    code_root.mkdir()
    env = os.environ.copy()
    env.update({
        "SOXS_PROJECT_DIR": str(code_root),
        "SOXS_STATE_DIR": str(roots["state"]),
        "SOXS_REPORTS_DIR": str(roots["reports"]),
        "SOXS_ARTIFACTS_DIR": str(roots["artifacts"]),
        "SOXS_LOGS_DIR": str(roots["logs"]),
        "PYTHONPATH": str(PROJECT_ROOT),
    })
    return env, roots


def test_explicit_runtime_roots_are_side_effect_free(tmp_path, monkeypatch):
    env, roots = _runtime_env(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
from src.config.runtime_paths import runtime_paths
from src.openalpha.selection_state import selection_state_path
from src.dashboard.snapshots import dashboard_snapshot_path
from src.notifier.alerts import default_ai_selection_notification_ledger_path
from src.candidate_validation.performance_tracker import CandidatePerformanceTracker
from src.candidate_validation.store import CandidateValidationStore

p = runtime_paths()
store = CandidateValidationStore()
tracker = CandidatePerformanceTracker()
print(json.dumps({
    "paths": {key: str(value) for key, value in p.__dict__.items()},
    "selection_state": str(selection_state_path()),
    "dashboard_snapshot": str(dashboard_snapshot_path("status")),
    "notification_ledger": str(default_ai_selection_notification_ledger_path()),
    "candidate_root": str(store.root_dir),
    "performance_root": str(tracker.root_dir),
}))
""",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["paths"]["project_dir"] == str(roots["project"])
    assert payload["paths"]["state_dir"] == str(roots["state"])
    assert payload["paths"]["reports_dir"] == str(roots["reports"])
    assert payload["paths"]["artifacts_dir"] == str(roots["artifacts"])
    assert payload["paths"]["logs_dir"] == str(roots["logs"])
    assert payload["selection_state"].startswith(str(roots["state"]))
    assert payload["dashboard_snapshot"].startswith(str(roots["state"]))
    assert payload["notification_ledger"].startswith(str(roots["state"]))
    assert payload["candidate_root"].startswith(str(roots["artifacts"]))
    assert payload["performance_root"].startswith(str(roots["artifacts"]))
    assert not (roots["project"] / "state").exists()
    assert not (roots["project"] / "reports").exists()
    assert not (roots["project"] / "artifacts").exists()
    assert not (roots["project"] / "logs").exists()
    assert (roots["artifacts"] / "candidates").is_dir()


def test_defaults_preserve_project_layout(tmp_path, monkeypatch):
    for name in (
        "SOXS_PROJECT_DIR",
        "SOXS_STATE_DIR",
        "SOXS_REPORTS_DIR",
        "SOXS_ARTIFACTS_DIR",
        "SOXS_LOGS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmp_path))
    from src.config.runtime_paths import runtime_paths

    paths = runtime_paths()
    assert paths.project_dir == tmp_path.resolve()
    assert paths.state_dir == tmp_path.resolve() / "state"
    assert paths.reports_dir == tmp_path.resolve() / "reports"
    assert paths.artifacts_dir == tmp_path.resolve() / "artifacts"
    assert paths.logs_dir == tmp_path.resolve() / "logs"


def test_runtime_identity_is_redacted_and_uses_explicit_roots(tmp_path):
    env, roots = _runtime_env(tmp_path)
    env["SOXS_PROJECT_DIR"] = str(PROJECT_ROOT)
    env["QUANTCAIRN_EXECUTION_MODE"] = "PAPER"
    env["SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN"] = "secret-token"
    env["SOXS_OPENALPHA_TELEGRAM_CHAT_ID"] = "secret-chat"
    result = subprocess.run(
        [sys.executable, "scripts/runtime_identity.py", "--json"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "secret-token" not in result.stdout
    assert "secret-chat" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["execution_mode"] == "PAPER"
    assert payload["state_root"] == str(roots["state"])
    assert payload["reports_root"] == str(roots["reports"])
    assert payload["artifacts_root"] == str(roots["artifacts"])
    assert payload["logs_root"] == str(roots["logs"])
    assert payload["telegram"] == {
        "bot_token_set": True,
        "public_chat_id_set": True,
        "admin_chat_id_set": False,
    }
