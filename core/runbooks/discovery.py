"""Discover bundled RUNBOOK.md assets for the internal runbook registry."""

from __future__ import annotations

from pathlib import Path


def discover_runbook_paths(root: Path | str) -> tuple[Path, ...]:
    """Return RUNBOOK.md files under root in deterministic relative-path order."""

    root_path = Path(root)
    if not root_path.is_dir():
        return ()

    paths = [
        path
        for path in root_path.rglob("RUNBOOK.md")
        if path.is_file()
    ]
    return tuple(
        sorted(
            paths,
            key=lambda path: path.relative_to(root_path).as_posix(),
        )
    )


__all__ = ["discover_runbook_paths"]
