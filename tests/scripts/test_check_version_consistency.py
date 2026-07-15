"""Tests for the repository product-version consistency check.

The tests keep public version metadata synchronized without treating unrelated
version strings in fixtures or dependency manifests as product versions.
"""

from pathlib import Path

from scripts import check_version_consistency


def _write_version_files(
    root: Path,
    *,
    backend_version: str = "0.1.0",
    changelog_version: str | None = "0.1.0",
) -> None:
    (root / "backend").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "drowai"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        '{"name": "drowai", "version": "0.1.0"}\n',
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        (
            '{"name": "drowai", "version": "0.1.0", '
            '"packages": {"": {"name": "drowai", "version": "0.1.0"}}}\n'
        ),
        encoding="utf-8",
    )
    (root / "backend" / "main.py").write_text(
        f'app = FastAPI(title="DrowAI", version="{backend_version}")\n',
        encoding="utf-8",
    )
    target = f"\nTarget version: `{changelog_version}`.\n" if changelog_version else "\n"
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n{target}",
        encoding="utf-8",
    )


def test_collect_versions_returns_all_public_version_sources(tmp_path: Path) -> None:
    _write_version_files(tmp_path)

    versions = check_version_consistency.collect_versions(tmp_path)

    assert versions == {
        "backend/main.py": "0.1.0",
        "CHANGELOG.md target": "0.1.0",
        "package-lock.json": "0.1.0",
        "package-lock.json packages root": "0.1.0",
        "package.json": "0.1.0",
        "pyproject.toml": "0.1.0",
    }


def test_check_versions_reports_mismatched_public_metadata(tmp_path: Path) -> None:
    _write_version_files(tmp_path, backend_version="0.1.1")

    errors = check_version_consistency.check_versions(tmp_path)

    assert errors == [
        "Product versions disagree: backend/main.py=0.1.1; "
        "CHANGELOG.md target=0.1.0; "
        "package-lock.json=0.1.0; package-lock.json packages root=0.1.0; "
        "package.json=0.1.0; pyproject.toml=0.1.0"
    ]


def test_check_versions_rejects_non_semantic_version(tmp_path: Path) -> None:
    _write_version_files(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "drowai"\nversion = "2026.29.dev.1"\n',
        encoding="utf-8",
    )

    errors = check_version_consistency.check_versions(tmp_path)

    assert any("not a supported Semantic Version" in error for error in errors)


def test_check_versions_reports_mismatched_changelog_target(tmp_path: Path) -> None:
    _write_version_files(tmp_path, changelog_version="0.2.0")

    errors = check_version_consistency.check_versions(tmp_path)

    assert errors == [
        "Product versions disagree: backend/main.py=0.1.0; "
        "CHANGELOG.md target=0.2.0; "
        "package-lock.json=0.1.0; package-lock.json packages root=0.1.0; "
        "package.json=0.1.0; pyproject.toml=0.1.0"
    ]


def test_check_versions_allows_unassigned_changelog_target(tmp_path: Path) -> None:
    _write_version_files(tmp_path, changelog_version=None)

    assert check_version_consistency.check_versions(tmp_path) == []
