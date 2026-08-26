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
import sysconfig
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RuntimeTrustRoots:
    """Filesystem roots trusted by the immutable-release import gate."""

    project_root: Path
    python_bin: Path
    python_prefix: Path
    site_package_roots: tuple[Path, ...]
    standard_library_roots: tuple[Path, ...]


def _approved_python_bin() -> Path:
    configured = str(os.environ.get("SOXS_PYTHON_BIN") or "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(sys.executable).resolve()


def _site_package_roots(python_prefix: Path) -> tuple[Path, ...]:
    roots: set[Path] = set()
    for pattern in (
        "lib/python*/site-packages",
        "lib/python*/dist-packages",
        "site-packages",
    ):
        roots.update(path.resolve() for path in python_prefix.glob(pattern))
    for path in site.getsitepackages():
        resolved = Path(path).resolve()
        if _under(resolved, python_prefix):
            roots.add(resolved)
    return tuple(sorted(roots, key=str))


def _standard_library_roots() -> tuple[Path, ...]:
    roots: set[Path] = set()
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib"):
        value = paths.get(key)
        if value:
            roots.add(Path(value).resolve())
    return tuple(sorted(roots, key=str))


def _runtime_trust_roots(project_root: Path) -> RuntimeTrustRoots:
    python_bin = _approved_python_bin()
    python_prefix = python_bin.parent.parent.resolve()
    return RuntimeTrustRoots(
        project_root=project_root.resolve(),
        python_bin=python_bin,
        python_prefix=python_prefix,
        site_package_roots=_site_package_roots(python_prefix),
        standard_library_roots=_standard_library_roots(),
    )


def _classify_import_origin(path: str, roots: RuntimeTrustRoots) -> str:
    if path == "BUILTIN":
        return "STANDARD_LIBRARY"
    origin = Path(path).resolve()
    if _under(origin, roots.project_root):
        return "PROJECT_RELEASE"
    if any(_under(origin, root) for root in roots.site_package_roots):
        return "APPROVED_VENV"
    if any(_under(origin, root) for root in roots.standard_library_roots):
        return "STANDARD_LIBRARY"
    return "FORBIDDEN_EXTERNAL"


def _release_venv_identity(project_root: Path, python_prefix: Path) -> dict[str, str]:
    """Check release/<sha> and venvs/<sha> when the deployment layout exposes both."""
    configured_sha = str(os.environ.get("SOXS_RELEASE_SHA") or "").strip()
    release_sha = configured_sha or (
        project_root.name if project_root.parent.name == "releases" else ""
    )
    venv_sha = python_prefix.name if python_prefix.parent.name == "venvs" else ""
    if not release_sha or not venv_sha:
        return {"status": "NOT_REQUIRED", "release_sha": release_sha, "venv_sha": venv_sha}
    status = "PASS" if release_sha == venv_sha else "FAIL"
    return {"status": status, "release_sha": release_sha, "venv_sha": venv_sha}


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
    trust_roots = _runtime_trust_roots(project_root)
    third_party, third_party_locations, third_party_failures = _import_modules(
        THIRD_PARTY_MODULES, project_root
    )
    core, core_locations, core_failures = _import_modules(TOP_MODULES, project_root)
    locations = {**third_party_locations, **core_locations}
    import_origin_classes = {
        name: _classify_import_origin(path, trust_roots)
        for name, path in locations.items()
    }
    forbidden_external = {
        name: path for name, path in locations.items()
        if import_origin_classes[name] == "FORBIDDEN_EXTERNAL"
    }
    failures = third_party_failures + core_failures
    editable_install_present = _editable_install(project_root)
    release_venv_identity = _release_venv_identity(project_root, trust_roots.python_prefix)
    return {
        "project_root": str(project_root),
        "release_sha": os.environ.get("SOXS_RELEASE_SHA", "UNKNOWN"),
        "python_executable": str(trust_roots.python_bin),
        "python_prefix": str(trust_roots.python_prefix),
        "python_version": sys.version.split()[0],
        "third_party_imports": third_party,
        "top_required_imports": core,
        "import_failures": failures,
        "module_locations": locations,
        "import_origin_classes": import_origin_classes,
        "imports_outside_release": forbidden_external,
        "forbidden_external_imports": forbidden_external,
        "approved_site_package_roots": [str(path) for path in trust_roots.site_package_roots],
        "standard_library_roots": [str(path) for path in trust_roots.standard_library_roots],
        "editable_install_present": editable_install_present,
        "release_venv_identity": release_venv_identity,
        "status": (
            "PASS"
            if not failures
            and not forbidden_external
            and not editable_install_present
            and release_venv_identity["status"] != "FAIL"
            else "BLOCKED"
        ),
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
