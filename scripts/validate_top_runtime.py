#!/usr/bin/env python3
"""Validate the Python environment used by an immutable PAPER TOP release.

This command imports only local modules and installed dependencies. It never
connects to a broker, starts a server, or writes runtime state.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import site
import sys
from pathlib import Path


THIRD_PARTY_MODULES = ("yaml", "numpy", "pandas", "flask", "longbridge")
TOP_MODULES = (
    "run",
    "src.engine.trading_engine",
    "src.dashboard.server",
)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _editable_install(project_root: Path) -> bool:
    """Detect editable metadata or .pth entries pointing at source code."""
    try:
        distribution = importlib.metadata.distribution("quantcairn")
    except importlib.metadata.PackageNotFoundError:
        distribution = None
    if distribution is not None:
        direct_url = distribution.read_text("direct_url.json") or ""
        if '"editable": true' in direct_url.lower():
            return True

    site_dirs = list(site.getsitepackages())
    user_site = site.getusersitepackages()
    if user_site:
        site_dirs.append(user_site)
    for directory in site_dirs:
        for pth in Path(directory).glob("*.pth"):
            if pth.name.startswith("__editable__.quantcairn"):
                return True
            try:
                content = pth.read_text(encoding="utf-8")
            except OSError:
                continue
            if str(project_root.resolve()) in content:
                return True
    return False


def _import_modules(names: tuple[str, ...], project_root: Path) -> tuple[list[str], dict[str, str], list[str]]:
    imported: list[str] = []
    locations: dict[str, str] = {}
    failures: list[str] = []
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    for name in names:
        try:
            module = importlib.import_module(name)
            imported.append(name)
            locations[name] = str(Path(module.__file__).resolve()) if getattr(module, "__file__", None) else "BUILTIN"
        except Exception as exc:  # import gate must report every missing dependency
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return imported, locations, failures


def validate(project_root: Path) -> dict[str, object]:
    project_root = project_root.resolve()
    third_party, third_party_locations, third_party_failures = _import_modules(
        THIRD_PARTY_MODULES, project_root
    )
    core, core_locations, core_failures = _import_modules(TOP_MODULES, project_root)
    locations = {**third_party_locations, **core_locations}
    source_outside_release = {
        name: path for name, path in locations.items()
        if path != "BUILTIN" and not _under(Path(path), project_root)
    }
    failures = third_party_failures + core_failures
    return {
        "project_root": str(project_root),
        "release_sha": os.environ.get("SOXS_RELEASE_SHA", "UNKNOWN"),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "third_party_imports": third_party,
        "top_required_imports": core,
        "import_failures": failures,
        "module_locations": locations,
        "imports_outside_release": source_outside_release,
        "editable_install_present": _editable_install(project_root),
        "status": "PASS" if not failures and not source_outside_release and not _editable_install(project_root) else "BLOCKED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(os.environ.get("SOXS_PROJECT_DIR", ".")))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate(args.project_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
