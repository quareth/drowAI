"""Tests for shared runtime workspace write-mode policy."""

from __future__ import annotations

import pytest

from runtime_shared.workspace_write_mode import (
    WORKSPACE_WRITE_MODE_APPEND,
    WORKSPACE_WRITE_MODE_WRITE,
    normalize_workspace_write_mode,
    workspace_path_allows_append,
)


@pytest.mark.parametrize("value", [None, "", "write", " WRITE "])
def test_normalize_workspace_write_mode_defaults_to_write(value: object) -> None:
    assert normalize_workspace_write_mode(value) == WORKSPACE_WRITE_MODE_WRITE


@pytest.mark.parametrize("value", ["append", "APPEND"])
def test_normalize_workspace_write_mode_accepts_append(value: object) -> None:
    assert normalize_workspace_write_mode(value) == WORKSPACE_WRITE_MODE_APPEND


@pytest.mark.parametrize("value", ["replace", "a", object()])
def test_normalize_workspace_write_mode_rejects_unknown_values(value: object) -> None:
    assert normalize_workspace_write_mode(value) is None


@pytest.mark.parametrize(
    "path",
    ["index/chunks_task-1.jsonl", "index/subdir/chunks.jsonl"],
)
def test_workspace_path_allows_append_for_index_paths(path: str) -> None:
    assert workspace_path_allows_append(path) is True


@pytest.mark.parametrize(
    "path",
    ["artifacts/out.txt", "../index/chunks.jsonl", "/index/chunks.jsonl"],
)
def test_workspace_path_allows_append_rejects_non_index_paths(path: str) -> None:
    assert workspace_path_allows_append(path) is False
