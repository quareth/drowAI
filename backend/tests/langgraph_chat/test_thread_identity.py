"""Tests for canonical LangGraph checkpoint thread identity helpers."""

from __future__ import annotations

import pytest

from backend.services.langgraph_chat.checkpoint.thread_identity import (
    format_graph_thread_id,
    generate_graph_thread_id,
    legacy_task_thread_id,
    normalize_graph_thread_id,
    owned_checkpoint_thread_ids,
)


def test_graph_thread_id_formats_internal_checkpoint_thread() -> None:
    graph_thread_id = "a" * 32

    assert format_graph_thread_id(graph_thread_id, task_id=7) == f"graph-{graph_thread_id}"
    assert legacy_task_thread_id(7) == "task-7"


def test_graph_thread_id_validation_rejects_reusable_task_identity() -> None:
    assert normalize_graph_thread_id("task-7") is None
    with pytest.raises(RuntimeError, match="graph_thread_id"):
        format_graph_thread_id("task-7", task_id=7)


def test_owned_checkpoint_thread_ids_keep_legacy_cleanup_when_graph_id_missing() -> None:
    assert owned_checkpoint_thread_ids(task_id=7, graph_thread_id="bad") == ("task-7",)


def test_generate_graph_thread_id_returns_valid_hex_identity() -> None:
    generated = generate_graph_thread_id()

    assert normalize_graph_thread_id(generated) == generated
