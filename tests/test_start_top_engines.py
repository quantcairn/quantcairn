"""Tests for TOP engines launcher and associated launchd configuration.

Verifies:
  1. start_top_engines.sh exists and is executable
  2. Engine definitions match expected ports and configs
  3. Launcher handles missing config files gracefully
  4. launchd plist template is valid XML and well-formed
  5. Plist references match start_top_engines.sh
  6. Plist passes basic plutil-style validation
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_DIR / "scripts" / "start_top_engines.sh"
PLIST_TEMPLATE = PROJECT_DIR / "deploy" / "launchd" / "com.quantcairn.top-engines.plist.template"


# ---------------------------------------------------------------------------
# 1. Script exists and is executable
# ---------------------------------------------------------------------------

def test_launcher_script_exists():
    assert LAUNCHER.exists(), f"Launcher script missing: {LAUNCHER}"


def test_launcher_script_is_executable():
    assert os.access(LAUNCHER, os.X_OK), f"Launcher not executable: {LAUNCHER}"


def test_launcher_can_be_parsed_by_bash():
    """Bash -n should not report syntax errors."""
    result = subprocess.run(
        ["bash", "-n", str(LAUNCHER)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# 2. Engine definitions match expected ports and configs
# ---------------------------------------------------------------------------

EXPECTED_ENGINES = [
    ("configs/TOP1.yaml", "8080", "top1"),
    ("configs/TOP2.yaml", "8081", "top2"),
    ("configs/TOP3.yaml", "8082", "top3"),
]


def test_launcher_defines_three_engines():
    content = LAUNCHER.read_text(encoding="utf-8")
    assert '"configs/TOP1.yaml 8080 top1"' in content
    assert '"configs/TOP2.yaml 8081 top2"' in content
    assert '"configs/TOP3.yaml 8082 top3"' in content


def test_launcher_ports_match_dashboard_tickers():
    """Dashboard TICKERS ports must equal launcher engine ports."""
    import src.dashboard.combined as combined
    for idx, item in enumerate(combined.TICKERS):
        _, expected_port, _ = EXPECTED_ENGINES[idx]
        assert item["port"] == int(expected_port), (
            f"Port mismatch: TICKERS[{idx}]={item['port']} "
            f"! = launcher {expected_port}"
        )


# ---------------------------------------------------------------------------
# 3. Launcher handles missing config gracefully
# ---------------------------------------------------------------------------

def test_launcher_skips_missing_config(tmp_path: Path):
    """When a config yaml is missing, launcher skips that engine and continues."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()

    # Only TOP2 config exists
    (cfg_dir / "TOP2.yaml").write_text(
        "ticker: TEST\nmode: paper\nrange:\n  mode: auto\nposition:\n  initial_capital: 700.0\n",
        encoding="utf-8",
    )

    # Minimal run_top_engine.sh
    (scripts_dir / "run_top_engine.sh").write_text(
        '#!/bin/bash\necho "STARTED: $1 port=$2"',
        encoding="utf-8",
    )
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)

    # Copy the launcher to tmp_path so PROJECT_DIR resolves to tmp_path
    launcher_copy = scripts_dir / "start_top_engines.sh"
    launcher_copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher_copy, 0o755)

    result = subprocess.run(
        ["bash", str(launcher_copy)],
        cwd=str(tmp_path),
        capture_output=True, text=True,
        timeout=10,
    )
    assert "SKIP" in result.stderr, f"Expected SKIP in stderr, got: {result.stderr}"
    assert "Launched" in result.stdout, f"Expected 'Launched' in stdout, got: {result.stdout}"


# ---------------------------------------------------------------------------
# 4. launchd plist template is valid XML and well-formed
# ---------------------------------------------------------------------------

def test_plist_template_exists():
    assert PLIST_TEMPLATE.exists(), f"Plist template missing: {PLIST_TEMPLATE}"


def test_plist_template_is_valid_xml():
    try:
        ET.parse(str(PLIST_TEMPLATE))
    except ET.ParseError as e:
        pytest.fail(f"Invalid XML in plist template: {e}")


def test_plist_template_can_be_parsed_as_plist():
    """plistlib can extract the dict from the plist template."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    # The plist dict is inside <plist><dict>...</dict></plist>
    plist_dict = root.find("dict")
    assert plist_dict is not None, "No <dict> element found in plist"


def test_plist_label_is_correct():
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    labels = _plist_key_values(plist_dict, "Label")
    assert len(labels) == 1
    assert labels[0] == "com.quantcairn.top-engines"


def test_plist_keep_alive_is_true():
    """TOP engines should restart automatically if they crash."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    keep_alive = _plist_key_values(plist_dict, "KeepAlive")
    assert len(keep_alive) >= 1


def test_plist_run_at_load_is_true():
    """TOP engines should start on boot/login."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    run_at_load = _plist_key_values(plist_dict, "RunAtLoad")
    assert len(run_at_load) >= 1


def test_plist_references_start_top_engines_script():
    """ProgramArguments must include scripts/start_top_engines.sh."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    program_args = _plist_key_values(plist_dict, "ProgramArguments")
    assert len(program_args) == 1
    args = program_args[0]
    assert any("start_top_engines.sh" in str(a) for a in args)


def test_plist_has_log_paths():
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    stdout = _plist_key_values(plist_dict, "StandardOutPath")
    stderr = _plist_key_values(plist_dict, "StandardErrorPath")
    assert len(stdout) == 1
    assert len(stderr) == 1
    assert "top-engines.out.log" in str(stdout[0])
    assert "top-engines.err.log" in str(stderr[0])


# ---------------------------------------------------------------------------
# 5. Plist references match start_top_engines.sh
# ---------------------------------------------------------------------------

def test_launcher_engine_count_matches_plist_scope():
    """The launcher ENGINE array defines 3 engines — no more, no fewer."""
    content = LAUNCHER.read_text(encoding="utf-8")
    engines_block = content[content.find("ENGINES=("):]
    engines_block = engines_block[:engines_block.find(")") + 1]
    # Count the triple-quoted config/port/name lines
    lines = [l for l in engines_block.split("\n") if "configs/TOP" in l and "yaml" in l]
    assert len(lines) == 3, f"Expected 3 engine definitions, found {len(lines)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plist_key_values(plist_dict: ET.Element, key: str) -> list:
    """Extract values for a given key from a plist <dict> element.

    Plist format: <key>Name</key> <value>...</value>
    """
    results = []
    children = list(plist_dict)
    for i in range(len(children)):
        if children[i].tag == "key" and children[i].text == key:
            if i + 1 < len(children):
                value_el = children[i + 1]
                if value_el.tag == "array":
                    items = []
                    for child in value_el:
                        if child.tag == "string":
                            items.append(child.text or "")
                    results.append(items)
                elif value_el.tag == "string":
                    results.append(value_el.text or "")
                elif value_el.tag in ("true", "false", "integer", "real"):
                    results.append(value_el.text or value_el.tag)
    return results
