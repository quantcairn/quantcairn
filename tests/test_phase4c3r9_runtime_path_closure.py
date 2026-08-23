from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_operational_writes_use_external_runtime_roots(tmp_path: Path):
    code_root = tmp_path / "immutable-code"
    state_root = tmp_path / "runtime" / "state"
    reports_root = tmp_path / "runtime" / "reports"
    artifacts_root = tmp_path / "runtime" / "artifacts"
    logs_root = tmp_path / "runtime" / "logs"
    for path in (state_root, reports_root, artifacts_root, logs_root):
        path.mkdir(parents=True)

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT),
            "SOXS_PROJECT_DIR": str(code_root),
            "SOXS_STATE_DIR": str(state_root),
            "SOXS_REPORTS_DIR": str(reports_root),
            "SOXS_ARTIFACTS_DIR": str(artifacts_root),
            "SOXS_LOGS_DIR": str(logs_root),
            "SOXS_RUNTIME_AUDIT_DIR": str(logs_root),
            "QUANTCAIRN_EXECUTION_MODE": "PAPER",
            "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
        }
    )
    script = r'''
from pathlib import Path

from src.config.loader import _parse_config
from src.config.runtime_paths import runtime_paths
from src.outcome.collector import _save_state, write_summary
from src.outcome.weight_advisor import _write_report
from src.regime.models import REGIME_ARTIFACT_DIR
from src.reports.pretrade_report import PretradeReport
from src.reports.trade_audit import trade_log_path
from src.shadow.universe import ShadowUniverseConfig, default_shadow_output_directory

paths = runtime_paths()
assert paths.project_dir == Path(__import__("os").environ["SOXS_PROJECT_DIR"]).resolve()
assert paths.state_dir == Path(__import__("os").environ["SOXS_STATE_DIR"]).resolve()
assert paths.reports_dir == Path(__import__("os").environ["SOXS_REPORTS_DIR"]).resolve()
assert paths.artifacts_dir == Path(__import__("os").environ["SOXS_ARTIFACTS_DIR"]).resolve()
assert paths.logs_dir == Path(__import__("os").environ["SOXS_LOGS_DIR"]).resolve()

flags_path = paths.state_dir / "trading_flags.json"
flags_path.write_text('{"reduce_only_all": true}\n', encoding="utf-8")
config = _parse_config({"mode": "paper"})
assert config.position.reduce_only is True

pretrade_path = PretradeReport(today="2026-08-23").write()
assert pretrade_path.parent == paths.reports_dir
assert pretrade_path.read_text(encoding="utf-8").startswith("{")
assert trade_log_path(day="20260823").parent == paths.logs_dir

_save_state({"test-fill"})
write_summary({"schema_version": "test"})
_write_report({"status": "TEST_ONLY"})
learning_root = paths.artifacts_dir / "learning"
assert (learning_root / ".outcome_collector_state.json").exists()
assert (learning_root / "outcome_summary.json").exists()
assert (learning_root / "suggested_weights.json").exists()

assert REGIME_ARTIFACT_DIR == paths.artifacts_dir / "regime"
shadow_path = default_shadow_output_directory("SOXS.US", "15m")
assert shadow_path == paths.artifacts_dir / "shadow" / "soxs_15m"
shadow_config = ShadowUniverseConfig.for_symbol(
    "SOXS.US", output_directory=Path("artifacts/shadow/soxs_15m")
)
assert shadow_config.validate() == []
shadow_config.output_dir.mkdir(parents=True, exist_ok=True)
(shadow_config.output_dir / "runtime_state.json").write_text("{}\n", encoding="utf-8")
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    source_runtime_dirs = [
        code_root / "state",
        code_root / "reports",
        code_root / "artifacts",
        code_root / "logs",
    ]
    assert all(not path.exists() for path in source_runtime_dirs)
    assert not (PROJECT_ROOT / "state" / "trading_flags.json").exists()
    assert (reports_root / "pretrade_check_2026-08-23.json").exists()
    assert (artifacts_root / "shadow" / "soxs_15m" / "runtime_state.json").exists()


def test_shadow_relative_paths_are_confined_to_external_artifacts(monkeypatch, tmp_path: Path):
    from src.shadow import universe

    shadow_root = tmp_path / "artifacts" / "shadow"
    monkeypatch.setattr(universe, "SHADOW_ROOT_DIR", shadow_root)

    resolved = universe.resolve_shadow_output_directory(Path("artifacts/shadow/custom"))
    assert resolved == shadow_root / "custom"
    assert universe.is_safe_shadow_output_directory(resolved)
    assert universe.is_safe_shadow_output_directory(Path("artifacts/shadow/custom"))
    assert not universe.is_safe_shadow_output_directory(Path("../outside"))
