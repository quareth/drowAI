"""Import-path and contract tests for process-local registry contracts."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from backend.services.agent_runs import registry
from backend.services.agent_runs import registry_contracts
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
)


_MOVED_SYMBOLS = (
    "ACTIVE_AGENT_RUN_STATUSES",
    "DEFAULT_FINISHED_RETENTION",
    "TERMINAL_AGENT_RUN_STATUSES",
    "ActiveAgentRunExistsError",
    "AgentRunKey",
    "AgentRunIdentityCollisionError",
    "AgentRunNotFoundError",
    "AgentRunTransition",
    "ClaimedHandoffBatch",
    "HandoffClaimNotFoundError",
    "HandoffWaitStatus",
    "LocalAgentRun",
)

_EXPECTED_LOCAL_AGENT_RUN_FIELDS = (
    "graph_thread_id",
    "assignment",
    "status",
    "lifecycle_version",
    "created_at",
    "started_at",
    "completed_at",
    "result",
    "safe_error",
    "task_handle",
    "cancel_requested",
    "result_consumed",
    "result_claim_id",
    "accounted_usage_record_count",
)


def _dataclass_shape(cls: type[Any]) -> tuple[Any, ...]:
    params = cls.__dataclass_params__
    return (
        params.init,
        params.repr,
        params.eq,
        params.order,
        params.unsafe_hash,
        params.frozen,
        getattr(cls, "__slots__", ()),
        tuple(
            (
                field.name,
                field.type,
                field.default,
                field.default_factory,
                field.init,
                field.repr,
                field.hash,
                field.compare,
                field.kw_only,
            )
            for field in dataclasses.fields(cls)
        ),
    )


def _entry_values(entry: Any) -> dict[str, Any]:
    return {
        field.name: getattr(entry, field.name)
        for field in dataclasses.fields(registry_contracts.LocalAgentRun)
    }


def _registry_import_hits() -> dict[str, list[str]]:
    roots = [
        Path("backend/services/agent_runs"),
        Path("backend/services/langgraph_chat"),
        Path("backend/tests/services/agent_runs"),
        Path("backend/tests/langgraph_chat"),
        Path("backend/tests/routers/tasks"),
        Path("backend/tests/e2e"),
    ]
    hits: dict[str, list[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported_names: list[str] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                imports_registry = module == "backend.services.agent_runs.registry"
                imports_relative_registry = (
                    module == "registry"
                    and node.level == 1
                    and path.is_relative_to(Path("backend/services/agent_runs"))
                )
                if imports_registry or imports_relative_registry:
                    imported_names.extend(alias.name for alias in node.names)
            if imported_names:
                hits[path.as_posix()] = imported_names
    return hits


def test_registry_contract_constants_aliases_and_exports_are_canonical() -> None:
    assert set(_MOVED_SYMBOLS).issubset(registry_contracts.__all__)
    assert registry_contracts.AgentRunKey == tuple[int, int, str]
    assert registry_contracts.HandoffWaitStatus == Literal["ready", "inactive"]
    assert registry_contracts.ACTIVE_AGENT_RUN_STATUSES == frozenset(
        {"queued", "running", "waiting_for_approval"}
    )
    assert registry_contracts.TERMINAL_AGENT_RUN_STATUSES == frozenset(
        {"completed", "failed", "cancelled"}
    )
    assert registry_contracts.DEFAULT_FINISHED_RETENTION == timedelta(minutes=15)


def test_registry_contract_dataclass_shapes_and_signatures_are_stable() -> None:
    assert _dataclass_shape(registry_contracts.LocalAgentRun) == (
        True,
        True,
        True,
        False,
        False,
        True,
        _EXPECTED_LOCAL_AGENT_RUN_FIELDS,
        tuple(
            (
                field.name,
                field.type,
                field.default,
                field.default_factory,
                field.init,
                field.repr,
                field.hash,
                field.compare,
                field.kw_only,
            )
            for field in dataclasses.fields(registry_contracts.LocalAgentRun)
        ),
    )
    field_names = tuple(
        field.name for field in dataclasses.fields(registry_contracts.LocalAgentRun)
    )
    assert field_names == _EXPECTED_LOCAL_AGENT_RUN_FIELDS
    assert getattr(registry_contracts.ClaimedHandoffBatch, "__slots__", ()) == (
        "claim_id",
        "tenant_id",
        "task_id",
        "agent_run_ids",
        "results",
        "active_runs",
    )
    assert getattr(registry_contracts.AgentRunTransition, "__slots__", ()) == (
        "entry",
        "changed",
    )
    assert str(inspect.signature(registry_contracts.LocalAgentRun)).startswith(
        "(graph_thread_id:"
    )
    assert str(inspect.signature(registry_contracts.ClaimedHandoffBatch)).startswith(
        "(claim_id:"
    )
    assert str(inspect.signature(registry_contracts.AgentRunTransition)).startswith(
        "(entry:"
    )


def test_registry_contract_properties_and_constructors_are_observable() -> None:
    assignment = build_agent_assignment()
    result = build_agent_result(assignment)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    entry = registry_contracts.LocalAgentRun(
        graph_thread_id="child-thread-1",
        assignment=assignment,
        status="completed",
        lifecycle_version=3,
        created_at=now,
        started_at=now,
        completed_at=now,
        result=result,
        safe_error=None,
        task_handle=None,
        cancel_requested=False,
        result_consumed=False,
        result_claim_id=None,
        accounted_usage_record_count=2,
    )

    assert _entry_values(entry)["graph_thread_id"] == "child-thread-1"
    assert entry.agent_run_id == assignment.agent_run_id
    assert entry.agent_id == assignment.agent_id
    assert entry.tenant_id == assignment.tenant_id
    assert entry.task_id == assignment.task_id
    assert entry.conversation_id == assignment.conversation_id
    assert entry.parent_turn_id == assignment.parent_turn_id
    assert entry.agent_kind == assignment.agent_kind

    claim = registry_contracts.ClaimedHandoffBatch(
        claim_id="handoff-claim:7:42:1",
        tenant_id=7,
        task_id=42,
        agent_run_ids=("run-1",),
        results=(result,),
        active_runs=(entry,),
    )
    assert claim.agent_run_ids == ("run-1",)
    assert claim.results == (result,)
    assert claim.active_runs == (entry,)

    transition = registry_contracts.AgentRunTransition(entry=entry, changed=True)
    assert transition.entry is entry
    assert transition.changed is True


def test_registry_error_messages_attributes_and_constructors_are_stable() -> None:
    active = registry_contracts.ActiveAgentRunExistsError(
        tenant_id=7,
        task_id=42,
        active_agent_run_id="run-1",
    )
    assert str(active) == (
        "An active process-local subagent run already exists for "
        "tenant_id=7, task_id=42: run-1"
    )
    assert active.tenant_id == 7
    assert active.task_id == 42
    assert active.active_agent_run_id == "run-1"

    collision = registry_contracts.AgentRunIdentityCollisionError(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )
    assert str(collision) == (
        "Agent run identity collision for "
        "tenant_id=7, task_id=42, agent_run_id=run-1"
    )
    assert collision.tenant_id == 7
    assert collision.task_id == 42
    assert collision.agent_run_id == "run-1"

    assert str(registry_contracts.AgentRunNotFoundError("missing")) == "'missing'"
    assert str(registry_contracts.HandoffClaimNotFoundError("missing")) == "'missing'"
    assert "__init__" not in registry_contracts.AgentRunNotFoundError.__dict__
    assert "__init__" not in registry_contracts.HandoffClaimNotFoundError.__dict__


@pytest.mark.asyncio
async def test_registry_facade_returns_and_raises_canonical_contracts() -> None:
    run_registry = registry.ProcessLocalAgentRunRegistry()
    assignment = build_agent_assignment()

    entry = await run_registry.register(assignment, graph_thread_id="child-thread-1")
    assert type(entry) is registry_contracts.LocalAgentRun

    with pytest.raises(registry_contracts.AgentRunIdentityCollisionError):
        await run_registry.register(
            assignment.model_copy(update={"objective": "Different objective."}),
            graph_thread_id="child-thread-1",
        )

    with pytest.raises(registry_contracts.AgentRunNotFoundError):
        await run_registry.mark_running(
            tenant_id=7,
            task_id=42,
            agent_run_id="missing",
        )

    completed = await run_registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=build_agent_result(assignment),
    )
    assert type(completed) is registry_contracts.LocalAgentRun
    claim = await run_registry.claim_ready_handoffs(tenant_id=7, task_id=42)
    assert type(claim) is registry_contracts.ClaimedHandoffBatch

    with pytest.raises(registry_contracts.HandoffClaimNotFoundError):
        await run_registry.release_handoffs("missing-claim")


def test_registry_contracts_import_only_stdlib_and_shared_contracts() -> None:
    source_path = (
        Path(__file__).resolve().parents[3]
        / "services/agent_runs/registry_contracts.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add((node.level, node.module))

    assert imports == {
        (0, "__future__"),
        (0, "asyncio"),
        (0, "dataclasses"),
        (0, "datetime"),
        (0, "typing"),
        (1, "contracts"),
    }


def test_moved_symbols_are_not_reexported_from_registry_facade() -> None:
    assert registry.__all__ == ["ProcessLocalAgentRunRegistry"]
    for symbol in _MOVED_SYMBOLS:
        assert not hasattr(registry, symbol)


def test_all_callers_import_moved_symbols_from_registry_contracts() -> None:
    registry_hits = _registry_import_hits()
    moved_registry_hits = {
        path: sorted(set(names).intersection(_MOVED_SYMBOLS))
        for path, names in registry_hits.items()
        if set(names).intersection(_MOVED_SYMBOLS)
    }
    assert moved_registry_hits == {}
    assert registry_hits
    assert all(
        names == ["ProcessLocalAgentRunRegistry"] for names in registry_hits.values()
    )
