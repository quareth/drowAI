"""Direct equivalence tests for process-local query and retention policy."""

from __future__ import annotations

import ast
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.services.agent_runs import registry_lifecycle, registry_queries
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.registry_contracts import AgentRunKey, LocalAgentRun
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
    build_runtime_identity,
)


def _entry(
    agent_run_id: str,
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    conversation_id: str = "conversation-1",
    graph_thread_id: str | None = None,
    created_at: datetime | None = None,
) -> LocalAgentRun:
    assignment = build_agent_assignment(
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        conversation_id=conversation_id,
        runtime_identity=build_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )
    return registry_lifecycle.build_queued_entry(
        assignment=assignment,
        graph_thread_id=graph_thread_id or f"thread-{agent_run_id}",
        created_at=created_at or datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )


def _key(entry: LocalAgentRun) -> AgentRunKey:
    return (entry.tenant_id, entry.task_id, entry.agent_run_id)


def _running(
    entry: LocalAgentRun,
    *,
    started_at: datetime | None = None,
) -> LocalAgentRun:
    return registry_lifecycle.build_running_entry(
        entry,
        started_at=started_at or datetime(2026, 8, 2, 12, 1, tzinfo=UTC),
    )


def _completed(
    entry: LocalAgentRun,
    *,
    completed_at: datetime | None = None,
) -> LocalAgentRun:
    return registry_lifecycle.build_completed_entry(
        entry,
        result=build_agent_result(entry.assignment),
        completed_at=completed_at or datetime(2026, 8, 2, 12, 2, tzinfo=UTC),
    )


def test_sort_keys_match_current_facade_ordering() -> None:
    entry = _completed(
        _running(
            _entry(
                "run-b",
                created_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            ),
            started_at=datetime(2026, 8, 2, 12, 1, tzinfo=UTC),
        ),
        completed_at=datetime(2026, 8, 2, 12, 3, tzinfo=UTC),
    )

    assert registry_queries.datetime_sort_key(None) == ""
    assert registry_queries.run_sort_key(entry) == (
        "2026-08-02T12:03:00+00:00",
        "2026-08-02T12:00:00+00:00",
        "run-b",
    )


@pytest.mark.asyncio
async def test_task_run_listing_preserves_insertion_order_and_scope() -> None:
    registry = ProcessLocalAgentRunRegistry()
    scoped_first = await registry.register(
        _entry("run-1").assignment,
        graph_thread_id="thread-1",
    )
    await registry.register(
        _entry("run-other-tenant", tenant_id=8).assignment,
        graph_thread_id="thread-other-tenant",
    )
    scoped_second = await registry.register(
        _entry("run-2").assignment,
        graph_thread_id="thread-2",
    )
    await registry.register(
        _entry("run-other-task", task_id=43).assignment,
        graph_thread_id="thread-other-task",
    )

    legacy = await registry.list_task_runs(tenant_id=7, task_id=42)
    extracted = registry_queries.list_task_runs(
        tuple(registry._runs.values()),
        tenant_id=7,
        task_id=42,
    )

    assert legacy == [scoped_first, scoped_second]
    assert extracted == legacy


def test_active_graph_thread_lookup_requires_unique_active_match() -> None:
    active = _running(_entry("run-1", graph_thread_id="child-thread"))
    duplicate = _running(_entry("run-2", graph_thread_id="child-thread"))
    foreign_tenant = _running(
        _entry("run-3", tenant_id=8, graph_thread_id="child-thread")
    )
    terminal = _completed(_entry("run-4", graph_thread_id="child-thread"))
    entries = (active, terminal, foreign_tenant)

    assert (
        registry_queries.find_active_by_graph_thread(
            entries,
            task_id=42,
            graph_thread_id="child-thread",
            tenant_id=7,
        )
        is active
    )
    assert (
        registry_queries.find_active_by_graph_thread(
            (active, duplicate),
            task_id=42,
            graph_thread_id="child-thread",
            tenant_id=7,
        )
        is None
    )
    assert (
        registry_queries.find_active_by_graph_thread(
            entries,
            task_id=42,
            graph_thread_id="child-thread",
        )
        is None
    )
    assert (
        registry_queries.find_active_by_graph_thread(
            (terminal,),
            task_id=42,
            graph_thread_id="child-thread",
            tenant_id=7,
        )
        is None
    )


def test_stale_finished_key_selection_matches_cutoff_and_protection_rules() -> None:
    cutoff = datetime(2026, 8, 2, 12, 15, tzinfo=UTC)
    old = _completed(
        _entry("run-old"),
        completed_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )
    at_cutoff = _completed(_entry("run-cutoff"), completed_at=cutoff)
    new = _completed(
        _entry("run-new"),
        completed_at=datetime(2026, 8, 2, 12, 16, tzinfo=UTC),
    )
    claimed = replace(
        _completed(
            _entry("run-claimed"),
            completed_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        ),
        result_claim_id="claim-1",
    )
    active = _running(_entry("run-active"))
    consumed = replace(
        _completed(_entry("run-consumed"), completed_at=cutoff),
        result_consumed=True,
    )
    runs = OrderedDict(
        (
            (_key(active), active),
            (_key(old), old),
            (_key(claimed), claimed),
            (_key(at_cutoff), at_cutoff),
            (_key(new), new),
            (_key(consumed), consumed),
        )
    )
    legacy = [
        key
        for key, entry in runs.items()
        if entry.completed_at is not None
        and entry.completed_at <= cutoff
        and entry.result_claim_id is None
    ]

    assert registry_queries.select_stale_finished_keys(runs, cutoff=cutoff) == legacy
    assert registry_queries.select_stale_finished_keys(runs, cutoff=cutoff) == [
        _key(old),
        _key(at_cutoff),
        _key(consumed),
    ]


def test_handoff_wait_status_matches_scoped_ready_inactive_and_active_states() -> None:
    active = _running(_entry("run-active", conversation_id="conversation-1"))
    foreign_conversation = _running(
        _entry("run-foreign-conversation", conversation_id="conversation-2")
    )
    ready = _completed(_entry("run-ready", conversation_id="conversation-1"))
    claimed = replace(_completed(_entry("run-claimed")), result_claim_id="claim-1")
    consumed = replace(_completed(_entry("run-consumed")), result_consumed=True)

    assert (
        registry_queries.handoff_wait_status(
            (active, foreign_conversation),
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
        )
        is None
    )
    assert (
        registry_queries.handoff_wait_status(
            (foreign_conversation,),
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
        )
        == "inactive"
    )
    assert (
        registry_queries.handoff_wait_status(
            (claimed, consumed),
            tenant_id=7,
            task_id=42,
            conversation_id=None,
        )
        == "inactive"
    )
    assert (
        registry_queries.handoff_wait_status(
            (active, ready),
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
        )
        == "ready"
    )
    assert (
        registry_queries.inactive_wait_status(
            (active, ready),
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
        )
        is None
    )
    assert (
        registry_queries.inactive_wait_status(
            (ready, foreign_conversation),
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
        )
        == "inactive"
    )


def test_registry_queries_are_pure_and_facade_delegates_to_policy() -> None:
    queries_path = (
        Path(__file__).resolve().parents[3] / "services/agent_runs/registry_queries.py"
    )
    registry_path = (
        Path(__file__).resolve().parents[3] / "services/agent_runs/registry.py"
    )
    queries_source = queries_path.read_text(encoding="utf-8")
    queries_tree = ast.parse(queries_source)
    imports: set[tuple[int, str]] = set()
    for node in ast.walk(queries_tree):
        if isinstance(node, ast.Import):
            imports.update((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add((node.level, node.module))

    assert imports == {
        (0, "__future__"),
        (0, "datetime"),
        (0, "typing"),
        (1, "registry_contracts"),
    }
    assert "asyncio.Lock" not in queries_source
    assert "safe_inc" not in queries_source
    assert "safe_gauge" not in queries_source
    assert "_clock" not in queries_source
    assert "del " not in queries_source
    assert registry_queries.__all__ == [
        "datetime_sort_key",
        "find_active_by_graph_thread",
        "handoff_wait_status",
        "inactive_wait_status",
        "list_task_runs",
        "run_sort_key",
        "select_stale_finished_keys",
    ]

    registry_source = registry_path.read_text(encoding="utf-8")
    registry_tree = ast.parse(registry_source)
    registry_imports_queries = any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            in {"registry_queries", "backend.services.agent_runs.registry_queries"}
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module is None
            and any(alias.name == "registry_queries" for alias in node.names)
        )
        for node in ast.walk(registry_tree)
    )
    assert registry_imports_queries is True
    assert "def _run_sort_key" not in registry_source
    assert "def _datetime_sort_key" not in registry_source
    assert "def _handoff_wait_status_locked" not in registry_source
    assert "has_active = False" not in registry_source
