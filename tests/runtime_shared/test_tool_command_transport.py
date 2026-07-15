"""Tests for shared runner tool-command transport normalization."""

from __future__ import annotations

import pytest

from runtime_shared.tool_command_transport import (
    TRANSPORT_FILE_COMM,
    TRANSPORT_PTY,
    normalize_tool_command_transport,
)


@pytest.mark.parametrize("value", ["pty", "PTY", " terminal ", "TERMINAL"])
def test_normalize_tool_command_transport_pty_aliases(value: str) -> None:
    assert normalize_tool_command_transport(value) == TRANSPORT_PTY


@pytest.mark.parametrize(
    "value",
    ["file", "file-comm", "file_comm", "jsonl", "container", " FILE_COMM "],
)
def test_normalize_tool_command_transport_file_comm_aliases(value: str) -> None:
    assert normalize_tool_command_transport(value) == TRANSPORT_FILE_COMM


@pytest.mark.parametrize("value", [None, "", "direct", "docker"])
def test_normalize_tool_command_transport_unknown_values(value: object) -> None:
    assert normalize_tool_command_transport(value) is None
