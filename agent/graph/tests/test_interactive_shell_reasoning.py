"""Interactive shell continuation tests for the shared coordinator.

These tests seed the public running-shell result shape produced by shell tools
and verify live continuation stays inside the execution-session coordinator
instead of falling back to generic planner polling.
"""

from __future__ import annotations

from typing import Any

import pytest

import agent.tools.shell  # noqa: F401 - register shell tools for planner specs.
from agent.graph.builders.simple_tool_builder import (
    build_simple_tool_graph,
)
from agent.graph.subgraphs.tool_execution_session import (
    _route_after_collection,
    begin_execution_session_state,
    build_tool_execution_session_subgraph,
    coordinate_shell_interaction,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution_runtime.planner_service import (
    build_planner_context,
)
from agent.graph.runtime_controls import set_execution_session_control
from agent.tool_runtime import ToolExecutionRequest
from runtime_shared.shell_session_contracts import (
    ShellInteractionBoundary,
    ShellProcessStatus,
    ShellSessionIdentity,
    ShellSessionLifecycleStatus,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_session_port import (
    override_shell_session_service_resolver,
)


def _running_interactive_state() -> InteractiveState:
    """Build current-turn control state for one yielded execution."""
    return InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Continue the command.",
            metadata={
                "turn_sequence": 7,
                "current_turn_runtime_controls": {
                    "turn_sequence": 7,
                    "unavailable_tools": [],
                    "active_execution": {
                        "originating_tool_id": "shell.utility",
                        "continuation_tool_id": "shell.write_stdin",
                        "process_status": "running",
                        "session_id": "shs_route_123",
                        "stdin_available": True,
                    },
                },
            },
        ),
        trace=TraceState(),
    )


def _add_shell_identity(metadata: dict[str, Any], *, task_id: int = 42) -> None:
    metadata.update(
        {
            "tenant_id": 1,
            "task_id": task_id,
            "execution_owner_id": f"main:task-{task_id}-turn-1",
            "runtime_placement_mode": "local",
            "workspace_id": f"task-{task_id}",
            "workspace_path": "/workspace",
        }
    )


def test_running_execution_remains_inside_execution_session() -> None:
    state = _running_interactive_state()

    assert _route_after_collection(state) == "shell_interaction"


def test_terminal_execution_leaves_execution_session() -> None:
    state = InteractiveState(
        facts=FactsState(task_id=42, message="Command completed.", metadata={}),
        trace=TraceState(),
    )

    assert _route_after_collection(state) == "terminal"


def test_simple_tool_graph_routes_only_terminal_session_result_to_ptr() -> None:
    graph = build_simple_tool_graph(build_only=True)

    assert (
        "tool_execution_session",
        "terminal_session_compressor",
    ) in graph.edges
    assert (
        "terminal_session_compressor",
        "tool_synthesizer",
    ) in graph.edges
    assert ("tool_synthesizer", "post_tool_reasoning") in graph.edges

    session_graph = build_tool_execution_session_subgraph(build_only=True)
    assert "tool_synthesizer" not in session_graph.nodes
    assert ("initialize", "collect_result") in session_graph.edges
    branch_specs = session_graph.branches["collect_result"].values()
    assert any(
        branch.ends
        == {
            "shell_interaction": "shell_interaction",
            "terminal": "__end__",
        }
        for branch in branch_specs
    )


@pytest.mark.asyncio
async def test_shell_coordinator_sends_non_empty_input_directly() -> None:
    public_session_id = "shs_main_continuation_123"
    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Continue the running shell command.",
            capability="deep_reasoning",
            current_goal="Observe the delayed shell output.",
            next_tool_hint=f"Continue shell session {public_session_id}.",
            metadata=_running_interactive_state().facts.metadata_copy(),
        ),
        trace=TraceState(observations=[f"shell.exec returned {public_session_id}"]),
    )
    metadata = interactive.facts.ensure_metadata()
    metadata["current_turn_runtime_controls"]["active_execution"][
        "session_id"
    ] = public_session_id
    _add_shell_identity(metadata)
    set_execution_session_control(
        metadata,
        turn_sequence=7,
        sequence_id="batch-start",
        originating_tool_id="shell.utility",
    )
    begin_execution_session_state(
        sequence_id="batch-start",
        originating_tool_id="shell.utility",
        originating_parameters={
            "command": "python3 -u interactive.py",
            "interactive": True,
        },
    )
    write_calls: list[tuple[ShellSessionIdentity, ShellWriteRequest]] = []

    class _ShellService:
        async def write_stdin(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWriteRequest,
        ) -> ShellSessionUpdate:
            write_calls.append((identity, request))
            return ShellSessionUpdate(
                success=True,
                status="success",
                process_status=ShellProcessStatus.RUNNING,
                session_status=ShellSessionLifecycleStatus.ACTIVE,
                interaction_boundary=ShellInteractionBoundary.OUTPUT_AVAILABLE,
                session_id=public_session_id,
                stdout="accepted\n",
                stderr="",
                exit_code=None,
                stdin_available=True,
                truncated=False,
                duration_ms=11,
            )

    with override_shell_session_service_resolver(lambda: _ShellService()):
        updated = await coordinate_shell_interaction(
            interactive,
            decide_fn=lambda **_kwargs: {"action": "send_input", "chars": "hello\n"},
        )

    updated_metadata = updated["facts"]["metadata"]
    row = updated_metadata["last_tool_result_compact_batch"]["results"][0]
    assert write_calls == [
        (
            ShellSessionIdentity(
                tenant_id=1,
                task_id=42,
                execution_owner_id="main:task-42-turn-1",
                runtime_placement_mode="local",
                workspace_id="task-42",
                workspace_path="/workspace",
                runner_id=None,
                execution_site_id=None,
            ),
                ShellWriteRequest(
                    session_id=public_session_id,
                    chars="hello\n",
                ),
        )
    ]
    assert "planner_plan" not in updated_metadata
    assert "tool_plan_prepared" not in updated_metadata
    assert row["tool_id"] == "shell.write_stdin"
    assert row["compact_tool_result"]["stdout"] == "accepted\n"


@pytest.mark.asyncio
async def test_wait_for_output_decision_creates_no_empty_write_call() -> None:
    """A runtime wait is internal and does not synthesize shell.write_stdin."""
    public_session_id = "shs_replan_continuation_789"
    interactive = _running_interactive_state()
    metadata = interactive.facts.ensure_metadata()
    metadata["current_turn_runtime_controls"]["active_execution"][
        "session_id"
    ] = public_session_id
    set_execution_session_control(
        metadata,
        turn_sequence=7,
        sequence_id="batch-start",
        originating_tool_id="shell.utility",
    )
    begin_execution_session_state(
        sequence_id="batch-start",
        originating_tool_id="shell.utility",
        originating_parameters={"command": "python3 -u delayed.py"},
    )

    updated = await coordinate_shell_interaction(
        interactive,
        decide_fn=lambda **_kwargs: {"action": "wait_for_output"},
        wait_fn=lambda **_kwargs: ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.COMPLETED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            session_id=public_session_id,
            stdout="done\n",
            stderr="",
            exit_code=0,
            stdin_available=False,
            truncated=False,
            duration_ms=11,
        ),
    )

    updated_metadata = updated["facts"]["metadata"]
    assert "planner_plan" not in updated_metadata
    row = updated_metadata["last_tool_result_compact_batch"]["results"][0]
    assert row["tool_id"] == "shell.utility"
    assert row["compact_tool_result"]["stdout"] == "done\n"


def test_planner_context_constrains_transient_running_session_to_continuation() -> None:
    public_session_id = "shs_transient_continuation_456"
    metadata = {
        "turn_sequence": 9,
        "current_turn_runtime_controls": {
            "turn_sequence": 9,
            "unavailable_tools": [],
            "active_execution": {
                "originating_tool_id": "shell.utility",
                "continuation_tool_id": "shell.write_stdin",
                "process_status": "running",
                "session_id": public_session_id,
                "stdin_available": True,
            },
        },
    }
    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Continue the command.",
            current_goal="Wait for completion.",
            metadata=metadata,
        ),
        trace=TraceState(),
    )
    request = ToolExecutionRequest(
        capability="deep_reasoning",
        targets=[],
        message="Continue the command.",
        task_id=42,
        metadata=interactive.facts.metadata_copy(),
        workspace_path="/workspace",
    )

    context = build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=lambda _categories, _config: [
            "shell.utility",
            "shell.write_stdin",
        ],
        get_full_tool_catalog_for_planner=lambda _config: [
            "shell.utility",
            "shell.write_stdin",
        ],
        working_memory_summary_max_chars=2000,
    )

    assert context["resolved_tools"] == ["shell.write_stdin"]
    assert context["runtime_continuation_tool"] == "shell.write_stdin"
    assert context["tool_intent"]["target"] == public_session_id
    assert public_session_id in context["next_tool_hint"]
