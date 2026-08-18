"""Discover immediate built-in skill package directories deterministically."""

from __future__ import annotations

from pathlib import Path

from core.skills.errors import SkillLoadError


def discover_skill_packages(root: Path | str) -> tuple[Path, ...]:
    """Return every immediate package directory for fail-fast loading."""

    package_root = Path(root)
    if not package_root.exists():
        raise SkillLoadError(f"skill root does not exist: {package_root}")
    if package_root.is_symlink() or not package_root.is_dir():
        raise SkillLoadError(f"skill root is not a directory: {package_root}")
    return tuple(
        sorted(
            (path for path in package_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
    )


__all__ = ["discover_skill_packages"]
