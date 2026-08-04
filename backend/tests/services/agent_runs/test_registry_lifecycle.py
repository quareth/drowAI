"""Direct equivalence tests for pure process-local lifecycle construction."""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.services.agent_runs import registry_lifecycle
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.registry_contracts import LocalAgentRun
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
)


def _clock(*moments: datetime) -> Iterator[datetime]:
    return iter(moments)


def _entry_values(entry: LocalAgentRun) -> dict[str, Any]:
    return {
        field.name: getattr(entry, field.name)
        for field in dataclasses.fields(entry)
    }


def _assert_entries_equal(actual: LocalAgentRun, expected: LocalAgentRun) -> None:
    assert type(actual) is LocalAgentRun
    assert _entry_values(actual) == _entry_values(expected)


@pytest.mark.asyncio
async def test_build_queued_entry_matches_register_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))

    actual = await run_registry.register(assignment, graph_thread_id="child-thread-1")
    expected = registry_lifecycle.build_queued_entry(
        assignment=assignment,
        graph_thread_id="child-thread-1",
        created_at=created_at,
    )

    _assert_entries_equal(actual, expected)


@pytest.mark.asyncio
async def test_build_running_entry_matches_mark_running_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    started_at = datetime(2026, 8, 2, 12, 1, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at, started_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")

    actual = await run_registry.mark_running(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )
    expected = registry_lifecycle.build_running_entry(
        queued,
        started_at=started_at,
    )

    _assert_entries_equal(actual, expected)
    assert (
        registry_lifecycle.build_running_entry(actual, started_at=created_at).started_at
        == started_at
    )


@pytest.mark.asyncio
async def test_build_waiting_entry_matches_approval_wait_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")

    actual = await run_registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        accounted_usage_record_count=-5,
    )
    expected = registry_lifecycle.build_waiting_for_approval_entry(
        queued,
        accounted_usage_record_count=-5,
    )

    _assert_entries_equal(actual, expected)
    assert actual.accounted_usage_record_count == 0
    assert (
        registry_lifecycle.build_waiting_for_approval_entry(
            actual,
            accounted_usage_record_count=None,
        ).accounted_usage_record_count
        == 0
    )


@pytest.mark.asyncio
async def test_build_cancellation_requested_entry_matches_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")

    actual = await run_registry.request_cancellation(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )
    expected = registry_lifecycle.build_cancellation_requested_entry(queued)

    _assert_entries_equal(actual, expected)


@pytest.mark.asyncio
async def test_build_completed_entry_matches_record_completed_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    completed_at = datetime(2026, 8, 2, 12, 1, tzinfo=UTC)
    assignment = build_agent_assignment()
    result = build_agent_result(assignment)
    registry_clock = _clock(created_at, completed_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")

    actual = await run_registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=result,
    )
    expected = registry_lifecycle.build_completed_entry(
        queued,
        result=result,
        completed_at=completed_at,
    )

    _assert_entries_equal(actual, expected)


@pytest.mark.asyncio
async def test_build_failed_entry_matches_record_failed_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    completed_at = datetime(2026, 8, 2, 12, 1, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at, completed_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")

    actual = await run_registry.mark_failed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        safe_error="Subagent worker failed",
    )
    expected = registry_lifecycle.build_failed_entry(
        queued,
        safe_error="Subagent worker failed",
        completed_at=completed_at,
    )

    _assert_entries_equal(actual, expected)
    assert actual.result is not None
    assert actual.result.outcome == "failed"
    assert actual.result.summary == "Subagent run failed: Subagent worker failed"
    assert actual.result.limitations == ("Subagent worker failed",)
    assert actual.result.recommended_next_steps == (
        "Review the failure and decide whether a new bounded assignment is needed.",
    )


@pytest.mark.asyncio
async def test_build_cancelled_entry_matches_record_cancelled_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    completed_at = datetime(2026, 8, 2, 12, 1, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at, completed_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")

    actual = await run_registry.mark_cancelled(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )
    expected = registry_lifecycle.build_cancelled_entry(
        queued,
        completed_at=completed_at,
    )

    _assert_entries_equal(actual, expected)
    assert actual.result is not None
    assert actual.result.outcome == "cancelled"
    assert actual.result.summary == (
        "Subagent run was cancelled before completing its assignment."
    )
    assert actual.result.limitations == ("Subagent run was cancelled.",)
    assert actual.result.recommended_next_steps == (
        "Decide whether the cancelled assignment is still required.",
    )


@pytest.mark.asyncio
async def test_build_cancelled_entry_matches_waiting_cancellation_inline_path() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    completed_at = datetime(2026, 8, 2, 12, 1, tzinfo=UTC)
    assignment = build_agent_assignment()
    registry_clock = _clock(created_at, completed_at)
    run_registry = ProcessLocalAgentRunRegistry(clock=lambda: next(registry_clock))
    queued = await run_registry.register(assignment, graph_thread_id="child-thread-1")
    waiting = registry_lifecycle.build_waiting_for_approval_entry(
        queued,
        accounted_usage_record_count=None,
    )
    await run_registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )

    actual = await run_registry.request_cancellation(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )
    expected = registry_lifecycle.build_cancelled_entry(
        waiting,
        completed_at=completed_at,
        cancel_requested=True,
    )

    _assert_entries_equal(actual, expected)


def test_terminal_helpers_match_current_fallback_semantics() -> None:
    created_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    completed_at = datetime(2026, 8, 2, 12, 1, tzinfo=UTC)
    queued = registry_lifecycle.build_queued_entry(
        assignment=build_agent_assignment(),
        graph_thread_id="child-thread-1",
        created_at=created_at,
    )

    assert registry_lifecycle.is_terminal(queued) is False
    failed = registry_lifecycle.build_failed_entry(
        queued,
        safe_error="custom failure",
        completed_at=completed_at,
    )
    assert registry_lifecycle.is_terminal(failed) is True
    assert (
        registry_lifecycle.fallback_terminal_result(
            queued,
            status="failed",
            safe_error=None,
        ).summary
        == "Subagent run failed: Subagent worker failed"
    )
    with pytest.raises(ValueError, match="fallback result is only supported"):
        registry_lifecycle.fallback_terminal_result(
            queued,
            status="completed",
            safe_error=None,
        )


def test_registry_lifecycle_is_pure_and_facade_delegates_to_policy() -> None:
    lifecycle_path = (
        Path(__file__).resolve().parents[3]
        / "services/agent_runs/registry_lifecycle.py"
    )
    registry_path = (
        Path(__file__).resolve().parents[3] / "services/agent_runs/registry.py"
    )
    lifecycle_source = lifecycle_path.read_text(encoding="utf-8")
    lifecycle_tree = ast.parse(lifecycle_source)
    imports: set[tuple[int, str]] = set()
    for node in ast.walk(lifecycle_tree):
        if isinstance(node, ast.Import):
            imports.update((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add((node.level, node.module))

    assert imports == {
        (0, "__future__"),
        (0, "dataclasses"),
        (0, "datetime"),
        (1, "contracts"),
        (1, "registry_contracts"),
    }
    assert "asyncio.Lock" not in lifecycle_source
    assert "safe_inc" not in lifecycle_source
    assert "safe_gauge" not in lifecycle_source
    assert "_clock" not in lifecycle_source
    assert registry_lifecycle.__all__ == [
        "build_cancelled_entry",
        "build_cancellation_requested_entry",
        "build_completed_entry",
        "build_failed_entry",
        "build_queued_entry",
        "build_running_entry",
        "build_terminal_entry",
        "build_waiting_for_approval_entry",
        "fallback_terminal_result",
        "is_terminal",
    ]

    registry_source = registry_path.read_text(encoding="utf-8")
    registry_tree = ast.parse(registry_source)
    registry_imports_lifecycle = any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            in {"registry_lifecycle", "backend.services.agent_runs.registry_lifecycle"}
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module is None
            and any(alias.name == "registry_lifecycle" for alias in node.names)
        )
        for node in ast.walk(registry_tree)
    )
    assert registry_imports_lifecycle is True
    assert "def _is_terminal" not in registry_source
    assert "def _terminal_entry" not in registry_source
    assert "def _fallback_terminal_result" not in registry_source
