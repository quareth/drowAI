"""Tests for the process-local subagent asyncio launcher."""

from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.subagents.registry import get_subagent_registry
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    build_agent_run_completion,
)
from backend.services.agent_runs.launcher import (
    AgentRunLauncher,
    SubagentRunCancelled,
    SubagentRunFailed,
    SubagentRunPaused,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.registry_contracts import (
    LocalAgentRun,
)
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
    build_runtime_identity,
)


def _runtime_identity(*, tenant_id: int = 7, task_id: int = 42) -> AgentRuntimeIdentity:
    return build_runtime_identity(tenant_id=tenant_id, task_id=task_id)


def _assignment(
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    agent_run_id: str = "run-1",
) -> AgentAssignment:
    return build_agent_assignment(
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        runtime_identity=_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )


def _result(agent_run_id: str = "run-1") -> AgentResult:
    return build_agent_result(
        _assignment(agent_run_id=agent_run_id),
        summary="Pathfinder found exposed HTTP.",
        recommended_next_steps=["Review HTTP headers"],
    )


def _completion(
    assignment: AgentAssignment,
    *,
    graph_thread_id: str = "child-thread-1",
) -> AgentRunCompletion:
    return build_agent_run_completion(
        result=_result(assignment.agent_run_id),
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        final_state={
            "trace": {
                "usage_records": [
                    {
                        "source": "subagent_runtime_model",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                ]
            }
        },
    )


@pytest.mark.asyncio
async def test_awaiting_launch_task_guarantees_completed_registry_state() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    release_worker = asyncio.Event()

    async def _worker(**kwargs: Any) -> AgentRunCompletion:
        assert kwargs["assignment"] == assignment
        assert kwargs["graph_thread_id"] == "child-thread-1"
        assert await kwargs["is_cancel_requested"]() is False
        await release_worker.wait()
        return _completion(assignment)

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )
    attached = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert attached is not None
    assert attached.task_handle is task
    assert task.done() is False

    release_worker.set()
    completion = await task
    assert completion.result == _result("run-1")
    assert completion.usage_records[0]["agent_run_id"] == "run-1"
    completed = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == _result("run-1")
    assert completed.task_handle is None


@pytest.mark.asyncio
async def test_terminal_completion_publishes_attributed_lifecycle_event() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        return _completion(assignment)

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        lifecycle_publisher=_publish,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
        parent_run_id="parent-run-1",
    )

    assert (await task).result == _result("run-1")
    completed = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert completed is not None
    assert completed.status == "completed"
    assert len(events) == 1
    task_id, event = events[0]
    metadata = event["metadata"]
    assert task_id == 42
    assert event["agent_run"]["status"] == "completed"
    assert metadata["producer_type"] == "subagent"
    assert metadata["agent_run_id"] == "run-1"
    assert metadata["agent_kind"] == "recon"
    assert metadata["agent_display_name"] == "Pathfinder"
    assert metadata["parent_turn_id"] == "turn-1"
    assert metadata["parent_run_id"] == "parent-run-1"
    assert metadata["lifecycle_version"] == 2
    assert "assignment" not in event["agent_run"] or event["agent_run"]["assignment"] is None


@pytest.mark.asyncio
async def test_lifecycle_publication_failure_preserves_settled_worker_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        raise RuntimeError("stream unavailable")

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        return _completion(assignment)

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        lifecycle_publisher=_publish,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )

    completion = await task
    terminal = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert completion.result == _result("run-1")
    assert terminal is not None
    assert terminal.status == "completed"
    assert terminal.task_handle is None


@pytest.mark.asyncio
async def test_worker_failure_is_sanitized_and_contained() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")

    worker_error = RuntimeError("secret token=abc123")

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        raise worker_error

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )

    with pytest.raises(RuntimeError) as raised:
        await task
    failed = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert failed is not None
    assert failed.status == "failed"
    assert raised.value is worker_error
    assert failed.safe_error == "Subagent worker failed"
    assert "abc123" not in failed.safe_error
    assert failed.task_handle is None


@pytest.mark.asyncio
async def test_lifecycle_task_cannot_overwrite_richer_terminal_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-1")
    release_worker = asyncio.Event()
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        await release_worker.wait()
        raise RuntimeError("later worker failure")

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        lifecycle_publisher=_publish,
    )
    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
        parent_run_id="parent-run-1",
    )
    richer_result = _result("run-1").model_copy(
        update={"summary": "Specific terminal handoff from child graph."}
    )
    completed = await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=richer_result,
    )

    release_worker.set()
    with pytest.raises(RuntimeError):
        await task

    terminal = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")
    assert terminal == completed
    assert terminal is not None
    assert terminal.lifecycle_version == completed.lifecycle_version
    assert terminal.result == richer_result
    assert terminal.safe_error is None
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_error", "expected_status", "expected_safe_error"),
    [
        (SubagentRunCancelled(object()), "cancelled", None),
        (
            SubagentRunFailed("secret token=abc123", object()),
            "failed",
            "Subagent worker failed",
        ),
    ],
)
async def test_specialized_worker_errors_use_fallback_terminalization(
    worker_error: BaseException,
    expected_status: str,
    expected_safe_error: str | None,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        raise worker_error

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        lifecycle_publisher=_publish,
    )
    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
        parent_run_id="parent-run-1",
    )

    with pytest.raises(type(worker_error)) as raised:
        await task
    terminal = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert terminal is not None
    assert terminal.status == expected_status
    assert raised.value is worker_error
    assert terminal.safe_error == expected_safe_error
    assert terminal.task_handle is None
    assert len(events) == 1
    assert events[0][0] == 42
    assert events[0][1]["agent_run"]["status"] == expected_status
    assert events[0][1]["metadata"]["parent_run_id"] == "parent-run-1"


@pytest.mark.asyncio
async def test_cancellation_signal_is_scoped_to_exact_local_run() -> None:
    registry = ProcessLocalAgentRunRegistry()
    first = _assignment(tenant_id=7, task_id=42, agent_run_id="run-1")
    second = _assignment(tenant_id=8, task_id=42, agent_run_id="run-2")
    await registry.register(first, graph_thread_id="child-thread-1")
    await registry.register(second, graph_thread_id="child-thread-2")

    async def _worker(**kwargs: Any) -> AgentRunCompletion:
        while not await kwargs["is_cancel_requested"]():
            await asyncio.sleep(0.01)
        await asyncio.sleep(60)
        return _completion(kwargs["assignment"], graph_thread_id=kwargs["graph_thread_id"])

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
    )
    first_task = await launcher.launch(
        assignment=first,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )
    second_task = await launcher.launch(
        assignment=second,
        runtime_config=object(),
        graph_thread_id="child-thread-2",
    )

    cancelled_request = await launcher.request_cancellation(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )

    assert cancelled_request.cancel_requested is True
    assert first_task.cancelling() > 0
    assert second_task.done() is False

    with contextlib.suppress(asyncio.CancelledError):
        await first_task
    cancelled = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")
    other = await registry.get(tenant_id=8, task_id=42, agent_run_id="run-2")

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.task_handle is None
    assert other is not None
    assert other.cancel_requested is False
    assert other.task_handle is second_task

    await launcher.request_cancellation(tenant_id=8, task_id=42, agent_run_id="run-2")
    with contextlib.suppress(asyncio.CancelledError):
        await second_task
    second_cancelled = await registry.get(
        tenant_id=8, task_id=42, agent_run_id="run-2"
    )
    assert second_cancelled is not None
    assert second_cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_paused_approval_cancellation_becomes_terminal_and_publishes() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        raise SubagentRunPaused(
            execution_result=SimpleNamespace(
                final_state={
                    "trace": {
                        "usage_records": [
                            {
                                "source": "subagent_runtime_model",
                                "prompt_tokens": 10,
                                "completion_tokens": 5,
                                "total_tokens": 15,
                            }
                        ]
                    }
                }
            )
        )

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        lifecycle_publisher=_publish,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )
    with pytest.raises(SubagentRunPaused):
        await task
    waiting = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert waiting is not None
    assert waiting.status == "waiting_for_approval"
    assert task.done() is True
    assert waiting.task_handle is task
    assert waiting.accounted_usage_record_count == 1

    cancelled = await launcher.request_cancellation(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is True
    assert cancelled.task_handle is None
    assert [event["agent_run"]["status"] for _task_id, event in events] == [
        "waiting_for_approval",
        "cancelled",
    ]
    assert events[-1][0] == 42
    assert events[-1][1]["metadata"]["agent_run_id"] == "run-1"
    assert events[-1][1]["metadata"]["lifecycle_version"] == cancelled.lifecycle_version


@pytest.mark.asyncio
async def test_create_task_failure_does_not_attach_local_handle() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        return _completion(assignment)

    def _failing_task_factory(_coro: Any) -> asyncio.Task[AgentRunCompletion]:
        raise RuntimeError("create task failed")

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        task_factory=_failing_task_factory,
    )

    with pytest.raises(RuntimeError):
        await launcher.launch(
            assignment=assignment,
            runtime_config=object(),
            graph_thread_id="child-thread-1",
        )
    entry = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert entry is not None
    assert entry.status == "queued"
    assert entry.task_handle is None


@pytest.mark.asyncio
async def test_attach_failure_cancels_and_settles_created_task() -> None:
    class _FailingAttachRegistry(ProcessLocalAgentRunRegistry):
        async def attach_task_handle(self, **_kwargs: Any) -> LocalAgentRun:
            raise RuntimeError("attach failed")

    registry = _FailingAttachRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    created_tasks: list[asyncio.Task[AgentRunCompletion]] = []
    worker_started = False

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        nonlocal worker_started
        worker_started = True
        return _completion(assignment)

    def _task_factory(
        coro: Any,
    ) -> asyncio.Task[AgentRunCompletion]:
        task = asyncio.create_task(coro)
        created_tasks.append(task)
        return task

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
        task_factory=_task_factory,
    )

    with pytest.raises(RuntimeError, match="attach failed"):
        await launcher.launch(
            assignment=assignment,
            runtime_config=object(),
            graph_thread_id="child-thread-1",
        )

    assert len(created_tasks) == 1
    assert created_tasks[0].cancelled() is True
    assert worker_started is False


@pytest.mark.asyncio
async def test_cancellation_before_worker_start_settles_before_task_await_returns() -> None:
    class _CancelOnAttachRegistry(ProcessLocalAgentRunRegistry):
        async def attach_task_handle(
            self,
            *,
            task_handle: asyncio.Task[Any],
            **kwargs: Any,
        ) -> LocalAgentRun:
            entry = await super().attach_task_handle(
                task_handle=task_handle,
                **kwargs,
            )
            task_handle.cancel()
            return entry

    registry = _CancelOnAttachRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    worker_started = False

    async def _worker(**_kwargs: Any) -> AgentRunCompletion:
        nonlocal worker_started
        worker_started = True
        return _completion(assignment)

    launcher = AgentRunLauncher(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        worker=_worker,
    )
    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    terminal = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert worker_started is False
    assert terminal is not None
    assert terminal.status == "cancelled"
    assert terminal.task_handle is None


def test_launcher_module_has_no_durable_or_route_boundary_dependencies() -> None:
    source_path = Path(__file__).resolve().parents[3] / "services/agent_runs/launcher.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    prohibited_parts = {"database", "durable", "lease", "poll", "polling", "scheduler"}
    assert all(
        module != "sqlalchemy"
        and not module.startswith("sqlalchemy.")
        and prohibited_parts.isdisjoint(module.lower().split("."))
        for module in imported_modules
    )
