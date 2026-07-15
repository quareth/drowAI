"""Validate DrowAI's public product-version metadata.

This module prevents release metadata drift across Python packaging, npm
metadata, the npm lockfile, the FastAPI application description, and an
optional target version under the changelog's Unreleased section. It does not
decide when a release occurs; a version becomes a release only when the
maintainer completes the documented release process and publishes its tag.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
BACKEND_VERSION_PATTERN = re.compile(
    r"\bversion\s*=\s*[\"'](?P<version>[^\"']+)[\"']"
)
UNRELEASED_SECTION_PATTERN = re.compile(
    r"^## \[Unreleased\]\s*$\n(?P<body>.*?)(?=^## \[|\Z)",
    re.MULTILINE | re.DOTALL,
)
CHANGELOG_TARGET_PATTERN = re.compile(
    r"^Target version:\s*`(?P<version>[^`]+)`\.\s*$",
    re.MULTILINE,
)


class VersionMetadataError(ValueError):
    """Raised when a required product-version source cannot be read."""


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VersionMetadataError(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise VersionMetadataError(f"{path.name} must contain a JSON object")
    return value


def _required_string(mapping: dict[str, object], key: str, *, source: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise VersionMetadataError(f"{source} must define a non-empty {key!r} string")
    return value


def collect_versions(root: Path = REPO_ROOT) -> dict[str, str]:
    """Return the product version declared by every public metadata source."""

    pyproject_path = root / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = pyproject["project"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise VersionMetadataError(f"Cannot read pyproject.toml project metadata: {exc}") from exc
    if not isinstance(project, dict):
        raise VersionMetadataError("pyproject.toml [project] must be a table")

    package = _read_json(root / "package.json")
    package_lock = _read_json(root / "package-lock.json")
    lock_packages = package_lock.get("packages")
    if not isinstance(lock_packages, dict) or not isinstance(lock_packages.get(""), dict):
        raise VersionMetadataError("package-lock.json must define the root packages entry")
    lock_root = lock_packages[""]

    backend_path = root / "backend" / "main.py"
    try:
        backend_source = backend_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionMetadataError(f"Cannot read backend/main.py: {exc}") from exc
    backend_match = BACKEND_VERSION_PATTERN.search(backend_source)
    if backend_match is None:
        raise VersionMetadataError("backend/main.py must declare the FastAPI version")

    changelog_path = root / "CHANGELOG.md"
    try:
        changelog_source = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionMetadataError(f"Cannot read CHANGELOG.md: {exc}") from exc
    unreleased_match = UNRELEASED_SECTION_PATTERN.search(changelog_source)
    if unreleased_match is None:
        raise VersionMetadataError("CHANGELOG.md must define an Unreleased section")
    changelog_target = CHANGELOG_TARGET_PATTERN.search(unreleased_match.group("body"))

    versions = {
        "backend/main.py": backend_match.group("version"),
    }
    if changelog_target is not None:
        versions["CHANGELOG.md target"] = changelog_target.group("version")
    versions.update(
        {
            "package-lock.json": _required_string(
                package_lock,
                "version",
                source="package-lock.json",
            ),
            "package-lock.json packages root": _required_string(
                lock_root,
                "version",
                source="package-lock.json root packages entry",
            ),
            "package.json": _required_string(package, "version", source="package.json"),
            "pyproject.toml": _required_string(
                project,
                "version",
                source="pyproject.toml [project]",
            ),
        }
    )
    return versions


def check_versions(root: Path = REPO_ROOT) -> list[str]:
    """Return human-readable errors for invalid or inconsistent versions."""

    try:
        versions = collect_versions(root)
    except VersionMetadataError as exc:
        return [str(exc)]

    errors = [
        f"{source} version {version!r} is not a supported Semantic Version"
        for source, version in versions.items()
        if SEMVER_PATTERN.fullmatch(version) is None
    ]
    if len(set(versions.values())) != 1:
        rendered = "; ".join(f"{source}={version}" for source, version in versions.items())
        errors.append(f"Product versions disagree: {rendered}")
    return errors


def main() -> int:
    """Run the consistency check as a command-line release gate."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to validate (defaults to this script's repository).",
    )
    args = parser.parse_args()

    errors = check_versions(args.root.resolve())
    if errors:
        for error in errors:
            print(f"[version-check] ERROR: {error}")
        return 1

    version = next(iter(collect_versions(args.root.resolve()).values()))
    print(f"[version-check] OK: all assigned version metadata uses {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
