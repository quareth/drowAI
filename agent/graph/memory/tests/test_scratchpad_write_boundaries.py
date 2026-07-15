"""Guardrails for deterministic scratchpad write ownership boundaries."""

from __future__ import annotations

import re
from pathlib import Path


def test_scratchpad_writes_are_centralized() -> None:
    """Ensure production scratchpad writes happen only via memory scratchpad helper."""
    repo_root = Path(__file__).resolve().parents[4]
    agent_root = repo_root / "agent" / "graph"
    write_pattern = re.compile(r"(?:interactive\.)?trace\.scratchpad\s*=")
    allowed_files = {
        (agent_root / "memory" / "scratchpad.py").resolve(),
    }

    violations: list[str] = []
    for path in agent_root.rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        content = path.read_text(encoding="utf-8")
        if write_pattern.search(content) and path.resolve() not in allowed_files:
            violations.append(path.relative_to(repo_root).as_posix())

    assert not violations, f"Unexpected scratchpad write locations: {violations}"
