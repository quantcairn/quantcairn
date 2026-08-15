"""Tests for launchd plist template validation."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_LAUNCHD = PROJECT_ROOT / "deploy" / "launchd"


def _write_template(path: Path, program_arguments: list[str]) -> None:
    arguments = "\n".join(f"        <string>{value}</string>" for value in program_arguments)
    path.write_text(
        """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<plist version=\"1.0\"><dict>
    <key>Label</key><string>com.quantcairn.test</string>
    <key>ProgramArguments</key><array>
""" + arguments + """
    </array>
    <key>WorkingDirectory</key><string>REPLACE_WITH_PROJECT_ROOT</string>
</dict></plist>
""",
        encoding="utf-8",
    )


def test_validate_templates_script_present():
    assert (DEPLOY_LAUNCHD / "validate_templates.py").exists()


def test_validate_templates_passes():
    """Run the actual validator and confirm it returns exit code 0."""
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            str(DEPLOY_LAUNCHD / "validate_templates.py"),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, f"Validator failed:\n{result.stdout}\n{result.stderr}"


def test_validator_accepts_shell_supervisor_without_python_placeholder(tmp_path):
    from deploy.launchd.validate_templates import validate_template

    path = tmp_path / "shell.plist.template"
    _write_template(path, ["/bin/bash", "REPLACE_WITH_PROJECT_ROOT/scripts/start_top_engines.sh"])
    ok, messages = validate_template(path)
    assert ok, messages


def test_validator_rejects_missing_python_entrypoint(tmp_path):
    from deploy.launchd.validate_templates import validate_template

    path = tmp_path / "python.plist.template"
    _write_template(path, ["REPLACE_WITH_PYTHON_PATH"])
    ok, messages = validate_template(path)
    assert not ok
    assert any("ProgramArguments" in message or "placeholder" in message for message in messages)


def test_all_templates_have_readme():
    """Every .plist.template must be mentioned in README.md."""
    readme = (DEPLOY_LAUNCHD / "README.md").read_text()
    for tmpl in sorted(DEPLOY_LAUNCHD.glob("*.plist.template")):
        name = tmpl.name
        assert name in readme, f"Template {name} not mentioned in README.md"


def test_template_labels_are_quantcairn():
    """All labels must use com.quantcairn prefix."""
    import xml.etree.ElementTree as ET

    for tmpl in DEPLOY_LAUNCHD.glob("*.plist.template"):
        root = ET.parse(tmpl).getroot()
        dict_elt = root.find("dict")
        assert dict_elt is not None, f"No <dict> in {tmpl.name}"

        children = list(dict_elt)
        for i, child in enumerate(children):
            if child.tag == "key" and (child.text or "").strip() == "Label":
                label = children[i + 1].text
                assert label.startswith("com.quantcairn"), \
                    f"{tmpl.name}: label '{label}' must start with com.quantcairn"
                break


def test_templates_have_no_hardcoded_user_paths():
    """No /Users/<name> or /home/<name> in templates."""
    import re

    pattern = re.compile(r"/(?:Users|home)/\w+")
    for tmpl in DEPLOY_LAUNCHD.glob("*.plist.template"):
        content = tmpl.read_text()
        matches = pattern.findall(content)
        assert not matches, f"{tmpl.name}: hardcoded paths found: {matches}"


def test_ai_selector_template_uses_calendar_schedule():
    """AI selector must use fixed calendar triggers instead of minute polling."""
    tmpl = DEPLOY_LAUNCHD / "com.quantcairn.ai-selector.plist.template"
    content = tmpl.read_text()
    assert "<key>StartCalendarInterval</key>" in content
    assert "<key>StartInterval</key>" not in content
    assert "<integer>21</integer>" in content
    assert "<integer>22</integer>" in content
    assert "<integer>30</integer>" in content


def test_candidate_validation_template_uses_apply_scheduler():
    """Candidate validation launchd template must call the scheduler in APPLY mode."""
    tmpl = DEPLOY_LAUNCHD / "com.quantcairn.candidate-validation.plist.template"
    assert tmpl.exists()
    content = tmpl.read_text()
    assert "run_candidate_validation_scheduler.py" in content
    assert "--apply" in content
    assert "<key>Label</key>" in content
    assert "com.quantcairn.candidate-validation" in content
    assert "candidate-validation.out.log" in content
    assert "candidate-validation.err.log" in content
