#!/usr/bin/env python3
"""Check that release version metadata is internally consistent.

This script is intentionally small so it can be reused by local release
preparation and the GitHub release workflow.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _read_pyproject_version(project_root: Path | None = None) -> str:
    root = project_root or PROJECT_ROOT
    pyproject_path = root / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = str(data["project"]["version"]).strip()
    if not version:
        raise ValueError("pyproject.toml project.version is empty")
    return version


def _read_package_version() -> str:
    import quantcairn

    version = str(quantcairn.__version__).strip()
    if not version:
        raise ValueError("quantcairn.__version__ is empty")
    return version


def _normalize_release_tag_version(tag: str) -> str:
    raw = str(tag).strip()
    if not raw:
        raise ValueError("release tag is empty")
    if raw.startswith("refs/tags/"):
        raw = raw.removeprefix("refs/tags/")
    if raw.startswith("v"):
        raw = raw[1:]
    version = raw.split("-", 1)[0]
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"release tag {tag!r} does not look like a supported release tag")
    return version


def check_release_version(*, tag: str | None = None, project_root: Path | None = None) -> dict[str, str]:
    pyproject_version = _read_pyproject_version(project_root)
    package_version = _read_package_version()
    if pyproject_version != package_version:
        raise ValueError(
            "package version mismatch: "
            f"pyproject.toml={pyproject_version!r} quantcairn.__version__={package_version!r}"
        )

    result: dict[str, str] = {
        "pyproject_version": pyproject_version,
        "package_version": package_version,
    }
    if tag is not None:
        release_tag_version = _normalize_release_tag_version(tag)
        if release_tag_version != pyproject_version:
            raise ValueError(
                "release tag version mismatch: "
                f"tag={tag!r} ({release_tag_version!r}) pyproject.toml={pyproject_version!r}"
            )
        result["release_tag_version"] = release_tag_version
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check QuantCairn release version consistency.")
    parser.add_argument("--tag", default=None, help="Release tag name to validate (e.g. vX.Y.Z-demo).")
    parser.add_argument(
        "--project-root",
        default=None,
        help="Repository root containing pyproject.toml (defaults to the repo root).",
    )
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve() if args.project_root else None
    try:
        result = check_release_version(tag=args.tag, project_root=project_root)
    except Exception as exc:  # pragma: no cover - exercised via CLI tests
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary: list[str] = [
        f"pyproject={result['pyproject_version']}",
        f"package={result['package_version']}",
    ]
    if "release_tag_version" in result:
        summary.append(f"tag={result['release_tag_version']}")
    print("Version OK:", ", ".join(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
