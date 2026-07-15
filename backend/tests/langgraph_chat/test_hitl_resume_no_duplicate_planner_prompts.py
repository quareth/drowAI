"""End-to-end HITL resume regression test for duplicate planner prompts.

This test simulates backend deep-reasoning execution with:
- real LangGraph interrupt/resume behavior,
- in-memory checkpointer persistence across turns,
- mocked LLM/tool dependencies.

It verifies that planner parameter-generation prompts are emitted exactly once
across an approve/resume cycle (no duplicate pre-approval planning).

REGRESSION: If re-pending of resumed interrupts were allowed, stale stream
re-emission could cause duplicate planner/resume side effects. CI must fail
if planner_generate_parameters count exceeds 1.
"""

from __future__ import annotations

import os
import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

try:
    from langgraph.checkpoint.memory import MemorySaver

    LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover - defensive for missing optional dependency
    LANGGRAPH_AVAILABLE = False
    MemorySaver = None  # type: ignore[assignment]

from agent.graph.state import InteractiveState
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
from backend.services.langgraph_chat.contracts import AgentMode, ChatInputs, ExecutionMode
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.checkpoint.interrupt_state_service import InterruptStateService
from tests.tool_execution_module_helper import patch_tool_execution_attr


pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE,
    reason="langgraph memory checkpointer is not available",
)

GRAPH_THREAD_ID = "a" * 32
TEST_RUNTIME_IDENTITY = {
    "graph_thread_id": GRAPH_THREAD_ID,
    "tenant_id": 1,
    "runtime_placement_mode": "local",
    "workspace_id": "task-hitl",
    "workspace_path": "/tmp/task-hitl",
    "actor_type": "user",
    "actor_id": "101",
}
TEST_TOOL_ID = "filesystem.write_file"
TEST_TOOL_PARAMETERS = {"path": "/workspace/hitl-fixture.txt", "content": "ok"}
TEST_TOOL_ARGUMENTS_JSON = '{"path": "/workspace/hitl-fixture.txt", "content": "ok"}'


def _tool_function_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool["function"]["name"])
    return str(getattr(tool, "name"))


class _DummyCheckpointerService:
    """Provide a shared in-memory checkpointer across handle/resume calls."""

    def __init__(self, checkpointer: MemorySaver) -> None:
        class _CheckpointerWithSetup:
            def __init__(self, inner: MemorySaver) -> None:
                self._inner = inner

            async def setup(self) -> None:
                return None

            def __getattr__(self, item: str) -> Any:
                return getattr(self._inner, item)

        self._checkpointer = _CheckpointerWithSetup(checkpointer)

    @asynccontextmanager
    async def get_checkpointer(self, task_id: int):  # noqa: ARG002
        yield self._checkpointer


@dataclass
class _FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 15
    model: str = "test-model"
    provider: str = "openai"
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass
class _FakeChatWithUsageResult:
    content: str
    usage: _FakeUsage
    structured_output: Dict[str, Any] | None = None


@dataclass
class _FakeToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class _FakeToolCallResult:
    content: str
    tool_calls: List[_FakeToolCall]
    raw: Dict[str, Any]
    usage: _FakeUsage


def _fake_runtime_selection(*, user_id: int, model: str = "gpt-4o-mini") -> Dict[str, Any]:
    return {
        "provider": "openai",
        "model": model,
        "credential_ref": {"user_id": user_id, "provider": "openai"},
        "reasoning_effort": "medium",
    }


class _FakeRuntimeServices:
    def __init__(self, client_cls: Any) -> None:
        class _Resolver:
            def get_client(self, _selection: Any, *, target: Any = None, **_kwargs: Any) -> Any:
                return client_cls(
                    api_key="test-key",
                    model=getattr(target, "model", None) or "gpt-4o-mini",
                )

        self.client_resolver = _Resolver()


def _fake_builder_envelope() -> Dict[str, Any]:
    return {
        "tool_calls": [
            {
                "tool_id": TEST_TOOL_ID,
                "parameters": TEST_TOOL_ARGUMENTS_JSON,
                "intent": "Write fixture output",
            }
        ],
        "execution_strategy": "sequential",
        "deferred_followups": [],
        "selection_rationale": "Test-selected tool",
    }


def _fake_tool_call_dict(function_name: str) -> Dict[str, Any]:
    return {
        "id": "tc-1",
        "type": "function",
        "function": {
            "name": function_name,
            "arguments": TEST_TOOL_ARGUMENTS_JSON,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume_response,expected_tool_runs,expected_status",
    [
        ({"action": "approve"}, 1, "success"),
        (
            {
                "action": "edit",
                "edited_parameters": {
                    **TEST_TOOL_PARAMETERS,
                    "content": "edited",
                },
            },
            1,
            "success",
        ),
        ({"action": "skip"}, 0, "skipped"),
    ],
)
async def test_deep_reasoning_hitl_resume_has_single_parameter_generation_prompt(
    monkeypatch: pytest.MonkeyPatch,
    resume_response: Dict[str, Any],
    expected_tool_runs: int,
    expected_status: str,
) -> None:
    prompt_calls: List[Dict[str, str]] = []
    tool_run_count = 0

    # Keep completion callback DB-free for this integration test.
    async def _fake_run_turn_with_completion_callback(*, llm_func, result_holder, **kwargs):  # noqa: ANN001
        await llm_func(lambda _event: None, result_holder)
        if False:  # pragma: no cover - make this an async generator
            yield {}

    monkeypatch.setattr(
        "backend.services.langgraph_chat.handlers.deep_reasoning_handler.run_turn_with_completion_callback",
        _fake_run_turn_with_completion_callback,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.completion_callback.persist_chat_message_from_container",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.facade.ENABLE_LANGGRAPH_DEEP_REASONING",
        True,
    )

    # Keep execution path deterministic and focused on call_tool + HITL boundary.
    def _classify_turn(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.capability = "deep_reasoning"
        return interactive.as_graph_update()

    async def _planner_node(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.capability = "deep_reasoning"
        interactive.facts.plan = ["Run one tool", "Summarize"]
        interactive.facts.todo_list = ["Run one tool", "Summarize"]
        interactive.facts.current_goal = "Run one tool"
        return interactive.as_graph_update()

    async def _plan_review_node(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        return InteractiveState.from_mapping(state).as_graph_update()

    async def _decision_router(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        metadata = dict(interactive.facts.metadata or {})
        has_tool_result = bool(
            metadata.get("tool_history")
            or metadata.get("last_tool_result")
            or metadata.get("synthesized_output")
            or interactive.trace.executed_tools
        )
        action = "finalize" if has_tool_result else "call_tool"
        interactive.facts.decision_history = list(interactive.facts.decision_history or [])
        interactive.facts.decision_history.append(
            "finalize: Goal achieved" if has_tool_result else "call_tool: Execute selected tool"
        )
        interactive.facts.metadata = metadata
        interactive.facts.metadata["router_outcome"] = {
            "action": action,
            "candidate_action": action,
            "reason": "test_fixture",
        }
        return interactive.as_graph_update()

    async def _select_categories(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.metadata = dict(interactive.facts.metadata or {})
        interactive.facts.metadata["selected_categories"] = ["filesystem"]
        return interactive.as_graph_update()

    async def _synthesize_tool_output(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.metadata = dict(interactive.facts.metadata or {})
        interactive.facts.metadata["synthesized_output"] = {
            "summary": "Tool completed",
            "key_findings": ["ok"],
            "success": True,
            "status": "success",
        }
        return interactive.as_graph_update()

    async def _post_tool_reasoning(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = list(interactive.facts.decision_history or [])
        interactive.facts.decision_history.append("finalize: Goal achieved")
        interactive.facts.metadata = dict(interactive.facts.metadata or {})
        interactive.facts.metadata["user_goal_achieved"] = True
        interactive.facts.metadata["router_outcome"] = {
            "action": "finalize",
            "candidate_action": "finalize",
            "reason": "test_fixture",
        }
        return interactive.as_graph_update()

    async def _observation_adapter(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        return InteractiveState.from_mapping(state).as_graph_update()

    async def _finalize(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.trace.final_text = "Deep reasoning completed"
        return interactive.as_graph_update()

    monkeypatch.setattr("agent.graph.nodes.classification.classify_turn", _classify_turn)
    monkeypatch.setattr("agent.graph.nodes.planner.planner_node", _planner_node)
    monkeypatch.setattr("agent.graph.nodes.plan_review.plan_review_node", _plan_review_node)
    monkeypatch.setattr("agent.graph.nodes.decision_router", _decision_router)
    decision_router_module = importlib.import_module("agent.graph.nodes.decision_router")
    monkeypatch.setattr(decision_router_module, "decision_router", _decision_router)
    monkeypatch.setattr(
        "agent.graph.nodes.select_tool_categories.select_tool_categories_node",
        _select_categories,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.tool_synthesizer.synthesize_tool_output",
        _synthesize_tool_output,
    )
    monkeypatch.setattr("agent.graph.nodes.post_tool_reasoning", _post_tool_reasoning)
    post_tool_module = importlib.import_module("agent.graph.nodes.post_tool_reasoning")
    monkeypatch.setattr(post_tool_module, "post_tool_reasoning", _post_tool_reasoning)
    post_tool_node_module = importlib.import_module("agent.graph.nodes.post_tool_reasoning.node")
    monkeypatch.setattr(post_tool_node_module, "post_tool_reasoning", _post_tool_reasoning)
    monkeypatch.setattr(
        "agent.graph.nodes.observation_adapter.adapt_to_observations",
        _observation_adapter,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.finalize_results",
        _finalize,
    )

    # Keep planner tool catalog deterministic.
    patch_tool_execution_attr(
        monkeypatch,
        "_get_full_tool_catalog_for_planner",
        lambda config: [TEST_TOOL_ID],  # noqa: ARG005
    )
    patch_tool_execution_attr(
        monkeypatch,
        "_get_category_filtered_catalog",
        lambda categories, config: [TEST_TOOL_ID],  # noqa: ARG005
    )

    # Ensure tool approval HITL triggers in this test.
    monkeypatch.setattr("agent.graph.nodes.hitl_helpers.ENABLE_HITL_INTERRUPTS", True)
    monkeypatch.setattr("agent.graph.subgraphs.tool_execution.should_require_approval", lambda metadata: True)

    class _FakeLLMClient:
        def __init__(self, api_key: str, model: str) -> None:  # noqa: ARG002
            pass

        async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> _FakeChatWithUsageResult:  # noqa: ARG002
            structured_name = getattr(kwargs.get("structured_output"), "name", "")
            if structured_name == "commit_tool_batch":
                prompt_calls.append({"method": "planner_generate_parameters", "user_prompt": user_prompt})
                return _FakeChatWithUsageResult(
                    content="",
                    structured_output=_fake_builder_envelope(),
                    usage=_FakeUsage(),
                )

            prompt_calls.append({"method": "planner_select_tools", "user_prompt": user_prompt})
            return _FakeChatWithUsageResult(
                content=f'{{"selected_tools": ["{TEST_TOOL_ID}"], "execution_strategy": "sequential"}}',
                structured_output={
                    "selected_tools": [TEST_TOOL_ID],
                    "execution_strategy": "sequential",
                },
                usage=_FakeUsage(),
            )

        async def chat_with_tools_with_usage(
            self,
            system_prompt: str,  # noqa: ARG002
            user_prompt: str,
            *,
            tools: List[Dict[str, Any]],
            **kwargs: Any,  # noqa: ARG002
        ) -> _FakeToolCallResult:
            prompt_calls.append({"method": "planner_generate_parameters", "user_prompt": user_prompt})
            function_name = _tool_function_name(tools[0])
            return _FakeToolCallResult(
                content="",
                tool_calls=[
                    _FakeToolCall(
                        id="tc-1",
                        name=function_name,
                        arguments=TEST_TOOL_ARGUMENTS_JSON,
                    )
                ],
                raw={},
                usage=_FakeUsage(),
            )

        async def chat_with_tools(
            self,
            system_prompt: str,
            user_prompt: str,
            *,
            tools: List[Dict[str, Any]],
            **kwargs: Any,
        ) -> Dict[str, Any]:
            prompt_calls.append({"method": "planner_generate_parameters", "user_prompt": user_prompt})
            function_name = _tool_function_name(tools[0])
            return {"tool_calls": [_fake_tool_call_dict(function_name)]}

    monkeypatch.setattr("agent.reasoning.enhanced_planner.LLMClientFactory.get_client", _FakeLLMClient)

    class _StubCoordinator:
        async def run(self, request):  # noqa: ANN001
            nonlocal tool_run_count
            tool_run_count += 1

            def _to_graph_metadata() -> dict:
                return {"tool_id": TEST_TOOL_ID, "result": {"success": True}}

            return SimpleNamespace(
                tool_id=TEST_TOOL_ID,
                parameters=dict(TEST_TOOL_PARAMETERS),
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
                duration=1.0,
                reasoning=["Executed fixture tool"],
                summary="ok",
                to_graph_metadata=_to_graph_metadata,
            )

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),  # noqa: ARG005
    )

    # Skip intent-classifier network calls in facade setup.
    async def _no_intent_classification(runtime_config):  # noqa: ANN001
        return None

    shared_checkpointer = MemorySaver()
    checkpointer_service = _DummyCheckpointerService(shared_checkpointer)
    facade = LangGraphChatFacade(checkpointer_service=checkpointer_service)
    monkeypatch.setattr(facade._intent_classifier, "enrich_runtime_config", _no_intent_classification)

    task_id = 9026
    expected_task_id = task_id
    stream_events: List[Dict[str, Any]] = []
    stream_hub = get_in_memory_stream_hub()
    original_publish = stream_hub.publish

    async def _capture_stream_events(*, task_id: int, event: Dict[str, Any]) -> None:
        captured_task_id = task_id
        if captured_task_id == expected_task_id:
            stream_events.append(dict(event))
        await original_publish(task_id=captured_task_id, event=event)

    monkeypatch.setattr(stream_hub, "publish", _capture_stream_events)

    runtime_selection = _fake_runtime_selection(user_id=101)
    runtime_services = _FakeRuntimeServices(_FakeLLMClient)
    chat_inputs = ChatInputs(
        task_id=task_id,
        user_id=101,
        message="Run one command with approval",
        conversation_id=None,
        history=[],
        api_key="test-key",
        provider="openai",
        model="gpt-4o-mini",
        credential_ref=runtime_selection["credential_ref"],
        llm_runtime_selection=runtime_selection,
        requested_mode=ExecutionMode.DEEP_REASONING,
        agent_mode=AgentMode.AGENT,
    )

    first_result = await facade.handle_turn(
        chat_inputs,
        metadata={
            **TEST_RUNTIME_IDENTITY,
            "turn_id": f"task-{task_id}-turn-1",
            "turn_number": 1,
            "turn_sequence": 1,
        },
        runtime_services=runtime_services,
    )
    assert first_result.metadata.get("interrupted") is True
    assert first_result.metadata.get("graph_name") == "deep_reasoning"

    interrupt_service = InterruptStateService(checkpointer_service=checkpointer_service)
    pending_interrupt = await interrupt_service.get_pending_interrupt(
        task_id,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="deep_reasoning",
    )
    assert pending_interrupt is not None
    assert pending_interrupt["interrupt_type"] == "tool_approval"
    assert pending_interrupt["payload"].get("tool_id") == TEST_TOOL_ID
    checkpoint_id = pending_interrupt.get("checkpoint_id")
    assert checkpoint_id is not None
    pending_interrupt_after_refresh = await InterruptStateService(
        checkpointer_service=checkpointer_service
    ).get_pending_interrupt(
        task_id,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="deep_reasoning",
    )
    assert pending_interrupt_after_refresh is not None
    assert pending_interrupt_after_refresh.get("checkpoint_id") == checkpoint_id
    assert pending_interrupt_after_refresh.get("interrupt_type") == "tool_approval"
    interrupt_event = next(
        (event for event in stream_events if event.get("type") == "graph_interrupt"),
        None,
    )
    assert interrupt_event is not None
    assert interrupt_event.get("graph_name") == "deep_reasoning"
    assert interrupt_event.get("interrupt_type") == "tool_approval"

    resumed = await facade.resume_from_interrupt(
        task_id=task_id,
        graph_thread_id=GRAPH_THREAD_ID,
        user_id=101,
        response=resume_response,
        graph_name="deep_reasoning",
        checkpoint_id=checkpoint_id,
        llm_runtime_selection=runtime_selection,
        runtime_services=runtime_services,
    )

    assert resumed.final_text == "Deep reasoning completed"
    assert resumed.interactive_state is not None
    final_state = resumed.interactive_state
    assert isinstance(final_state, InteractiveState)
    tool_history = final_state.facts.metadata.get("tool_history", [])
    assert isinstance(tool_history, list)
    assert len(tool_history) == expected_tool_runs
    assert tool_run_count == expected_tool_runs

    methods = [entry["method"] for entry in prompt_calls]
    assert methods[:2] == ["planner_select_tools", "planner_generate_parameters"]
    # REGRESSION: must be exactly 1; duplicate would indicate stale replay path.
    assert methods.count("planner_generate_parameters") == 1

    usage_records = final_state.trace.usage_records or []
    param_usage_records = [r for r in usage_records if r.get("source") == "planner_parameter_generation"]
    assert len(param_usage_records) == 1

    executed_tools = final_state.trace.executed_tools or []
    assert len(executed_tools) == 1
    assert executed_tools[0].tool_id == TEST_TOOL_ID
    assert executed_tools[0].status == expected_status
    if expected_tool_runs > 0:
        streamed_types = [event.get("type") for event in stream_events]
        assert "tool_start" in streamed_types
        assert "tool_end" in streamed_types


@pytest.mark.asyncio
async def test_simple_tool_backend_hitl_resume_has_single_parameter_generation_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_calls: List[Dict[str, str]] = []
    tool_run_count = 0

    async def _fake_run_turn_with_completion_callback(*, llm_func, result_holder, **kwargs):  # noqa: ANN001
        await llm_func(lambda _event: None, result_holder)
        if False:  # pragma: no cover
            yield {}

    monkeypatch.setattr(
        "backend.services.langgraph_chat.handlers.simple_tool_handler.run_turn_with_completion_callback",
        _fake_run_turn_with_completion_callback,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.completion_callback.persist_chat_message_from_container",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.facade.ENABLE_LANGGRAPH_SIMPLE_TOOL",
        True,
    )

    def _classify_turn(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.capability = "simple_tool_execution"
        return interactive.as_graph_update()

    async def _select_categories(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.metadata = dict(interactive.facts.metadata or {})
        interactive.facts.metadata["selected_categories"] = ["filesystem"]
        return interactive.as_graph_update()

    async def _articulation(state: Dict[str, Any], context=None, config=None):  # noqa: ANN001, ARG001
        return InteractiveState.from_mapping(state).as_graph_update()

    async def _synthesize_tool_output(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.metadata = dict(interactive.facts.metadata or {})
        interactive.facts.metadata["synthesized_output"] = {
            "summary": "Tool completed",
            "success": True,
            "status": "success",
        }
        return interactive.as_graph_update()

    async def _post_tool_reasoning(state: Dict[str, Any], context=None, config=None, writer=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = list(interactive.facts.decision_history or [])
        interactive.facts.decision_history.append("finalize: Goal achieved")
        interactive.facts.metadata = dict(interactive.facts.metadata or {})
        interactive.facts.metadata["router_outcome"] = {
            "action": "finalize",
            "candidate_action": "finalize",
            "reason": "test_fixture",
        }
        return interactive.as_graph_update()

    async def _format_results(state: Dict[str, Any], context=None, config=None):  # noqa: ANN001, ARG001
        return InteractiveState.from_mapping(state).as_graph_update()

    def _finalize_turn(state: Dict[str, Any], context=None):  # noqa: ANN001, ARG001
        interactive = InteractiveState.from_mapping(state)
        interactive.trace.final_text = "Simple tool completed"
        return interactive.as_graph_update()

    monkeypatch.setattr("agent.graph.nodes.classification.classify_turn", _classify_turn)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.classify_turn", _classify_turn)
    monkeypatch.setattr(
        "agent.graph.nodes.select_tool_categories.select_tool_categories_node",
        _select_categories,
    )
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.select_tool_categories_node",
        _select_categories,
    )
    monkeypatch.setattr("agent.graph.nodes.tool_articulation.articulate_tool_intent", _articulation)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.articulate_tool_intent", _articulation)
    monkeypatch.setattr(
        "agent.graph.nodes.tool_synthesizer.synthesize_tool_output",
        _synthesize_tool_output,
    )
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.synthesize_tool_output", _synthesize_tool_output)
    monkeypatch.setattr("agent.graph.nodes.post_tool_reasoning", _post_tool_reasoning)
    post_tool_module = importlib.import_module("agent.graph.nodes.post_tool_reasoning")
    monkeypatch.setattr(post_tool_module, "post_tool_reasoning", _post_tool_reasoning)
    post_tool_node_module = importlib.import_module("agent.graph.nodes.post_tool_reasoning.node")
    monkeypatch.setattr(post_tool_node_module, "post_tool_reasoning", _post_tool_reasoning)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.post_tool_reasoning", _post_tool_reasoning)
    monkeypatch.setattr("agent.graph.nodes.finalize.finalize_results", _format_results)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.finalize_results", _format_results)
    monkeypatch.setattr("agent.graph.nodes.finalizer.finalize_turn", _finalize_turn)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.finalize_turn", _finalize_turn)

    patch_tool_execution_attr(
        monkeypatch,
        "_get_full_tool_catalog_for_planner",
        lambda config: [TEST_TOOL_ID],  # noqa: ARG005
    )
    patch_tool_execution_attr(
        monkeypatch,
        "_get_category_filtered_catalog",
        lambda categories, config: [TEST_TOOL_ID],  # noqa: ARG005
    )
    monkeypatch.setattr("agent.graph.nodes.hitl_helpers.ENABLE_HITL_INTERRUPTS", True)
    monkeypatch.setattr("agent.graph.subgraphs.tool_execution.should_require_approval", lambda metadata: True)

    class _FakeLLMClient:
        def __init__(self, api_key: str, model: str) -> None:  # noqa: ARG002
            pass

        async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> _FakeChatWithUsageResult:  # noqa: ARG002
            structured_name = getattr(kwargs.get("structured_output"), "name", "")
            if structured_name == "commit_tool_batch":
                prompt_calls.append({"method": "planner_generate_parameters", "user_prompt": user_prompt})
                return _FakeChatWithUsageResult(
                    content="",
                    structured_output=_fake_builder_envelope(),
                    usage=_FakeUsage(),
                )

            prompt_calls.append({"method": "planner_select_tools", "user_prompt": user_prompt})
            return _FakeChatWithUsageResult(
                content=f'{{"selected_tools": ["{TEST_TOOL_ID}"], "execution_strategy": "sequential"}}',
                structured_output={
                    "selected_tools": [TEST_TOOL_ID],
                    "execution_strategy": "sequential",
                },
                usage=_FakeUsage(),
            )

        async def chat_with_tools_with_usage(
            self,
            system_prompt: str,  # noqa: ARG002
            user_prompt: str,
            *,
            tools: List[Dict[str, Any]],
            **kwargs: Any,  # noqa: ARG002
        ) -> _FakeToolCallResult:
            prompt_calls.append({"method": "planner_generate_parameters", "user_prompt": user_prompt})
            function_name = _tool_function_name(tools[0])
            return _FakeToolCallResult(
                content="",
                tool_calls=[_FakeToolCall(id="tc-1", name=function_name, arguments=TEST_TOOL_ARGUMENTS_JSON)],
                raw={},
                usage=_FakeUsage(),
            )

        async def chat_with_tools(
            self,
            system_prompt: str,
            user_prompt: str,
            *,
            tools: List[Dict[str, Any]],
            **kwargs: Any,
        ) -> Dict[str, Any]:
            prompt_calls.append({"method": "planner_generate_parameters", "user_prompt": user_prompt})
            function_name = _tool_function_name(tools[0])
            return {"tool_calls": [_fake_tool_call_dict(function_name)]}

    monkeypatch.setattr("agent.reasoning.enhanced_planner.LLMClientFactory.get_client", _FakeLLMClient)

    class _StubCoordinator:
        async def run(self, request):  # noqa: ANN001
            nonlocal tool_run_count
            tool_run_count += 1

            def _to_graph_metadata() -> dict:
                return {"tool_id": TEST_TOOL_ID, "result": {"success": True}}

            return SimpleNamespace(
                tool_id=TEST_TOOL_ID,
                parameters=dict(TEST_TOOL_PARAMETERS),
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
                duration=1.0,
                reasoning=["Executed fixture tool"],
                summary="ok",
                to_graph_metadata=_to_graph_metadata,
            )

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),  # noqa: ARG005
    )

    async def _no_intent_classification(runtime_config):  # noqa: ANN001
        return None

    shared_checkpointer = MemorySaver()
    checkpointer_service = _DummyCheckpointerService(shared_checkpointer)
    facade = LangGraphChatFacade(checkpointer_service=checkpointer_service)
    monkeypatch.setattr(facade._intent_classifier, "enrich_runtime_config", _no_intent_classification)

    task_id = 9025
    expected_task_id = task_id
    stream_events: List[Dict[str, Any]] = []
    stream_hub = get_in_memory_stream_hub()
    original_publish = stream_hub.publish

    async def _capture_stream_events(*, task_id: int, event: Dict[str, Any]) -> None:
        captured_task_id = task_id
        if captured_task_id == expected_task_id:
            stream_events.append(dict(event))
        await original_publish(task_id=captured_task_id, event=event)

    monkeypatch.setattr(stream_hub, "publish", _capture_stream_events)

    runtime_selection = _fake_runtime_selection(user_id=101)
    runtime_services = _FakeRuntimeServices(_FakeLLMClient)
    chat_inputs = ChatInputs(
        task_id=task_id,
        user_id=101,
        message="Run one command with approval",
        conversation_id=None,
        history=[],
        api_key="test-key",
        provider="openai",
        model="gpt-4o-mini",
        credential_ref=runtime_selection["credential_ref"],
        llm_runtime_selection=runtime_selection,
        requested_mode=ExecutionMode.SIMPLE_TOOL,
        agent_mode=AgentMode.AGENT,
    )

    first_result = await facade.handle_turn(
        chat_inputs,
        metadata={
            **TEST_RUNTIME_IDENTITY,
            "turn_id": f"task-{task_id}-turn-1",
            "turn_number": 1,
            "turn_sequence": 1,
        },
        runtime_services=runtime_services,
    )
    assert first_result.metadata.get("interrupted") is True
    assert first_result.metadata.get("graph_name") == "simple_tool"

    interrupt_service = InterruptStateService(checkpointer_service=checkpointer_service)
    pending_interrupt = await interrupt_service.get_pending_interrupt(
        task_id,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
    )
    assert pending_interrupt is not None
    checkpoint_id = pending_interrupt.get("checkpoint_id")
    assert checkpoint_id is not None
    pending_interrupt_after_refresh = await InterruptStateService(
        checkpointer_service=checkpointer_service
    ).get_pending_interrupt(
        task_id,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
    )
    assert pending_interrupt_after_refresh is not None
    assert pending_interrupt_after_refresh.get("checkpoint_id") == checkpoint_id
    assert pending_interrupt_after_refresh.get("interrupt_type") == "tool_approval"
    interrupt_event = next(
        (event for event in stream_events if event.get("type") == "graph_interrupt"),
        None,
    )
    assert interrupt_event is not None
    assert interrupt_event.get("graph_name") == "simple_tool"
    assert interrupt_event.get("interrupt_type") == "tool_approval"

    resumed = await facade.resume_from_interrupt(
        task_id=task_id,
        graph_thread_id=GRAPH_THREAD_ID,
        user_id=101,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        llm_runtime_selection=runtime_selection,
        runtime_services=runtime_services,
    )
    assert resumed.final_text == "Simple tool completed"
    assert resumed.interactive_state is not None
    methods = [entry["method"] for entry in prompt_calls]
    assert methods[:2] == ["planner_select_tools", "planner_generate_parameters"]
    # REGRESSION: must be exactly 1; duplicate would indicate stale replay path.
    assert methods.count("planner_generate_parameters") == 1
    assert tool_run_count == 1
    streamed_types = [event.get("type") for event in stream_events]
    assert "tool_start" in streamed_types
    assert "tool_end" in streamed_types
