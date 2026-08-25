from __future__ import annotations

import plistlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "deploy/launchd/com.quantcairn.top-engines.plist.template"


def _rendered_top_plist(tmp_path: Path) -> dict:
    release = tmp_path / "release" / "abc123"
    config_root = tmp_path / "runtime" / "state"
    top_config_root = config_root / "top_configs_paper"
    logs_root = tmp_path / "runtime" / "logs"
    approved_python = tmp_path / "runtime" / "python"
    approved_python.parent.mkdir(parents=True, exist_ok=True)
    approved_python.write_text("#!/bin/sh\n", encoding="utf-8")
    approved_python.chmod(0o755)
    substitutions = {
        "REPLACE_WITH_PYTHON_PATH": str(approved_python),
        "REPLACE_WITH_PROJECT_ROOT": str(release),
        "REPLACE_WITH_RELEASE_SHA": "abc123",
        "REPLACE_WITH_CONFIG_ROOT": str(config_root),
        "REPLACE_WITH_TOP_CONFIG_ROOT": str(top_config_root),
        "REPLACE_WITH_STATE_ROOT": str(config_root),
        "REPLACE_WITH_REPORTS_ROOT": str(tmp_path / "runtime" / "reports"),
        "REPLACE_WITH_ARTIFACTS_ROOT": str(tmp_path / "runtime" / "artifacts"),
        "REPLACE_WITH_LOGS_ROOT": str(logs_root),
    }
    content = TEMPLATE.read_text(encoding="utf-8")
    for source, value in substitutions.items():
        content = content.replace(source, value)
    rendered = tmp_path / "com.quantcairn.top-engines.plist"
    rendered.write_text(content, encoding="utf-8")
    return plistlib.loads(rendered.read_bytes())


def test_top_template_requires_external_paper_runtime_contract(tmp_path: Path) -> None:
    plist = _rendered_top_plist(tmp_path)
    env = plist["EnvironmentVariables"]
    release = Path(plist["WorkingDirectory"])

    assert release.name == "abc123"
    assert env["SOXS_PROJECT_DIR"] == str(release)
    assert env["SOXS_RELEASE_SHA"] == "abc123"
    assert env["SOXS_CONFIG_DIR"].endswith("runtime/state")
    assert env["SOXS_TOP_CONFIG_DIR"].endswith("runtime/state/top_configs_paper")
    assert env["SOXS_LOG_DIR"].endswith("runtime/logs")
    assert env["SOXS_LOGS_DIR"] == env["SOXS_LOG_DIR"]
    assert env["SOXS_DISABLE_LIVE_CREDENTIALS"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["QUANTCAIRN_EXECUTION_MODE"] == "PAPER"
    assert Path(env["SOXS_PYTHON_BIN"]).is_file()
    assert "/Users/chenwei/quantcairn" not in str(plist)


def test_external_top_config_resolution_never_becomes_rooted_at_slash(tmp_path: Path) -> None:
    plist = _rendered_top_plist(tmp_path)
    env = plist["EnvironmentVariables"]
    top_dir = Path(env["SOXS_TOP_CONFIG_DIR"])
    top_dir.mkdir(parents=True)
    for index in range(1, 4):
        (top_dir / f"TOP{index}.yaml").write_text(
            f"mode: paper\nport: {8079 + index}\n", encoding="utf-8"
        )

    resolved = [top_dir / f"TOP{index}.yaml" for index in range(1, 4)]
    assert all(path.is_file() for path in resolved)
    assert all(str(path) != f"/TOP{index}.yaml" for index, path in enumerate(resolved, 1))
    assert [path.parent for path in resolved] == [top_dir] * 3


def test_top_template_contains_side_effect_free_preflight_contract() -> None:
    content = TEMPLATE.read_text(encoding="utf-8")
    for token in (
        "REPLACE_WITH_PROJECT_ROOT",
        "REPLACE_WITH_PYTHON_PATH",
        "REPLACE_WITH_CONFIG_ROOT",
        "REPLACE_WITH_TOP_CONFIG_ROOT",
        "REPLACE_WITH_LOGS_ROOT",
        "SOXS_DISABLE_LIVE_CREDENTIALS",
        "QUANTCAIRN_EXECUTION_MODE",
    ):
        assert token in content
    assert "scripts/start_top_engines.sh" in content
    assert "launchctl" not in content
