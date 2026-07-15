"""Tests for release-snapshot publication safety policies.

These tests lock path classification and workflow-template sanitization without
reading developer-local files or secret contents.
"""

from __future__ import annotations

import pytest

from scripts.check_publication_safety import (
    state_example_violations,
    tracked_path_violation,
)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env.production",
        ".npmrc",
        ".docker/config.json",
        ".aws/credentials",
        "credentials/runner.json",
        "agent/workspaces/task-1/result.txt",
        ".codex/agents/implementation-state.md",
        "capture.pcap",
    ],
)
def test_tracked_path_violation_rejects_private_state(path: str) -> None:
    assert tracked_path_violation(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "env.example",
        "deploy/env/runner.env.example",
        ".codex/agents/implementation-state.example.md",
        ".cursor/worktrees.json",
        "drowai_runner/credentials.py",
        "tests/fixtures/vpn/invalid-public-endpoint.ovpn",
    ],
)
def test_tracked_path_violation_allows_public_source(path: str) -> None:
    assert tracked_path_violation(path) is None


def test_state_example_violations_accepts_generalized_template() -> None:
    path = ".codex/agents/implementation-state.example.md"
    content = 'guide: "docs/path/to/implementation-guide.md"\nupdated_at: "YYYY-MM-DDTHH:MM:SSZ"\n'

    assert state_example_violations(path, content) == []


@pytest.mark.parametrize(
    ("content", "expected_reason"),
    [
        ('updated_at: "2026-07-15T12:00:00Z"', "contains a concrete timestamp"),
        ('guide: ".tmp/private-guide.md"', "contains a local filesystem path"),
        (
            'guide: "docs/plans/internal-feature.md"',
            "contains a historical implementation guide path",
        ),
    ],
)
def test_state_example_violations_rejects_local_history(
    content: str,
    expected_reason: str,
) -> None:
    path = ".cursor/agents/implementation-state.example.md"

    assert expected_reason in state_example_violations(path, content)
