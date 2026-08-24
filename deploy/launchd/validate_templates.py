#!/usr/bin/env python3
"""Validate launchd plist templates.

Checks XML structure plus the operational contracts shared by the launchd
templates: explicit persistent roots, PAPER defaults, valid entrypoints, and
service-specific CLI/ownership boundaries.

Usage:
    python3 deploy/launchd/validate_templates.py
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEPLOY_DIR.parents[1]
README_PATH = DEPLOY_DIR / "README.md"

REQUIRED_PLACEHOLDERS = {
    "REPLACE_WITH_PYTHON_PATH",
    "REPLACE_WITH_PROJECT_ROOT",
}

RUNTIME_ROOT_PLACEHOLDERS = {
    "SOXS_PROJECT_DIR": "REPLACE_WITH_PROJECT_ROOT",
    "SOXS_RELEASE_SHA": "REPLACE_WITH_RELEASE_SHA",
    "SOXS_STATE_DIR": "REPLACE_WITH_STATE_ROOT",
    "SOXS_REPORTS_DIR": "REPLACE_WITH_REPORTS_ROOT",
    "SOXS_ARTIFACTS_DIR": "REPLACE_WITH_ARTIFACTS_ROOT",
    "SOXS_LOGS_DIR": "REPLACE_WITH_LOGS_ROOT",
}

KNOWN_SERVICES = {
    "com.quantcairn.ai-selector",
    "com.quantcairn.candidate-validation",
    "com.quantcairn.combined",
    "com.quantcairn.orphan-monitor",
    "com.quantcairn.research",
    "com.quantcairn.top-engines",
    "com.quantcairn.daily-runtime-snapshot",
}

FORBIDDEN_PATTERNS = [
    re.compile(r"/Users/\w+"),        # Hardcoded macOS home dirs
    re.compile(r"/home/\w+"),         # Hardcoded Linux home dirs
    re.compile(r"com\.soxs\."),       # Legacy label prefix
    re.compile(r"com\.openalpha\."),  # Legacy label prefix
]

EXPECTED_LABEL_PREFIX = "com.quantcairn"

PASS = 0
FAIL = 1


def find_template_paths() -> list[Path]:
    return sorted(DEPLOY_DIR.glob("*.plist.template"))


def parse_plist(path: Path) -> tuple[ET.Element | None, str | None]:
    """Return (dict_element, error_message)."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        if root.tag != "plist":
            return None, f"Root element is <{root.tag}>, expected <plist>"
        # Navigate into <plist><dict>
        dict_elt = root.find("dict")
        if dict_elt is None:
            return None, "No <dict> child found under <plist>"
        return dict_elt, None
    except ET.ParseError as exc:
        return None, f"XML parse error: {exc}"


def get_dict_value(elt: ET.Element, key: str) -> str | None:
    """Look up <key>k</key><string>v</string> in a <dict> element."""
    children = list(elt)
    for i, child in enumerate(children):
        if child.tag == "key" and (child.text or "").strip() == key:
            if i + 1 < len(children):
                next_elt = children[i + 1]
                if next_elt.tag == "string":
                    return (next_elt.text or "").strip()
                if next_elt.tag == "integer":
                    return (next_elt.text or "").strip()
                if next_elt.tag == "array":
                    # Join array string values
                    parts = []
                    for item in next_elt:
                        if item.tag == "string":
                            parts.append((item.text or "").strip())
                    return " ".join(parts)
    return None


def get_program_arguments(elt: ET.Element) -> list[str]:
    """Return ProgramArguments as individual strings for entrypoint checks."""
    children = list(elt)
    for i, child in enumerate(children):
        if child.tag == "key" and (child.text or "").strip() == "ProgramArguments":
            if i + 1 >= len(children) or children[i + 1].tag != "array":
                return []
            return [
                (item.text or "").strip()
                for item in children[i + 1]
                if item.tag == "string"
            ]
    return []


def get_environment_variables(elt: ET.Element) -> dict[str, str]:
    """Return the EnvironmentVariables string dictionary."""
    children = list(elt)
    for i, child in enumerate(children):
        if child.tag != "key" or (child.text or "").strip() != "EnvironmentVariables":
            continue
        if i + 1 >= len(children) or children[i + 1].tag != "dict":
            return {}
        env_children = list(children[i + 1])
        values: dict[str, str] = {}
        for j, env_key in enumerate(env_children):
            if env_key.tag != "key" or j + 1 >= len(env_children):
                continue
            value = env_children[j + 1]
            if value.tag == "string":
                values[(env_key.text or "").strip()] = (value.text or "").strip()
        return values
    return {}


def _service_label(elt: ET.Element) -> str:
    return get_dict_value(elt, "Label") or ""


def _script_reference(program_args: list[str]) -> str:
    """Extract the project-relative script from direct or shell entrypoints."""
    joined = " ".join(program_args)
    match = re.findall(r"REPLACE_WITH_PROJECT_ROOT/(scripts/[A-Za-z0-9_./-]+)", joined)
    return match[-1] if match else ""


def _is_shell_entrypoint(program_args: list[str]) -> bool:
    return bool(program_args and program_args[0] in {"/bin/bash", "/bin/sh", "/bin/zsh"})


def check_operational_contract(path: Path, root: ET.Element) -> list[str]:
    """Validate contracts that apply to known active service templates."""
    label = _service_label(root)
    if label not in KNOWN_SERVICES:
        return []

    violations: list[str] = []
    env = get_environment_variables(root)
    for key, expected in RUNTIME_ROOT_PLACEHOLDERS.items():
        if env.get(key) != expected:
            violations.append(f"  FAIL: {label} requires {key}={expected}")

    if env.get("QUANTCAIRN_HOME") != "REPLACE_WITH_PROJECT_ROOT":
        violations.append(f"  FAIL: {label} requires QUANTCAIRN_HOME=REPLACE_WITH_PROJECT_ROOT")
    if env.get("QUANTCAIRN_EXECUTION_MODE") != "PAPER":
        violations.append(f"  FAIL: {label} must default QUANTCAIRN_EXECUTION_MODE=PAPER")
    if label == "com.quantcairn.orphan-monitor" and env.get("SOXS_DISABLE_ORPHAN_MONITOR") != "1":
        violations.append("  FAIL: orphan monitor template must be disabled by default in PAPER")

    for key in ("StandardOutPath", "StandardErrorPath"):
        value = get_dict_value(root, key) or ""
        if "REPLACE_WITH_LOGS_ROOT/" not in value:
            violations.append(f"  FAIL: {label} {key} must use REPLACE_WITH_LOGS_ROOT")

    program_args = get_program_arguments(root)
    script = _script_reference(program_args)
    if not script:
        violations.append(f"  FAIL: {label} has no project-relative operational script")
    elif not (PROJECT_ROOT / script).is_file():
        violations.append(f"  FAIL: referenced script does not exist: {script}")

    joined_args = " ".join(program_args)
    if label == "com.quantcairn.research" and (
        "scripts/run_daily_research.py" not in joined_args
        or "--mode independent" not in joined_args
    ):
        violations.append("  FAIL: Research must invoke --mode independent")
    if label == "com.quantcairn.research" and script:
        cli_source = (PROJECT_ROOT / script).read_text(encoding="utf-8")
        if "--mode" not in cli_source or "independent" not in cli_source:
            violations.append("  FAIL: Research entrypoint does not expose independent mode")
    if label == "com.quantcairn.candidate-validation" and "--apply" not in joined_args:
        violations.append("  FAIL: Candidate Validation must preserve the apply scheduler contract")
    if label == "com.quantcairn.top-engines":
        if "scripts/start_top_engines.sh" not in joined_args or "restart" in joined_args:
            violations.append("  FAIL: TOP launchd must start the canonical supervisor, not restart mode")
        if "run_top_engine.sh" in joined_args:
            violations.append("  FAIL: TOP launchd must not launch individual engines")
    if label == "com.quantcairn.combined" and "8090" not in path.read_text(encoding="utf-8"):
        violations.append("  FAIL: Combined Dashboard template must document port 8090")
    if "multi_launch.sh" in joined_args or "restart_top_engines.sh" in joined_args:
        violations.append("  FAIL: obsolete TOP launcher referenced by active template")

    return violations


def check_forbidden_patterns(path: Path) -> list[str]:
    """Return list of violations."""
    violations = []
    content = path.read_text()
    for pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(content):
            violations.append(f"  Forbidden pattern '{match.group()}' at {path.name}")
    return violations


def check_label_prefix(elt: ET.Element) -> str | None:
    """Return violation message if label doesn't have correct prefix."""
    label = get_dict_value(elt, "Label")
    if label is None:
        return "  Missing <key>Label</key>"
    if not label.startswith(EXPECTED_LABEL_PREFIX):
        return f"  Label '{label}' does not start with '{EXPECTED_LABEL_PREFIX}'"
    return None


def collect_placeholders(elt: ET.Element) -> set[str]:
    """Collect all REPLACE_WITH_* values from the plist."""
    found = set()
    for elem in elt.iter():
        if elem.tag == "string" and elem.text:
            for match in re.finditer(r"REPLACE_WITH_\w+", elem.text):
                found.add(match.group())
    return found


def validate_template(path: Path) -> tuple[bool, list[str]]:
    """Return (valid, messages)."""
    msgs = []
    ok = True

    msgs.append(f"Checking: {path.name}")

    # 1. XML validity
    root, err = parse_plist(path)
    if err:
        msgs.append(f"  FAIL: {err}")
        return False, msgs
    msgs.append("  OK: XML valid")

    # 2. No forbidden patterns
    violations = check_forbidden_patterns(path)
    if violations:
        msgs.extend(violations)
        ok = False
    else:
        msgs.append("  OK: No hardcoded paths or legacy labels")

    # 3. Label check
    label_violation = check_label_prefix(root)
    if label_violation:
        msgs.append(f"  FAIL: {label_violation}")
        ok = False
    else:
        label = get_dict_value(root, "Label")
        msgs.append(f"  OK: Label = {label}")

    # 4. Placeholder presence. Shell supervisors legitimately provide their
    # own interpreter, so only Python entrypoints require the Python token.
    placeholders = collect_placeholders(root)
    program_args_list = get_program_arguments(root)
    entrypoint = program_args_list[0] if program_args_list else ""
    required_placeholders = {"REPLACE_WITH_PROJECT_ROOT"}
    if entrypoint not in {"/bin/bash", "/bin/sh"}:
        required_placeholders.add("REPLACE_WITH_PYTHON_PATH")
    missing = required_placeholders - placeholders
    if missing:
        msgs.append(f"  FAIL: Missing required placeholders: {', '.join(sorted(missing))}")
        ok = False
    else:
        msgs.append(f"  OK: All required placeholders present ({', '.join(sorted(placeholders))})")

    # 5. ProgramArguments
    program_args = " ".join(program_args_list)
    script_reference = _script_reference(program_args_list)
    if program_args_list and script_reference:
        msgs.append(f"  OK: ProgramArguments points to scripts/: {program_args}")
    elif program_args_list and len(program_args_list) >= 2 and not _is_shell_entrypoint(program_args_list):
        msgs.append(f"  FAIL: ProgramArguments does not reference a project script: {program_args}")
        ok = False
    else:
        msgs.append("  FAIL: Missing ProgramArguments")
        ok = False

    # 6. Operational runtime/PAPER/CLI contracts for known services.
    contract_violations = check_operational_contract(path, root)
    if contract_violations:
        msgs.extend(contract_violations)
        ok = False
    else:
        msgs.append("  OK: Runtime roots, PAPER mode, entrypoint, and service contract valid")

    return ok, msgs


def check_readme_placeholders() -> tuple[bool, list[str]]:
    """Verify README documents every placeholder used in templates."""
    msgs = []
    readme = README_PATH.read_text() if README_PATH.exists() else ""

    all_placeholders = set()
    for path in find_template_paths():
        root, _ = parse_plist(path)
        if root is None:
            continue
        all_placeholders |= collect_placeholders(root)

    documented = set()
    for match in re.finditer(r"`(REPLACE_WITH_\w+)`", readme):
        documented.add(match.group(1))

    missing_from_readme = all_placeholders - documented
    if missing_from_readme:
        msgs.append(f"  FAIL: Placeholders in templates but NOT in README: {', '.join(sorted(missing_from_readme))}")
        return False, msgs

    extra_in_readme = documented - all_placeholders
    if extra_in_readme:
        # Not a failure — README may document placeholders that aren't used
        # in the current templates but are valid for future use.
        msgs.append(f"  NOTE: Placeholders in README but not in any template: {', '.join(sorted(extra_in_readme))}")

    msgs.append(f"  OK: All {len(all_placeholders)} template placeholders documented in README")
    return True, msgs


def main() -> int:
    print("QuantCairn — launchd Template Validator")
    print(f"Directory: {DEPLOY_DIR}")
    print()

    paths = find_template_paths()
    if not paths:
        print("FAIL: No .plist.template files found in deploy/launchd/")
        return FAIL

    all_ok = True
    for path in paths:
        ok, msgs = validate_template(path)
        if not ok:
            all_ok = False
        for msg in msgs:
            print(msg)
        print()

    # Check README
    print("Checking: README.md placeholder documentation")
    readme_ok, readme_msgs = check_readme_placeholders()
    if not readme_ok:
        all_ok = False
    for msg in readme_msgs:
        print(msg)
    print()

    if all_ok:
        print(f"RESULT: All {len(paths)} template(s) valid. Ready for deployment.")
        return PASS
    else:
        print("RESULT: Some checks failed. Fix before deploying.")
        return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
