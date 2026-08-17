"""Verify shell stdin stays process-local across planner checkpoint boundaries."""

from __future__ import annotations

import json

import pytest

from agent.config import AgentConfig
from agent.execution_strategy import ExecutionStrategy
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution_runtime.per_call_execution import (
    _materialize_process_local_shell_input,
)
from agent.graph.subgraphs.tool_execution_runtime.planner_service import (
    _redact_shell_stdin_in_plan,
    _serialize_tool_batch,
    ensure_action_plan,
)
from agent.graph.subgraphs.tool_execution_session_state import (
    SHELL_STDIN_REDACTED_MARKER,
    abort_execution_session_state,
    begin_execution_session_state,
    read_shell_input,
    remember_shell_input,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.models import ActionPlan, ActionType
from agent.tool_runtime import ToolExecutionRequest


def _execution_metadata(sequence_id: str) -> dict[str, object]:
    return {
        "turn_sequence": 7,
        "current_turn_runtime_controls": {
            "turn_sequence": 7,
            "unavailable_tools": [],
            "execution_session": {
                "sequence_id": sequence_id,
                "originating_tool_id": "shell.utility",
            },
        },
    }


def test_planner_serialization_keeps_exact_shell_stdin_process_local() -> None:
    sequence_id = "stdin-checkpoint-sequence"
    call_id = "stdin-checkpoint-call"
    secret = "PocSecret-stdin-checkpoint-9f4c2a\n"
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "read -s password"},
    )
    try:
        serialized = _serialize_tool_batch(
            ToolBatch(
                tool_batch_id="stdin-checkpoint-batch",
                tool_calls=(
                    ToolCall(
                        tool_call_id=call_id,
                        tool_id="shell.write_stdin",
                        parameters={"session_id": "shs_public_123", "chars": secret},
                    ),
                ),
                requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
            ),
            execution_sequence_id=sequence_id,
        )

        assert secret not in json.dumps(serialized)
        assert (
            serialized["tool_calls"][0]["parameters"]["chars"]
            == SHELL_STDIN_REDACTED_MARKER
        )
        assert read_shell_input(sequence_id=sequence_id, call_id=call_id) == secret
    finally:
        abort_execution_session_state(sequence_id)


@pytest.mark.asyncio
async def test_ensure_action_plan_checkpoints_only_redacted_shell_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id = "stdin-planner-sequence"
    call_id = "stdin-planner-call"
    secret = "PocSecret-planner-state-81fb32\n"
    metadata = _execution_metadata(sequence_id)
    interactive = InteractiveState(
        facts=FactsState(task_id=42, message="Continue.", metadata=metadata),
        trace=TraceState(),
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="Continue.",
        task_id=42,
        metadata=metadata,
    )
    batch = ToolBatch(
        tool_batch_id="stdin-planner-batch",
        tool_calls=(
            ToolCall(
                tool_call_id=call_id,
                tool_id="shell.write_stdin",
                parameters={"session_id": "shs_public_plan", "chars": secret},
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )

    class _Planner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def build_action_plan(
            self,
            _action: object,
            _context: object,
        ) -> ActionPlan:
            return ActionPlan(
                type=ActionType.GATHER_INFO,
                target="shs_public_plan",
                selected_tools=["shell.write_stdin"],
                candidate_tools=["shell.write_stdin"],
                tool_parameters={"shell.write_stdin": dict(batch.tool_calls[0].parameters)},
                execution_strategy=ExecutionStrategy.SEQUENTIAL,
                reasoning="Provide the requested credential.",
                expected_outcome="The running process continues.",
                tool_batch=batch,
            )

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.planner_service.EnhancedActionPlanner",
        _Planner,
    )
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "read -s password"},
    )
    try:
        await ensure_action_plan(
            interactive,
            request,
            AgentConfig(),
            build_action_for_planner=lambda *_args: object(),
            build_planner_context=lambda *_args: {"context": "safe"},
        )

        assert secret not in json.dumps(interactive.facts.metadata)
        assert secret not in json.dumps(request.metadata)
        assert (
            interactive.facts.metadata["planner_plan"]["tool_batch"]["tool_calls"][0][
                "parameters"
            ]["chars"]
            == SHELL_STDIN_REDACTED_MARKER
        )
        assert read_shell_input(sequence_id=sequence_id, call_id=call_id) == secret
    finally:
        abort_execution_session_state(sequence_id)


def test_dispatch_materializes_shell_stdin_without_mutating_checkpoint_metadata() -> None:
    sequence_id = "stdin-dispatch-sequence"
    call_id = "stdin-dispatch-call"
    secret = "PocSecret-stdin-dispatch-27ab81\n"
    metadata = _execution_metadata(sequence_id)
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "read -s password"},
    )
    try:
        remember_shell_input(sequence_id=sequence_id, call_id=call_id, chars=secret)
        checkpoint_call = ToolCall(
            tool_call_id=call_id,
            tool_id="shell.write_stdin",
            parameters={
                "session_id": "shs_public_456",
                "chars": SHELL_STDIN_REDACTED_MARKER,
            },
        )

        dispatch_call = _materialize_process_local_shell_input(
            checkpoint_call,
            metadata=metadata,
        )

        assert dispatch_call is not None
        assert dispatch_call.parameters["chars"] == secret
        assert checkpoint_call.parameters["chars"] == SHELL_STDIN_REDACTED_MARKER
        assert secret not in json.dumps(metadata)
    finally:
        abort_execution_session_state(sequence_id)


def test_legacy_raw_planner_plan_is_migrated_before_reuse() -> None:
    sequence_id = "stdin-legacy-sequence"
    call_id = "stdin-legacy-call"
    secret = "PocSecret-legacy-checkpoint-a7e012\n"
    legacy_plan = {
        "reasoning": "Continue the existing session.",
        "tool_batch": {
            "tool_batch_id": "stdin-legacy-batch",
            "requested_execution_strategy": "sequential",
            "tool_calls": [
                {
                    "tool_call_id": call_id,
                    "tool_id": "shell.write_stdin",
                    "parameters": {
                        "session_id": "shs_public_legacy",
                        "chars": secret,
                    },
                }
            ],
        },
    }
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "read -s password"},
    )
    try:
        migrated = _redact_shell_stdin_in_plan(
            legacy_plan,
            execution_sequence_id=sequence_id,
        )

        assert secret not in json.dumps(migrated)
        assert legacy_plan["tool_batch"]["tool_calls"][0]["parameters"]["chars"] == secret
        assert read_shell_input(sequence_id=sequence_id, call_id=call_id) == secret
    finally:
        abort_execution_session_state(sequence_id)


def test_dispatch_fails_closed_when_process_local_shell_stdin_is_missing() -> None:
    checkpoint_call = ToolCall(
        tool_call_id="missing-stdin-call",
        tool_id="shell.write_stdin",
        parameters={
            "session_id": "shs_public_789",
            "chars": SHELL_STDIN_REDACTED_MARKER,
        },
    )

    assert (
        _materialize_process_local_shell_input(
            checkpoint_call,
            metadata=_execution_metadata("lost-process-local-sequence"),
        )
        is None
    )
