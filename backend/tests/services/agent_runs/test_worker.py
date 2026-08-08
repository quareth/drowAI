"""Tests for the generic process-local subagent worker extraction."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from agent.graph.adapters.executor_adapter import GraphToolExecutor
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.graph.subgraphs.tool_execution_runtime.request_context import (
    build_request_and_coordinator_config,
)
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.registry import SubagentRegistry
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from agent.subagents.runtime.state import (
    apply_subagent_state_to_interactive,
    build_subagent_initial_state,
    subagent_state_from_graph_state,
)
from agent.graph.subgraphs.tool_execution_runtime.lane_dispatch import (
    ToolCallDispatchInput,
    dispatch_tool_call_by_lane,
)
from agent.models import ExecutionStrategy
from agent.tool_runtime import ToolExecutionCoordinator
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.launcher import SubagentRunFailed
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.worker import (
    ProcessLocalAgentRunWorker,
    extract_subagent_result_from_state,
    prepare_subagent_child_config,
    resolve_definition_for_assignment,
)
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
    build_runtime_identity,
)
from runtime_shared import shell_session_port
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_capabilities import ShellCapability


def _pathfinder_definition() -> SubagentDefinition:
    [definition] = [
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    ]
    return definition


def _runtime_identity() -> AgentRuntimeIdentity:
    return build_runtime_identity(
        user_id=3,
    )


def _assignment() -> AgentAssignment:
    return build_agent_assignment(
        objective="Map live hosts on the approved target.",
        suggested_capabilities=["host_discovery"],
        relevant_context={
            "ticket": "ENG-123",
            "turn_sequence": 4,
            "agent_mode": "full_access",
        },
        runtime_identity=_runtime_identity(),
    )


def _result() -> AgentResult:
    return build_agent_result(
        _assignment(),
        summary="Pathfinder found one live host.",
        key_findings=["10.0.0.10 responded to probes."],
        tools_used=["fping"],
        evidence_refs=[],
        recommended_next_steps=[],
        final_checkpoint_id=None,
    )


def _final_state() -> dict[str, Any]:
    return {
        "facts": {
            "metadata": {
                SUBAGENT_RESULT_METADATA_KEY: _result().model_dump(mode="json")
            }
        },
        "trace": {
            "usage_records": [
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            ]
        },
    }


def test_subagent_initial_graph_state_uses_agent_run_execution_owner() -> None:
    definition = _pathfinder_definition()
    assignment = _assignment()
    runtime_projection = assignment.runtime_identity.model_dump(mode="json")
    runtime_projection["execution_owner_id"] = "main:stale-parent-turn"
    stale_parent_context = {
        **runtime_projection,
        "graph_thread_id": "parent-thread",
        "execution_owner_id": "main:stale-parent-turn",
        "turn_id": "stale-parent-turn",
    }

    graph_input = build_subagent_initial_state(
        definition=definition,
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )
    config = prepare_subagent_child_config(
        {
            "configurable": {
                "runtime_projection": runtime_projection,
                "graph_runtime_context": stale_parent_context,
            }
        },
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )

    state_context = graph_input["facts"]["metadata"]["graph_runtime_context"]
    config_context = config["configurable"]["graph_runtime_context"]
    assert GraphRuntimeContext.model_validate(state_context).model_dump() == (
        GraphRuntimeContext.model_validate(config_context).model_dump()
    )
    assert state_context["execution_owner_id"] == "subagent:run-1"
    assert state_context["turn_id"] == assignment.parent_turn_id
    assert state_context["graph_thread_id"] == "child-thread-1"
    assert config_context["execution_owner_id"] != "main:stale-parent-turn"
    assert config_context["graph_thread_id"] != "parent-thread"
    assert "credential_ref" not in state_context


def test_resumed_subagent_state_reasserts_agent_run_execution_owner() -> None:
    definition = _pathfinder_definition()
    assignment = _assignment()
    graph_input = build_subagent_initial_state(
        definition=definition,
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )
    resumed = InteractiveState.from_mapping(graph_input)
    resumed.facts.metadata["graph_runtime_context"][
        "execution_owner_id"
    ] = "main:stale-parent-turn"

    subagent = subagent_state_from_graph_state(resumed, definition=definition)
    refreshed = apply_subagent_state_to_interactive(
        resumed,
        subagent,
        definition=definition,
    )

    graph_context = refreshed.facts.metadata["graph_runtime_context"]
    assert graph_context["execution_owner_id"] == "subagent:run-1"
    assert graph_context["execution_owner_id"] != "main:stale-parent-turn"


class _FakeCheckpointerService:
    def __init__(self) -> None:
        self.task_ids: list[int] = []

    @asynccontextmanager
    async def get_checkpointer(self, task_id: int) -> Any:
        self.task_ids.append(task_id)
        yield "checkpoint"


class _FakeExecutor:
    def __init__(self, result: GraphExecutionResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: Any,
        config: dict[str, Any],
        task_id: int,
        state_container: Any = None,
        should_cancel: Any = None,
    ) -> GraphExecutionResult:
        self.calls.append(
            {
                "compiled_graph": compiled_graph,
                "graph_input": graph_input,
                "config": config,
                "task_id": task_id,
                "state_container": state_container,
                "should_cancel": should_cancel,
                "cancelled": should_cancel(),
            }
        )
        return self.result


class _ShellDispatchingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.dispatched_tools: list[str] = []
        self.request_owner_id: str | None = None
        self.service_owner_id: str | None = None

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: Any,
        config: dict[str, Any],
        task_id: int,
        state_container: Any = None,
        should_cancel: Any = None,
    ) -> GraphExecutionResult:
        self.calls.append(
            {
                "compiled_graph": compiled_graph,
                "graph_input": graph_input,
                "config": config,
                "task_id": task_id,
                "state_container": state_container,
                "should_cancel": should_cancel,
            }
        )
        tool_candidates = graph_input["facts"]["tool_candidates"]
        assert "shell.utility" in tool_candidates
        assert "shell.assessment" in tool_candidates
        assert "shell.write_stdin" in tool_candidates
        context = config["configurable"]["graph_runtime_context"]
        state_context = graph_input["facts"]["metadata"]["graph_runtime_context"]
        assert state_context["execution_owner_id"] == context["execution_owner_id"]
        assert state_context["execution_owner_id"] == "subagent:run-1"
        await self._dispatch_real_shell_request(graph_input)

        async def _execute_session(_decision: Any, dispatch_input: Any) -> dict[str, Any]:
            self.dispatched_tools.append(dispatch_input.tool_id)
            return {
                "tool": dispatch_input.tool_id,
                "success": True,
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "status": "success",
                "metadata": {},
            }

        async def _unexpected_transport(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("shell tools must dispatch through session control")

        for tool_id, parameters in (
            ("shell.utility", {"command": "printf ready"}),
            ("shell.assessment", {"command": "printf assessment"}),
            (
                "shell.write_stdin",
                {"session_id": "shs_worker_path", "chars": ""},
            ),
        ):
            result = await dispatch_tool_call_by_lane(
                dispatch_input=ToolCallDispatchInput(
                    tool_id=tool_id,
                    normalized_parameters=parameters,
                    timeout_plan=None,
                    tool_call_id=f"call-{tool_id}",
                    tool_batch_id="batch-worker-shell",
                    runtime_placement_mode=context["runtime_placement_mode"],
                    tenant_id=context["tenant_id"],
                    task_id=context["task_id"],
                    execution_owner_id=context["execution_owner_id"],
                    runtime_metadata={"workspace_id": context["workspace_id"]},
                ),
                execute_local=_unexpected_transport,
                execute_runner=_unexpected_transport,
                execute_session=_execute_session,
            )
            assert result["success"] is True
            assert result["metadata"]["route_policy"]["selected_authority"] == (
                "runtime_session_control"
            )

        return GraphExecutionResult(final_state=_final_state())

    async def _dispatch_real_shell_request(self, graph_input: dict[str, Any]) -> None:
        interactive = InteractiveState.from_mapping(graph_input)
        metadata = dict(interactive.facts.metadata)
        request, coordinator_config, _runtime_context, _workspace_path = (
            build_request_and_coordinator_config(
                interactive=interactive,
                context=None,
                metadata=metadata,
            )
        )
        self.request_owner_id = request.metadata["execution_owner_id"]

        service = _FakeShellSessionService()
        previous_shell_service_resolver = (
            shell_session_port._shell_session_service_resolver
        )
        shell_session_port.set_shell_session_service_resolver(lambda: service)
        try:
            coordinator = ToolExecutionCoordinator(
                config=coordinator_config,
                planner=_ShellExecPlanner(),
                executor=GraphToolExecutor(executor=_ApprovingExecutor()),
            )
            outcome = await coordinator.run(request)
        finally:
            shell_session_port._shell_session_service_resolver = (
                previous_shell_service_resolver
            )

        assert outcome.result["metadata"]["route_policy"]["selected_authority"] == (
            "runtime_session_control"
        )
        [(identity, _shell_request)] = service.exec_calls
        self.service_owner_id = identity.execution_owner_id


class _ShellExecPlanner:
    async def build_action_plan(self, _action: Any, _context: dict[str, Any]) -> Any:
        class _Plan:
            tool_batch = ToolBatch(
                tool_batch_id="tb_worker_shell",
                tool_calls=(
                    ToolCall(
                        tool_call_id="tc_worker_shell",
                        tool_id="shell.utility",
                        parameters={"command": "printf worker"},
                    ),
                ),
                requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
                selection_rationale="Selected shell.utility.",
            )
            selected_tools = ["shell.utility"]
            tool_parameters = {"shell.utility": {"command": "printf worker"}}
            reasoning = "Selected shell.utility."
            expected_outcome = "Worker shell dispatch completed."
            execution_strategy = ExecutionStrategy.SEQUENTIAL

        return _Plan()


class _ApprovingExecutor:
    async def _maybe_request_approval(
        self,
        _tool: str,
        _params: dict[str, Any],
        _reasoning: str,
    ) -> bool:
        return True


class _FakeShellSessionService:
    def __init__(self) -> None:
        self.exec_calls: list[tuple[ShellSessionIdentity, ShellExecRequest]] = []
        self.write_calls: list[tuple[ShellSessionIdentity, ShellWriteRequest]] = []

    async def execute(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        capability: ShellCapability = ShellCapability.ASSESSMENT,
    ) -> ShellSessionUpdate:
        self.exec_calls.append((identity, request))
        return ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.COMPLETED,
            session_id="shs_worker_path",
            stdout="worker",
            stderr="",
            exit_code=0,
            stdin_available=False,
            truncated=False,
            duration_ms=1,
            summary="worker",
        )

    async def get_session_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None:
        return ShellCapability.UTILITY

    async def write_stdin(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellWriteRequest,
    ) -> ShellSessionUpdate:
        self.write_calls.append((identity, request))
        return ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.RUNNING,
            session_id=request.session_id,
            stdout="",
            stderr="",
            exit_code=None,
            stdin_available=True,
            truncated=False,
            duration_ms=1,
            summary="poll",
        )

    async def close_owner_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> None:
        return None

    async def close_task_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_generic_worker_builds_definition_configured_graph_input_config_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.agent_runs.worker as worker_module

    definition = _pathfinder_definition()
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    checkpointers = _FakeCheckpointerService()
    executor = _FakeExecutor(GraphExecutionResult(final_state=_final_state()))
    build_calls: list[tuple[str, Any]] = []

    def _fake_build_subagent_graph(
        actual_definition: SubagentDefinition,
        *,
        checkpointer: Any = None,
    ) -> str:
        build_calls.append((actual_definition.id, checkpointer))
        return "compiled-subagent"

    monkeypatch.setattr(
        worker_module,
        "build_subagent_graph",
        _fake_build_subagent_graph,
    )

    worker = ProcessLocalAgentRunWorker(
        registry=registry,
        definition_registry=SubagentRegistry([definition]),
        checkpointer_service=checkpointers,
        executor=executor,
    )

    completion = await worker(
        assignment=assignment,
        runtime_config={
            "configurable": {
                "runtime_projection": {
                    "tenant_id": 7,
                    "credential_ref": {"credential_id": "must-not-cross"},
                }
            }
        },
        graph_thread_id="child-thread-1",
        is_cancel_requested=_not_cancelled,
    )

    assert completion.result == _result()
    assert completion.graph_thread_id == "child-thread-1"
    assert completion.usage_records[0]["agent_run_id"] == "run-1"
    assert completion.usage_records[0]["tenant_id"] == 7
    assert completion.usage_records[0]["task_id"] == 42
    assert completion.usage_records[0]["user_id"] == 3
    assert completion.usage_records[0]["conversation_id"] == "conversation-1"
    assert completion.usage_records[0]["turn_sequence"] == 4
    assert checkpointers.task_ids == [42]
    assert build_calls == [("pathfinder", "checkpoint")]
    [call] = executor.calls
    assert call["compiled_graph"] == "compiled-subagent"
    assert call["graph_input"] == build_subagent_initial_state(
        definition=definition,
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )
    assert call["config"] == prepare_subagent_child_config(
        {
            "configurable": {
                "runtime_projection": {
                    "tenant_id": 7,
                    "credential_ref": {"credential_id": "must-not-cross"},
                }
            }
        },
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )
    assert "credential_ref" not in call["config"]["configurable"][
        "graph_runtime_context"
    ]
    assert call["task_id"] == 42
    assert call["state_container"] is None
    assert call["cancelled"] is False


@pytest.mark.asyncio
async def test_generic_worker_graph_input_can_dispatch_universal_shell_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.agent_runs.worker as worker_module

    definition = _pathfinder_definition()
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-shell")
    executor = _ShellDispatchingExecutor()

    monkeypatch.setattr(
        worker_module,
        "build_subagent_graph",
        lambda _definition, *, checkpointer=None: "compiled-subagent",
    )

    worker = ProcessLocalAgentRunWorker(
        registry=registry,
        definition_registry=SubagentRegistry([definition]),
        checkpointer_service=_FakeCheckpointerService(),
        executor=executor,
    )

    completion = await worker(
        assignment=assignment,
        runtime_config={
            "configurable": {
                "runtime_projection": {
                    "runtime_placement_mode": (
                        assignment.runtime_identity.runtime_placement_mode
                    ),
                    "workspace_id": assignment.runtime_identity.workspace_id,
                    "workspace_path": assignment.runtime_identity.workspace_path,
                    "actor_type": assignment.runtime_identity.actor_type,
                    "actor_id": assignment.runtime_identity.actor_id,
                    "runner_id": assignment.runtime_identity.runner_id,
                    "execution_site_id": assignment.runtime_identity.execution_site_id,
                }
            }
        },
        graph_thread_id="child-thread-shell",
        is_cancel_requested=_not_cancelled,
    )

    assert completion.result == _result()
    assert executor.dispatched_tools == [
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
    ]
    assert executor.request_owner_id == "subagent:run-1"
    assert executor.service_owner_id == "subagent:run-1"


def test_generic_worker_resolves_definition_by_assignment_kind() -> None:
    definition = _pathfinder_definition()

    assert (
        resolve_definition_for_assignment(
            SubagentRegistry([definition]),
            assignment=_assignment(),
        )
        == definition
    )


def test_generic_worker_rejects_missing_assignment_agent_id() -> None:
    definition = _pathfinder_definition()
    other_definition = replace(
        definition,
        id="otheragent",
        display_name="Other Agent",
        icon="otheragent",
    )

    with pytest.raises(RuntimeError, match="No subagent definition matches"):
        resolve_definition_for_assignment(
            SubagentRegistry([other_definition]),
            assignment=_assignment(),
        )


def test_generic_result_extraction_reads_definition_owned_result() -> None:
    assert extract_subagent_result_from_state(
        _final_state(),
        assignment=_assignment(),
    ) == _result()


def test_subagent_child_config_uses_agent_run_execution_owner() -> None:
    assignment = _assignment()

    config = prepare_subagent_child_config(
        {
            "configurable": {
                "runtime_projection": {
                    "tenant_id": 999,
                    "turn_id": "stale-parent-turn",
                    "turn_sequence": 99,
                    "execution_owner_id": "main:stale-parent-turn",
                    "credential_ref": {"credential_id": "must-not-cross"},
                }
            }
        },
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )

    configurable = config["configurable"]
    graph_context = configurable["graph_runtime_context"]
    assert graph_context["execution_owner_id"] == "subagent:run-1"
    assert graph_context["turn_id"] == assignment.parent_turn_id
    assert graph_context["turn_sequence"] == 4
    assert graph_context["task_id"] == assignment.task_id
    assert graph_context["tenant_id"] == assignment.tenant_id
    assert graph_context["graph_thread_id"] == "child-thread-1"
    assert "credential_ref" not in graph_context
    assert configurable["canonical_turn_id"] == assignment.parent_turn_id
    assert configurable["canonical_conversation_id"] == assignment.conversation_id
    assert configurable["canonical_turn_sequence"] == 4


@pytest.mark.asyncio
async def test_generic_worker_failure_preserves_graph_usage_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.agent_runs.worker as worker_module

    definition = _pathfinder_definition()
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    checkpointers = _FakeCheckpointerService()
    final_state = {
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
    executor = _FakeExecutor(GraphExecutionResult(final_state=final_state))

    monkeypatch.setattr(
        worker_module,
        "build_subagent_graph",
        lambda _definition, *, checkpointer=None: "compiled-subagent",
    )
    worker = ProcessLocalAgentRunWorker(
        registry=registry,
        definition_registry=SubagentRegistry([definition]),
        checkpointer_service=checkpointers,
        executor=executor,
    )

    with pytest.raises(SubagentRunFailed) as exc_info:
        await worker(
            assignment=assignment,
            runtime_config={"configurable": {}},
            graph_thread_id="child-thread-1",
            is_cancel_requested=_not_cancelled,
        )

    assert exc_info.value.execution_result.final_state == final_state


async def _not_cancelled() -> bool:
    return False
