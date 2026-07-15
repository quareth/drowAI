"""Regression tests for simple-tool HITL plan preparation behavior.

These tests ensure the simple-tool graph can prepare planner output before
approval interrupts and that execution reuses prepared plan state.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution import (
    _TOOL_CALL_ID_KEY,
    _TOOL_DISPATCH_CACHE_KEY,
    approval_gate_node,
    dispatch_tool_execution_node,
    prepare_tool_execution_plan,
    run_tool_execution,
)


def _base_state(*, prepared: bool = False) -> dict:
    planner_plan = {
        "selected_tools": ["shell.exec"],
        "tool_parameters": {"shell.exec": {"command": "echo ok"}},
        "execution_strategy": "sequential",
    }
    metadata = {
        "agent_mode": "agent",
        "planner_plan": planner_plan,
        "graph_runtime_context": {
            "task_id": 1,
            "tenant_id": 1,
            "runtime_placement_mode": "local",
            "workspace_id": "task-1",
            "actor_type": "system",
            "actor_id": "langgraph",
            "workspace_path": "/tmp",
        },
        # Phase 5 cutover: the hot-path ConversationContextBundle is
        # required by the tool-execution request-context builder.
        METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
            conversation_id="conv-st-hitl",
            turn_id="turn-st-hitl",
            turn_sequence=0,
            messages=[],
        ),
    }
    if prepared:
        metadata["tool_plan_prepared"] = True

    facts = FactsState(
        task_id=1,
        message="run echo",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo ok"}},
        metadata=metadata,
    )
    trace = TraceState()
    return InteractiveState(facts=facts, trace=trace).as_graph_state()


def _fake_outcome() -> SimpleNamespace:
    def _to_graph_metadata() -> dict:
        return {"tool_id": "shell.exec", "result": {"success": True}}

    return SimpleNamespace(
        tool_id="shell.exec",
        parameters={"command": "echo ok"},
        duration=1.0,
        result={
            "success": True,
            "status": "success",
            "stdout": "ok",
            "stdout_excerpt": "ok",
            "stderr": "",
            "stderr_excerpt": "",
            "observation": "ok",
            "duration": 1,
            "exit_code": 0,
        },
        catalog=[],
        reasoning=["Executed shell command"],
        summary="ok",
        to_graph_metadata=_to_graph_metadata,
    )


def _fake_validation_outcome() -> SimpleNamespace:
    def _to_graph_metadata() -> dict:
        return {
            "tool_id": "shell.exec",
            "result": {
                "success": False,
                "status": "validation_error",
                "validation_errors": [{"field": "command", "error": "Field required"}],
            },
        }

    return SimpleNamespace(
        tool_id="shell.exec",
        parameters={"target": "127.0.0.1"},
        duration=0.01,
        result={
            "success": False,
            "status": "validation_error",
            "stdout": "",
            "stdout_excerpt": "",
            "stderr": "Validation error: command: Field required",
            "stderr_excerpt": "Validation error: command: Field required",
            "observation": "Planner produced invalid parameters for shell.exec",
            "duration": 0.01,
            "exit_code": -1,
            "validation_errors": [{"field": "command", "error": "Field required"}],
        },
        catalog=[],
        reasoning=["Planner validation error"],
        summary="validation failed",
        to_graph_metadata=_to_graph_metadata,
    )


def _checkpoint_retry_config() -> dict:
    return {
        "configurable": {
            "thread_id": "task-1",
            "graph_name": "simple_tool",
            "retry_attempt": 1,
            "retry_max_attempts": 2,
            "previous_failure": {
                "error_code": "tool_argument_invalid",
                "failure_stage": "tool_execution",
                "tool_name": "shell.exec",
                "tool_call_id": "call-old",
                "summary": "missing required command argument",
                "raw_provider_payload": "Bearer sk-LEAK-ME",
            },
        }
    }


@pytest.mark.asyncio
async def test_prepare_tool_execution_plan_sets_prepared_flag() -> None:
    state = _base_state(prepared=False)
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock:
        updated = await prepare_tool_execution_plan(state)

    ensure_mock.assert_awaited_once()
    interactive = InteractiveState.from_mapping(updated)
    assert interactive.facts.metadata.get("tool_plan_prepared") is True


@pytest.mark.asyncio
async def test_prepare_tool_execution_plan_retry_context_invalidates_stale_plan() -> None:
    state = _base_state(prepared=True)

    async def _record_replan(interactive, request, _config) -> None:
        metadata = request.metadata or {}
        assert "planner_plan" not in metadata
        assert "planner_context_snapshot" not in metadata
        assert "plan_context" not in metadata
        assert "tool_plan_prepared" not in metadata
        assert "shell.exec" not in (interactive.facts.tool_parameters or {})

        retry_context = metadata.get("checkpoint_retry_context")
        assert retry_context["retry_attempt"] == 1
        assert retry_context["retry_max_attempts"] == 2
        assert retry_context["previous_failure"]["tool_name"] == "shell.exec"
        assert "Bearer sk-LEAK-ME" not in repr(retry_context)

        retry_hint = metadata.get("next_tool_hint")
        assert "Checkpoint retry" in retry_hint
        assert "missing required command argument" in retry_hint
        assert "do not repeat the same failing call unchanged" in retry_hint
        assert "Bearer sk-LEAK-ME" not in retry_hint

        metadata["planner_plan"] = {
            "selected_tools": ["shell.exec"],
            "tool_parameters": {"shell.exec": {"command": "echo corrected"}},
            "execution_strategy": "sequential",
        }
        interactive.facts.selected_tool = "shell.exec"
        interactive.facts.tool_parameters = {
            "shell.exec": {"command": "echo corrected"}
        }
        interactive.facts.metadata = metadata

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
        side_effect=_record_replan,
    ) as ensure_mock:
        updated = await prepare_tool_execution_plan(
            state,
            config=_checkpoint_retry_config(),
        )

    ensure_mock.assert_awaited_once()
    interactive = InteractiveState.from_mapping(updated)
    metadata = interactive.facts.metadata or {}
    assert metadata.get("tool_plan_prepared") is True
    assert metadata["planner_plan"]["tool_parameters"]["shell.exec"] == {
        "command": "echo corrected"
    }


@pytest.mark.asyncio
async def test_prepare_tool_execution_plan_preserves_existing_tool_call_id_before_dispatch() -> None:
    state = _base_state(prepared=False)
    interactive = InteractiveState.from_mapping(state)
    metadata = dict(interactive.facts.metadata or {})
    metadata[_TOOL_CALL_ID_KEY] = "tc-previous-cycle"
    interactive.facts.metadata = metadata

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ):
        updated = await prepare_tool_execution_plan(interactive.as_graph_state())

    rotated = InteractiveState.from_mapping(updated)
    tool_call_id = (rotated.facts.metadata or {}).get(_TOOL_CALL_ID_KEY)
    assert tool_call_id == "tc-previous-cycle"


@pytest.mark.asyncio
async def test_run_tool_execution_skips_replanning_when_prepared() -> None:
    state = _base_state(prepared=True)
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=False,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ):
        updated = await run_tool_execution(state)

    ensure_mock.assert_not_awaited()
    interactive = InteractiveState.from_mapping(updated)
    assert "tool_plan_prepared" not in (interactive.facts.metadata or {})


@pytest.mark.asyncio
async def test_run_tool_execution_retry_context_replans_before_dispatch() -> None:
    state = _base_state(prepared=True)

    async def _record_replan(interactive, request, _config) -> None:
        metadata = request.metadata or {}
        assert "planner_plan" not in metadata
        assert "tool_plan_prepared" not in metadata
        metadata["planner_plan"] = {
            "selected_tools": ["shell.exec"],
            "tool_parameters": {"shell.exec": {"command": "echo corrected"}},
            "execution_strategy": "sequential",
        }
        interactive.facts.selected_tool = "shell.exec"
        interactive.facts.tool_parameters = {
            "shell.exec": {"command": "echo corrected"}
        }
        interactive.facts.metadata = metadata

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
        side_effect=_record_replan,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=False,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ):
        updated = await run_tool_execution(
            state,
            config=_checkpoint_retry_config(),
        )

    ensure_mock.assert_awaited_once()
    interactive = InteractiveState.from_mapping(updated)
    metadata = interactive.facts.metadata or {}
    assert "tool_plan_prepared" not in metadata
    assert metadata.get("checkpoint_retry_context", {}).get("retry_attempt") == 1


@pytest.mark.asyncio
async def test_run_tool_execution_keeps_prepared_flag_until_approval_interrupt() -> None:
    state = _base_state(prepared=True)

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        return_value={"action": "approve"},
    ) as approval_mock, patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ):
        updated = await run_tool_execution(state)

    ensure_mock.assert_not_awaited()
    approval_metadata = approval_mock.call_args.kwargs.get("metadata", {})
    assert approval_metadata.get("tool_plan_prepared") is True
    interactive = InteractiveState.from_mapping(updated)
    assert "tool_plan_prepared" not in (interactive.facts.metadata or {})


@pytest.mark.asyncio
async def test_run_tool_execution_validation_error_continues_to_compact_metadata() -> None:
    state = _base_state(prepared=True)
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=False,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_validation_outcome(),
    ):
        updated = await run_tool_execution(state)

    ensure_mock.assert_not_awaited()
    interactive = InteractiveState.from_mapping(updated)
    compact = (interactive.facts.metadata or {}).get("last_tool_result_compact", {})
    assert compact.get("status") == "validation_error"
    assert (interactive.facts.metadata or {}).get("tool_history")


@pytest.mark.asyncio
async def test_approval_gate_node_has_no_planner_side_effects() -> None:
    state = _base_state(prepared=True)
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        return_value={"action": "approve"},
    ) as approval_mock:
        updated = await approval_gate_node(state)

    ensure_mock.assert_not_awaited()
    approval_mock.assert_called_once()
    interactive = InteractiveState.from_mapping(updated)
    metadata = interactive.facts.metadata or {}
    assert metadata.get("tool_approval_gate_completed") is True
    assert metadata.get("tool_approval_response", {}).get("action") == "approve"


@pytest.mark.asyncio
async def test_dispatch_node_uses_prepared_data_without_reapproval() -> None:
    state = _base_state(prepared=True)
    interactive = InteractiveState.from_mapping(state)
    metadata = dict(interactive.facts.metadata or {})
    metadata["tool_approval_gate_completed"] = True
    metadata["tool_approval_response"] = {"action": "approve"}
    interactive.facts.metadata = metadata
    prepared_state = interactive.as_graph_state()

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        side_effect=AssertionError("dispatch should not request approval"),
    ), patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ):
        updated = await dispatch_tool_execution_node(prepared_state)

    ensure_mock.assert_not_awaited()
    updated_interactive = InteractiveState.from_mapping(updated)
    updated_metadata = updated_interactive.facts.metadata or {}
    assert "tool_approval_gate_completed" not in updated_metadata
    assert "tool_approval_response" not in updated_metadata


@pytest.mark.asyncio
async def test_dispatch_duplicate_resume_reexecutes_with_fresh_call_identity() -> None:
    """Duplicate dispatch attempts re-execute when call identity is reminted."""
    state = _base_state(prepared=True)
    interactive = InteractiveState.from_mapping(state)
    metadata = dict(interactive.facts.metadata or {})
    metadata["tool_approval_gate_completed"] = True
    metadata["tool_approval_response"] = {"action": "approve"}
    metadata[_TOOL_CALL_ID_KEY] = "tc-idempotent-test"
    interactive.facts.metadata = metadata
    prepared_state = interactive.as_graph_state()

    run_mock = AsyncMock(return_value=_fake_outcome())
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        side_effect=AssertionError("should not request approval"),
    ), patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        side_effect=run_mock,
    ):
        first = await dispatch_tool_execution_node(prepared_state)

    run_mock.assert_awaited_once()
    first_interactive = InteractiveState.from_mapping(first)
    first_compact = first_interactive.facts.metadata.get("last_tool_result_compact", {})
    assert first_compact.get("tool") == "shell.exec"
    assert first_compact.get("success") is True

    # Second dispatch with same state (simulates duplicate resume)
    second_state = first_interactive.as_graph_state()
    run_mock.reset_mock()
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        side_effect=AssertionError("should not request approval"),
    ), patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        side_effect=run_mock,
    ):
        second = await dispatch_tool_execution_node(second_state)

    run_mock.assert_awaited_once()
    second_interactive = InteractiveState.from_mapping(second)
    second_compact = second_interactive.facts.metadata.get("last_tool_result_compact", {})
    assert second_compact.get("tool") == "shell.exec"
    assert second_compact.get("success") is True
    assert second_compact.get("summary") == first_compact.get("summary")


@pytest.mark.asyncio
async def test_run_tool_execution_ignores_stale_dispatch_cache_when_gate_not_completed() -> None:
    state = _base_state(prepared=True)
    interactive = InteractiveState.from_mapping(state)
    metadata = dict(interactive.facts.metadata or {})
    metadata[_TOOL_CALL_ID_KEY] = "tc-stale"
    metadata[_TOOL_DISPATCH_CACHE_KEY] = {
        "tc-stale": {
            "last_tool_result_compact": {"tool": "shell.exec", "summary": "stale-cached-summary"},
            "last_tool_result": {"tool": "shell.exec", "success": True},
            "tool_history_entry": {"tool_id": "shell.exec"},
            "reasoning_additions": ["stale cache"],
        }
    }
    # No approval gate markers -> this is a fresh dispatch cycle.
    interactive.facts.metadata = metadata

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=False,
    ), patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ) as run_mock:
        updated = await run_tool_execution(interactive.as_graph_state())

    ensure_mock.assert_not_awaited()
    run_mock.assert_awaited_once()
    updated_interactive = InteractiveState.from_mapping(updated)
    updated_metadata = updated_interactive.facts.metadata or {}
    assert updated_metadata.get("last_tool_result_compact", {}).get("summary") != "stale-cached-summary"


@pytest.mark.asyncio
async def test_run_tool_execution_emits_approval_to_tool_start_metric_when_resume_config() -> None:
    """Task 4.2: approval_to_tool_start_ms metric emitted when config has approval_received_at."""
    import time
    state = _base_state(prepared=True)
    interactive = InteractiveState.from_mapping(state)
    metadata = dict(interactive.facts.metadata or {})
    metadata["tool_approval_gate_completed"] = True
    metadata["tool_approval_response"] = {"action": "approve"}
    interactive.facts.metadata = metadata
    prepared_state = interactive.as_graph_state()

    approval_received_at = time.perf_counter() - 0.05  # 50ms ago
    config = {
        "configurable": {
            "approval_received_at": approval_received_at,
            "graph_name": "simple_tool",
        }
    }

    with patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.safe_gauge",
    ) as mock_gauge:
        await run_tool_execution(prepared_state, config=config)

    metric_names = [c[0][0] for c in mock_gauge.call_args_list]
    approval_calls = [c for c in mock_gauge.call_args_list if c[0][0] == "approval_to_tool_start_ms"]
    assert len(approval_calls) == 1
    assert approval_calls[0][0][1] >= 40  # ~50ms elapsed, allow some variance
    assert "approval_to_tool_start_ms_graph_simple_tool" in metric_names
    assert "approval_to_tool_start_ms_path_unknown" in metric_names
