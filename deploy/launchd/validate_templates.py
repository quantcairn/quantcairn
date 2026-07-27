#!/usr/bin/env python3
"""Validate launchd plist templates.

Checks:
1. All template files are valid XML / plist structure
2. No hardcoded personal paths (e.g. /Users/chenwei)
3. All REPLACE_WITH_* placeholders are present where expected
4. README documents every placeholder found in templates
5. All labels use the com.quantcairn prefix

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

    # 4. Placeholder presence
    placeholders = collect_placeholders(root)
    missing = REQUIRED_PLACEHOLDERS - placeholders
    if missing:
        msgs.append(f"  FAIL: Missing required placeholders: {', '.join(sorted(missing))}")
        ok = False
    else:
        msgs.append(f"  OK: All required placeholders present ({', '.join(sorted(placeholders))})")

    # 5. ProgramArguments
    program_args = get_dict_value(root, "ProgramArguments")
    if program_args:
        if "scripts/" in program_args:
            msgs.append(f"  OK: ProgramArguments points to scripts/: {program_args}")
        else:
            msgs.append(f"  WARN: ProgramArguments does not reference scripts/: {program_args}")
    else:
        msgs.append("  FAIL: Missing ProgramArguments")
        ok = False

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
