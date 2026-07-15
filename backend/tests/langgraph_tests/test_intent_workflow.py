"""Regression tests for LangGraph intent routing and turn orchestration."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import pytest

from agent.graph import build_initial_state, build_simple_chat_graph
from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.persistence import get_default_checkpointer
from agent.graph.state import FactsState, TraceState, InteractiveInput, InteractiveState
from agent.graph.nodes.simple_chat import run_simple_chat
from agent.graph.nodes.post_tool_reasoning.models import RetryablePostToolReasoningError
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter
from backend.services.usage_tracking.models import UsageData
from backend.services.langgraph_chat.intent.classifier import IntentClassifier
from backend.services.langgraph_chat.intent.signals import (
    collect_intent_signals,
    embed_intent_signals,
)
from backend.services.langgraph_chat.routing.selectors import select_branch, ChatBranch
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.exceptions import HITLError
from backend.services.langgraph_chat.compression.window_models import (
    ContextWindowDecision,
    ContextWindowSnapshot,
)
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
    CompressionPassResult,
    ContextCompressionOutcome,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.turn_service import (
    _build_compression_epoch_id,
)
from backend.services.langgraph_chat.execution.turn_service import (
    TurnExecutionService,
    run_langgraph_generation,
    run_checkpoint_retry_generation,
    run_resume_generation,
)
from backend.services.streaming.in_memory_hub import InMemoryStreamHub, QueuedMessage
from agent.tool_runtime import ToolExecutionOutcome, ToolCatalogEntry
from agent.models import ActionPlan, ActionType, ExecutionStrategy
from tests.tool_execution_module_helper import patch_tool_execution_attr

GRAPH_THREAD_ID = "a" * 32
_FACADE_CONTEXT_SOURCE_IDS = list(range(1, 13))
_FACADE_CONTEXT_HISTORY = [
    message
    for turn_number in range(1, 7)
    for message in (
        {"role": "user", "content": f"question {turn_number}"},
        {"role": "assistant", "content": f"answer {turn_number}"},
    )
]


def _decision_with_prompt_budget(
    decision: ContextWindowDecision,
) -> tuple[ContextWindowDecision, SimpleNamespace]:
    """Return the exact classifier decision contract used by the facade."""
    return decision, SimpleNamespace(
        usable_prompt_tokens=127_999,
        trigger_tokens=102_399,
        reserved_output_tokens=1,
        override_active=False,
    )


class _RefusalLifecycle:
    """Capture lifecycle finalization for start-flow refusal regressions."""

    def __init__(self) -> None:
        self.end_calls: list[Dict[str, Any]] = []

    def start_run(self, **_kwargs: Any) -> None:
        return None

    def is_cancel_requested(self, **_kwargs: Any) -> bool:
        return False

    def end_run(self, **kwargs: Any) -> None:
        self.end_calls.append(dict(kwargs))


async def _run_facade_context_decision(
    *,
    service: TurnExecutionService,
    chat_inputs: ChatInputs,
    runtime_services: Any,
    handoff: Dict[str, Any],
    turn_sequence: int | None = None,
) -> None:
    """Execute the facade-owned context decision for mocked-facade tests."""
    original_history = list(chat_inputs.history)
    history = list(original_history)
    source_ids = list(chat_inputs.history_source_message_ids)
    assert history == _FACADE_CONTEXT_HISTORY
    assert source_ids == _FACADE_CONTEXT_SOURCE_IDS

    class _Session:
        def close(self) -> None:
            return

    class _SnapshotRepository:
        def __init__(self, _db: Any) -> None:
            return

        def persist_snapshot(self, **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                message=kwargs["summary_text"],
                token_count=kwargs["token_count"],
            )

    service._turn_compression_service._session_factory = _Session
    service._turn_compression_service._compression_snapshot_repository_factory = (
        _SnapshotRepository
    )
    (
        history_for_facade,
        context_window_metadata,
        compression_metadata,
        context_event_emitted,
    ) = await service._turn_compression_service.prepare_preturn_history(
        task_id=chat_inputs.task_id,
        conversation_id=chat_inputs.conversation_id or "",
        turn_sequence=turn_sequence,
        history=history,
        history_source_message_ids=source_ids,
        model=chat_inputs.model or "",
        context_limit_tokens=128_000,
        request_prompt_tokens=128_000,
        reserved_output_tokens=1,
        candidate_classifier_prompt_counter=lambda _history: 1_000,
        provider=chat_inputs.provider or "openai",
        llm_runtime_selection=chat_inputs.llm_runtime_selection,
        runtime_services=runtime_services,
        runtime_user_id=chat_inputs.user_id,
        on_context_window_snapshot=lambda snapshot: handoff.update(
            {"context_window": snapshot}
        ),
    )
    assert history_for_facade == history
    chat_inputs.history = original_history
    handoff.update(
        {
            "context_window": context_window_metadata,
            "compression": compression_metadata,
            "context_event_emitted": context_event_emitted,
        }
    )


async def _commit_facade_turn(
    chat_inputs: ChatInputs,
    *,
    result_conversation_id: str,
    service: TurnExecutionService,
    runtime_services: Any,
    pre_classifier_context_handoff: Dict[str, Any],
    **_kwargs: Any,
) -> LangGraphChatResult:
    """Run the moved decision before returning a mocked successful facade result."""
    await _run_facade_context_decision(
        service=service,
        chat_inputs=chat_inputs,
        runtime_services=runtime_services,
        handoff=pre_classifier_context_handoff,
    )
    return LangGraphChatResult(
        final_text="ok",
        conversation_id=result_conversation_id,
        metadata={"role": "assistant", "streaming": False},
        _event_iterator=lambda: _empty_async_iter(),
    )


def _commit_facade_handler(
    service: TurnExecutionService,
    result_conversation_id: str,
) -> Any:
    """Return an async mocked-facade handler bound to one turn service."""

    async def _handle(chat_inputs: ChatInputs, **kwargs: Any) -> LangGraphChatResult:
        return await _commit_facade_turn(
            chat_inputs,
            result_conversation_id=result_conversation_id,
            service=service,
            **kwargs,
        )

    return _handle


@pytest.fixture(autouse=True)
def _stub_llm_runtime_config_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide non-secret provider runtime dependencies for orchestration tests."""

    class _StubRuntimeClient:
        _reasoning_effort = "minimal"

        async def chat(self, *_args: Any, **_kwargs: Any) -> str:
            return (
                '{"selected_categories":["information_gathering"],'
                '"reasoning":"deterministic test selection"}'
            )

        async def chat_with_usage(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content=(
                    '{"selected_categories":["information_gathering"],'
                    '"reasoning":"deterministic test selection"}'
                ),
                usage=None,
                structured_output={
                    "selected_categories": ["information_gathering"],
                    "reasoning": "deterministic test selection",
                },
            )

        async def stream_chat_messages(self, *_args: Any, **_kwargs: Any) -> Any:
            yield "Safe completion response."

        async def stream_chat_messages_with_usage(self, *_args: Any, **_kwargs: Any) -> Any:
            async def _chunks():
                yield "Safe completion response."

            return SimpleNamespace(
                content_iterator=_chunks(),
                get_final_usage=lambda: UsageData(
                    prompt_tokens=10,
                    completion_tokens=1,
                    total_tokens=11,
                    model="gpt-5.2",
                    provider="openai",
                    api_surface="responses",
                ),
            )

    class _StubClientResolver:
        def get_client(self, *_args: Any, **_kwargs: Any) -> Any:
            return _StubRuntimeClient()

        def resolve_secret(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(provider="openai", value="sk-test-runtime")

    class _StubRuntimeServices:
        client_resolver = _StubClientResolver()

    class _StubRuntimeSelection:
        provider = "openai"
        model = "gpt-5.2"
        credential_ref = SimpleNamespace(
            user_id=10,
            provider="openai",
            to_dict=lambda: {"user_id": 10, "provider": "openai"},
        )
        reasoning_effort = None

        def to_dict(self) -> Dict[str, Any]:
            return {
                "provider": self.provider,
                "model": self.model,
                "credential_ref": self.credential_ref.to_dict(),
                "reasoning_effort": self.reasoning_effort,
            }

    class _StubRuntimeConfigService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def build_runtime_selection(self, *_args: Any, **_kwargs: Any) -> Any:
            return _StubRuntimeSelection()

        def build_continuation_selection(self, *_args: Any, **_kwargs: Any) -> Any:
            return _StubRuntimeSelection()

        def build_runtime_services(self) -> Any:
            return _StubRuntimeServices()

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LLMRuntimeConfigService",
        _StubRuntimeConfigService,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.LLMRuntimeConfigService",
        _StubRuntimeConfigService,
    )
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.LLMRuntimeConfigService",
        _StubRuntimeConfigService,
        raising=False,
    )
    monkeypatch.setattr(
        "backend.services.llm_provider.LLMRuntimeConfigService",
        _StubRuntimeConfigService,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.bootstrap_service.TurnExecutionBootstrapService._load_graph_thread_id",
        lambda self, *, task_id: GRAPH_THREAD_ID,
    )


@pytest.fixture(autouse=True)
def _stub_tool_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubPlanner:
        def __init__(self, _config, **_kwargs):  # noqa: ANN001
            pass

        async def build_action_plan(self, action, _context):  # noqa: ANN001
            target = action.target or "127.0.0.1"
            return ActionPlan(
                type=ActionType.GATHER_INFO,
                target=target,
                selected_tools=["information_gathering.network_discovery.nmap"],
                tool_parameters={
                    "information_gathering.network_discovery.nmap": {
                        "target": target,
                        "ports": "1-1024",
                    }
                },
                llm_tool_parameters={},
                execution_strategy=ExecutionStrategy.SEQUENTIAL,
                reasoning="Deterministic test plan",
                expected_outcome="Discover open ports on target",
                usage_records=[],
            )

    class _StubCoordinator:
        async def run(self, request):  # noqa: ANN001
            catalog = [
                ToolCatalogEntry(
                    tool_id="information_gathering.network_discovery.nmap",
                    name="nmap",
                    category="network",
                    description="",
                )
            ]
            return ToolExecutionOutcome(
                tool_id="information_gathering.network_discovery.nmap",
                parameters={
                    "target": (request.targets or ["127.0.0.1"])[0],
                    "ports": "1-1024",
                },
                catalog=catalog,
                result={
                    "tool": "information_gathering.network_discovery.nmap",
                    "success": True,
                    "stdout_excerpt": "Scan complete",
                    "stderr_excerpt": "",
                    "observation": "Open ports discovered",
                    "status": "success",
                },
                summary="Summarised output",
                reasoning=["Planner reasoning"],
                duration=0.1,
            )

    patch_tool_execution_attr(
        monkeypatch,
        "EnhancedActionPlanner",
        _StubPlanner,
    )
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )


@pytest.fixture(autouse=True)
def _stub_finalize_results_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubFinalizeClient:
        _reasoning_effort = "minimal"

        async def stream_chat_messages(self, _messages, **_kwargs):  # noqa: ANN001
            yield "Safe completion response."

        async def stream_chat_messages_with_usage(self, _messages, **_kwargs):  # noqa: ANN001
            async def _chunks():
                yield "Safe completion response."

            return SimpleNamespace(
                content_iterator=_chunks(),
                get_final_usage=lambda: UsageData(
                    prompt_tokens=10,
                    completion_tokens=1,
                    total_tokens=11,
                    model="gpt-5-mini",
                    provider="openai",
                    api_surface="responses",
                ),
            )

        async def chat(
            self, system_prompt: str, _user_prompt: str, **_kwargs: Any
        ) -> str:
            _ = system_prompt
            return '{"next_action":"finalize","action_reasoning":"Sufficient evidence collected.","user_goal_achieved":true}'

    monkeypatch.setattr(
        "agent.graph.nodes.finalize.resolve_llm_client",
        lambda *_args, **_kwargs: _StubFinalizeClient(),
    )
    post_tool_module = importlib.import_module(
        "agent.graph.nodes.post_tool_reasoning.node"
    )
    monkeypatch.setattr(
        post_tool_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _StubFinalizeClient(),
    )


def _build_state(
    *,
    message: str,
    metadata: Dict[str, Any],
    builder,
) -> InteractiveState:
    if "context_bundle" not in metadata:
        from agent.graph.context.builder import (
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle,
        )

        metadata = dict(metadata)
        metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
            conversation_id="intent-test",
            turn_id="intent-test-turn-0",
            turn_sequence=0,
            messages=[],
            current_message=message,
        )
    metadata.setdefault(
        "graph_runtime_context",
        {
            "task_id": 99,
            "user_id": 10,
            "graph_thread_id": GRAPH_THREAD_ID,
            "tenant_id": 1,
            "runtime_placement_mode": "local",
            "workspace_id": "task-99",
            "workspace_path": "/tmp/task-99",
            "actor_type": "user",
            "actor_id": "10",
        },
    )
    payload = InteractiveInput(task_id=99, message=message, metadata=metadata)
    initial = build_initial_state(payload)
    # Ensure tool eligibility where needed
    initial.setdefault("facts", {}).setdefault("tool_ids", ["nmap"])
    compiled = builder(checkpointer=get_default_checkpointer())

    class _Resolver:
        def get_client(self, *_args: Any, **_kwargs: Any) -> Any:
            class _Client:
                _reasoning_effort = "minimal"

                async def chat(self, *_chat_args: Any, **_chat_kwargs: Any) -> str:
                    return (
                        '{"selected_categories":["information_gathering"],'
                        '"reasoning":"deterministic test selection"}'
                    )

                async def stream_chat_messages_with_usage(self, *_args: Any, **_kwargs: Any) -> Any:
                    async def _chunks():
                        yield "I will run the selected tool."

                    return SimpleNamespace(
                        content_iterator=_chunks(),
                        get_final_usage=lambda: UsageData(
                            prompt_tokens=10,
                            completion_tokens=1,
                            total_tokens=11,
                            model="gpt-5.2",
                            provider="openai",
                            api_surface="responses",
                        ),
                    )

            return _Client()

    config = {
        "configurable": {
            "thread_id": "intent-test-thread",
            "runtime_services": SimpleNamespace(client_resolver=_Resolver()),
            "llm_runtime_selection": {
                "provider": "openai",
                "model": "gpt-5.2",
                "credential_ref": {"user_id": 10, "provider": "openai"},
                "reasoning_effort": None,
            },
            "runtime_projection": {
                "user_id": 10,
                "task_id": 99,
                "graph_thread_id": GRAPH_THREAD_ID,
                "tenant_id": 1,
                "runtime_placement_mode": "local",
                "workspace_id": "task-99",
                "workspace_path": "/tmp/task-99",
                "actor_type": "user",
                "actor_id": "10",
            },
        }
    }
    if hasattr(compiled, "ainvoke"):
        result = asyncio.run(compiled.ainvoke(initial, config=config))
    else:
        result = compiled.invoke(initial, config=config)
    return InteractiveState.from_mapping(result)


def test_guardrail_forces_safe_completion() -> None:
    metadata = {
        "intent_hints": {"safety": "refusal", "targets": ["10.10.10.10"]},
        "risk_flags": ["dangerous_shell_command"],
        "eligible_routes": ["normal_chat"],
        "forced_capability": "respond_only",
    }
    state = _build_state(
        message="Please run rm -rf /",
        metadata=metadata,
        builder=build_simple_tool_graph,
    )

    assert state.facts.capability == "respond_only"
    assert any("selected respond_only" in entry for entry in state.trace.reasoning)


def test_streaming_events_include_intent_summary() -> None:
    metadata = {
        "intent_hints": {
            "tool_hints": ["network_scan"],
            "targets": ["10.10.10.10"],
            "classifier_label": "tool_call",
            "classifier_confidence": 0.82,
        },
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "risk_flags": ["moderate_risk"],
    }
    state = _build_state(
        message="Scan 10.10.10.10",
        metadata=metadata,
        builder=build_simple_tool_graph,
    )

    adapter = LangGraphStreamingAdapter()
    summary_event = adapter.build_intent_summary_event(state, turn_id="stream-turn")
    assert summary_event is not None
    assert (
        summary_event["metadata"]["intent_summary"]["capability"]
        == "simple_tool_execution"
    )
    assert summary_event["metadata"]["intent_summary"]["tool_hints"] == ["network_scan"]
    assert summary_event["metadata"]["intent_summary"]["risk_flags"] == [
        "moderate_risk"
    ]

    tool_events = adapter.build_tool_events(state, turn_id="stream-turn")
    sequence = [summary_event, *tool_events]
    assert sequence[0]["metadata"]["subtype"] == "intent_summary"
    assert sequence[-1]["type"] == "assistant_final"


def test_tool_events_use_execution_summary() -> None:
    adapter = LangGraphStreamingAdapter()
    state = InteractiveState(
        facts=FactsState(
            task_id=7,
            message="Scan 10.0.0.7",
            capability="simple_tool_execution",
            metadata={
                "last_tool_result": {
                    "tool": "information_gathering.network_discovery.nmap",
                    "status": "success",
                    "stdout_excerpt": "Scan complete",
                    "stderr_excerpt": "",
                }
            },
        ),
        trace=TraceState(final_text="Scan complete"),
    )
    events = adapter.build_tool_events(state, turn_id="tool-turn")

    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_delta",
        "tool_end",
        "assistant_final",
    ]
    assert (
        events[0]["metadata"]["tool"] == "information_gathering.network_discovery.nmap"
    )
    assert events[0]["metadata"]["status"] == "in_progress"
    assert "completed" in events[1]["content"].lower()
    assert events[1]["metadata"]["status"] == "success"
    assert events[2]["metadata"]["status"] == "success"
    assert events[3]["metadata"]["subtype"] == "assistant_final"
    assert events[3]["metadata"]["internal_only"] is True


def test_tool_events_include_planner_reasoning() -> None:
    adapter = LangGraphStreamingAdapter()
    state = InteractiveState(
        facts=FactsState(
            task_id=9,
            message="Scan 10.0.0.9",
            capability="simple_tool_execution",
            metadata={
                "last_tool_result": {
                    "tool": "information_gathering.network_discovery.nmap",
                    "status": "success",
                    "stdout_excerpt": "done",
                    "stderr_excerpt": "",
                },
                "tool_history": [
                    {
                        "tool": "information_gathering.network_discovery.nmap",
                        "reasoning": ["Planner reasoning"],
                        "catalog": [
                            {
                                "tool_id": "information_gathering.network_discovery.nmap",
                                "name": "nmap",
                                "category": "network",
                                "description": "",
                            }
                        ],
                    }
                ],
            },
        ),
        trace=TraceState(final_text="done"),
    )

    events = adapter.build_tool_events(state, turn_id="planner-turn")

    for event in events:
        meta = event.get("metadata", {})
        assert meta.get("planner_reasoning") == ["Planner reasoning"]
        assert meta.get("tool_catalog")


def test_simple_chat_events_remain_final_only() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=11,
            message="Hello there",
            capability="respond_only",
            conversation_id=None,
            metadata={},
        ),
        trace=TraceState(final_text="Hello there!"),
    )

    adapter = LangGraphStreamingAdapter()
    event = adapter.build_final_event(state, turn_id="simple-event")

    assert event["type"] == "assistant_final"
    assert event["content"] == "Hello there!"
    assert event["metadata"]["id"] == "simple-event"
    assert event["metadata"]["subtype"] == "assistant_final"
    assert event["metadata"]["internal_only"] is True


def test_tool_events_emit_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    increments: list[tuple[str, int]] = []
    gauges: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.adapter.safe_inc",
        lambda name, value=1: increments.append((name, value)),
        raising=False,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.adapter.safe_gauge",
        lambda name, value: gauges.append((name, value)),
        raising=False,
    )

    adapter = LangGraphStreamingAdapter()
    state = InteractiveState(
        facts=FactsState(
            task_id=8,
            message="Run tool",
            capability="simple_tool_execution",
            metadata={
                "last_tool_result": {
                    "tool": "info.tool",
                    "status": "success",
                    "stdout_excerpt": "done",
                    "stderr_excerpt": "",
                    "duration": 0.25,
                }
            },
        ),
        trace=TraceState(final_text="done"),
    )

    adapter.build_tool_events(state, turn_id="tool-turn")

    assert ("langgraph_tool_runs", 1) in increments
    assert any(name == "langgraph_tool_latency_ms" for name, _ in gauges)


def test_intent_classifier_lightweight_load() -> None:
    response = """{
        "label": "simple_chat",
        "confidence": 0.55,
        "reasoning": "Low-risk acknowledgement."
    }"""

    class _StubClient:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(
            self, *, system_prompt: str, user_prompt: str, **kwargs: Any
        ) -> str:  # type: ignore[override]
            self.calls += 1
            return response

    def _runtime_config(run_id: int) -> Any:
        from backend.services.langgraph_chat.contracts import (
            ChatInputs,
            LangGraphRuntimeConfig,
            ExecutionMode,
        )
        from agent.graph.context.builder import (
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle,
        )

        chat_inputs = ChatInputs(
            task_id=run_id,
            user_id=run_id,
            message="Thanks!",
            conversation_id=f"conv-{run_id}",
            history=[],
            api_key="test-key",
            model="gpt-load-test",
        )
        return LangGraphRuntimeConfig(
            chat_inputs=chat_inputs,
            execution_mode=ExecutionMode.NORMAL_CHAT,
            metadata={
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id=chat_inputs.conversation_id,
                    turn_id=f"turn-{run_id}",
                    turn_sequence=run_id,
                    messages=chat_inputs.history,
                    current_message=chat_inputs.message,
                )
            },
        )

    stub_client = _StubClient()
    classifier = IntentClassifier(
        client_factory=lambda call_settings: stub_client
    )

    async def _run() -> None:
        for idx in range(5):
            config = _runtime_config(idx)
            await classifier.enrich_runtime_config(config)
            assert config.metadata["intent_classifier_label"] == "simple_chat"

    asyncio.run(_run())
    assert stub_client.calls == 5


def test_force_simple_chat_capability_override() -> None:
    metadata: Dict[str, Any] = {"force_simple_chat": True}
    bundle = collect_intent_signals(
        message="Run nmap 10.0.0.1", history=[], metadata=metadata
    )
    embed_intent_signals(metadata, bundle)

    payload = InteractiveInput(
        task_id=1, message="Run nmap 10.0.0.1", metadata=metadata
    )
    state = payload.to_state()

    from agent.graph.routers.intent_router import choose_capability

    capability, decisions = choose_capability(state)

    assert capability == "respond_only"
    assert decisions["considered"][0] == "respond_only"
    assert state.facts.metadata.get("forced_capability") == "respond_only"
    assert (
        state.facts.metadata.get("intent_hints", {}).get("forced_route")
        == "simple_chat"
    )

    config = LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=1,
            user_id=1,
            message="Run nmap 10.0.0.1",
            conversation_id=None,
            history=[],
            api_key=None,
            model=None,
        ),
        execution_mode=ExecutionMode.NORMAL_CHAT,
        metadata=metadata,
    )

    branch = select_branch(config)
    assert branch is ChatBranch.NORMAL_CHAT


def test_existing_suggested_capabilities_populate_metadata() -> None:
    metadata: Dict[str, Any] = {"suggested_capabilities": ["network scanning"]}
    bundle = collect_intent_signals(
        message="Use nmap on 10.0.0.5", history=[], metadata=metadata
    )
    embed_intent_signals(metadata, bundle)

    assert metadata.get("intent_capability") == "scan_ports"
    assert metadata.get("suggested_capabilities") == ["scan_ports"]
    assert metadata.get("intent_hints", {}).get("suggested_capabilities") == [
        "scan_ports"
    ]


def test_router_defaults_to_respond_only_without_inferred_capability() -> None:
    metadata: Dict[str, Any] = {}
    bundle = collect_intent_signals(
        message="Run gobuster against https://example.com",
        history=[],
        metadata=metadata,
    )
    embed_intent_signals(metadata, bundle)

    payload = InteractiveInput(
        task_id=3, message="Run gobuster against https://example.com", metadata=metadata
    )
    state = payload.to_state()

    from agent.graph.routers.intent_router import choose_capability

    capability, decisions = choose_capability(state)

    assert capability == "respond_only"
    assert decisions["considered"][0] == "respond_only"


def test_router_normalizes_direct_executor_alias_to_simple_tool_execution() -> None:
    metadata: Dict[str, Any] = {
        "intent_hints": {
            "classifier_label": "direct_executor",
            "classifier_confidence": 0.92,
            "tool_hints": [],
            "targets": [],
        },
        "eligible_routes": ["direct_executor"],
    }
    payload = InteractiveInput(
        task_id=7,
        message="Run a port scan on the host",
        metadata=metadata,
    )
    state = payload.to_state()

    from agent.graph.routers.intent_router import choose_capability

    capability, decisions = choose_capability(state)

    assert capability == "simple_tool_execution"
    assert decisions["considered"][0] == "simple_tool_execution"
    assert state.facts.capability == "simple_tool_execution"


def test_router_normalizes_plan_executor_alias_to_deep_reasoning() -> None:
    metadata: Dict[str, Any] = {
        "intent_hints": {
            "classifier_label": "plan_executor",
            "classifier_confidence": 0.92,
            "tool_hints": [],
            "targets": [],
        },
        "eligible_routes": ["plan_executor"],
    }
    payload = InteractiveInput(
        task_id=7,
        message="First scan the host then enumerate services",
        metadata=metadata,
    )
    state = payload.to_state()

    from agent.graph.routers.intent_router import choose_capability

    capability, decisions = choose_capability(state)

    assert capability == "deep_reasoning"
    assert decisions["considered"][0] == "deep_reasoning"
    assert state.facts.capability == "deep_reasoning"


def test_direct_executor_and_plan_executor_labels_route_to_distinct_branches() -> None:
    """Branch-selection regression guard for the direct-executor / DR split.

    Verifies that the two LLM-facing labels (``direct_executor`` and
    ``plan_executor``) select disjoint internal canonical capabilities
    (``simple_tool_execution`` vs ``deep_reasoning``) from the same router
    entry point across representative messages on both sides of the new
    routing boundary:

    - ``direct_executor`` must cover BOTH a one-shot single tool call AND a
      bounded multi-step chain that needs NO formal planning (e.g., a
      conditional "ping then if online nmap"). The new direct executor is a
      bounded progressive executor, not a one-shot wrapper.
    - ``plan_executor`` must cover requests that genuinely require upfront
      decomposition into a tracked plan (e.g., a phased full engagement).

    The router is label-driven — the message text is decoration — so this
    test pins the runtime CONTRACT: when the classifier produces these
    labels for these representative messages, the runtime must map them to
    the correct canonical capability. The prompt-level regression (which
    labels the classifier SHOULD produce) lives in
    ``core/prompts/tests`` and ``test_intent_classifier.py``.
    """
    from agent.graph.routers.intent_router import choose_capability

    def _route(label: str, message: str) -> str:
        metadata: Dict[str, Any] = {
            "intent_hints": {
                "classifier_label": label,
                "classifier_confidence": 0.9,
                "tool_hints": [],
                "targets": [],
            },
            "eligible_routes": [label],
        }
        payload = InteractiveInput(task_id=1, message=message, metadata=metadata)
        state = payload.to_state()
        capability, _decisions = choose_capability(state)
        return capability

    # direct_executor side: one-shot AND bounded multi-step-without-planning.
    direct_cases = [
        ("direct_executor", "Scan 127.0.0.1 with nmap"),  # one-shot
        ("direct_executor", "Ping 10.0.0.5 and if online run nmap on it"),
        ("direct_executor", "Resolve example.com then whois the resulting IP"),
    ]
    for label, msg in direct_cases:
        assert _route(label, msg) == "simple_tool_execution", (
            f"direct_executor must route to simple_tool_execution for: {msg!r}"
        )

    # plan_executor side: requests that genuinely need upfront decomposition.
    planner_cases = [
        (
            "plan_executor",
            "Do a full pentest of 10.0.0.0/24: recon hosts, enumerate "
            "services, check known vulns, and produce a report.",
        ),
        (
            "plan_executor",
            "Run a full external assessment on example.com across web, "
            "DNS, and network surfaces.",
        ),
    ]
    for label, msg in planner_cases:
        assert _route(label, msg) == "deep_reasoning", (
            f"plan_executor must route to deep_reasoning for: {msg!r}"
        )

    # Contract: the two labels must never collapse to the same capability.
    direct_caps = {_route(label, msg) for label, msg in direct_cases}
    planner_caps = {_route(label, msg) for label, msg in planner_cases}
    assert direct_caps.isdisjoint(planner_caps)


def test_router_honors_graph_entry_override_for_plan_against_simple_chat_label() -> (
    None
):
    """Graph-entry override beats classifier `simple_chat` label.

    Regression for the deep-reasoning graph-entry path in Plan mode:
    even when the intent classifier label says ``simple_chat`` (and the
    eligible_routes only contain ``normal_chat``), the router must
    pick ``deep_reasoning`` because the facade has already resolved
    the user-surface tier and propagated the graph-entry override
    derived from ``execution_route_policy``. Without this short-circuit
    the deep-reasoning graph would self-route to ``respond_only`` and
    fall through ``fallback_finalize`` instead of running the planner.
    """
    from agent.graph.routers.intent_router import choose_capability

    metadata: Dict[str, Any] = {
        # Classifier disagrees with the user-surface tier — Plan must
        # still win at graph entry because the facade picked
        # DeepReasoningHandler from the route policy.
        "intent_hints": {
            "classifier_label": "simple_chat",
            "classifier_confidence": 0.95,
            "tool_hints": [],
            "targets": [],
        },
        "eligible_routes": ["normal_chat"],
        "intent_router_graph_entry_override": "deep_reasoning",
    }
    payload = InteractiveInput(
        task_id=99,
        message="plan a multi-step engagement",
        metadata=metadata,
    )
    state = payload.to_state()

    capability, decisions = choose_capability(state)

    assert capability == "deep_reasoning"
    assert decisions["graph_entry_override"] == ["deep_reasoning"]
    assert state.facts.capability == "deep_reasoning"


def test_router_graph_entry_override_absent_keeps_classifier_label() -> None:
    """Negative control: no graph-entry override means classifier label wins.

    Pins that the override branch only fires when the metadata key is
    actually set. Without this check, a regression that always treats
    the override slot as authoritative would silently corrupt the
    `agent` / `agent_full` paths (which never set the key).
    """
    from agent.graph.routers.intent_router import choose_capability

    metadata: Dict[str, Any] = {
        "intent_hints": {
            "classifier_label": "simple_chat",
            "classifier_confidence": 0.9,
            "tool_hints": [],
            "targets": [],
        },
        "eligible_routes": ["normal_chat"],
    }
    payload = InteractiveInput(
        task_id=100,
        message="just chat",
        metadata=metadata,
    )
    state = payload.to_state()

    capability, decisions = choose_capability(state)
    # `simple_chat` -> `respond_only` per `_normalize_capability` table.
    assert capability == "respond_only"
    assert "graph_entry_override" not in decisions


def test_simple_chat_graph_applies_preset_result() -> None:
    metadata: Dict[str, Any] = {
        "simple_chat_runtime": {
            "result": {"content": "Hello there!", "conversation_id": "conv-simple"},
        }
    }
    payload = InteractiveInput(task_id=5, message="Hi!", metadata=metadata)
    initial = build_initial_state(payload)

    compiled = build_simple_chat_graph(checkpointer=get_default_checkpointer())

    if hasattr(compiled, "ainvoke"):

        async def _run() -> Any:
            return await compiled.ainvoke(
                initial, config={"configurable": {"thread_id": "simple-chat"}}
            )

        result = asyncio.run(_run())
    else:
        result = compiled.invoke(
            initial, config={"configurable": {"thread_id": "simple-chat"}}
        )

    state = InteractiveState.from_mapping(result)
    assert state.trace.final_text == "Hello there!"
    assert state.facts.conversation_id == "conv-simple"
    assert "simple_chat_runtime" not in state.facts.metadata
    assert any("post-processing applied" in entry for entry in state.trace.reasoning)


def test_simple_chat_streaming_events_final_only() -> None:
    metadata: Dict[str, Any] = {
        "simple_chat_runtime": {
            "result": {"content": "Trimmed text ", "conversation_id": "conv-simple"},
        }
    }
    payload = InteractiveInput(task_id=7, message="Hi!", metadata=metadata)
    initial = build_initial_state(payload)

    compiled = build_simple_chat_graph(checkpointer=get_default_checkpointer())
    if hasattr(compiled, "ainvoke"):

        async def _run() -> Any:
            return await compiled.ainvoke(
                initial, config={"configurable": {"thread_id": "simple-stream"}}
            )

        result = asyncio.run(_run())
    else:
        result = compiled.invoke(
            initial, config={"configurable": {"thread_id": "simple-stream"}}
        )

    state = InteractiveState.from_mapping(result)
    adapter = LangGraphStreamingAdapter()
    events = adapter.build_simple_chat_events(state, turn_id="simple-stream")

    assert len(events) == 1
    final = events[0]
    assert final["type"] == "assistant_final"
    assert final["metadata"]["role"] == "assistant"
    assert final["metadata"]["streaming"] is False
    assert final["content"] == "Trimmed text"


@pytest.mark.asyncio
async def test_simple_chat_node_handles_generation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingLLMClient:
        async def chat_messages(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("boom")

        async def chat_messages_with_usage(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        async def stream_chat_messages(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        async def stream_chat_messages_with_usage(
            self, *args: Any, **kwargs: Any
        ) -> Any:
            raise RuntimeError("boom")

    def _resolve_failing_client(*args: Any, **kwargs: Any) -> _FailingLLMClient:
        return _FailingLLMClient()

    monkeypatch.setattr(
        "agent.graph.nodes.simple_chat.resolve_llm_client",
        _resolve_failing_client,
    )

    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    bundle = build_conversation_context_bundle(
        conversation_id="conv-error",
        turn_id="conv-error-turn-0",
        turn_sequence=0,
        messages=[],
    )
    payload = InteractiveInput(
        task_id=8,
        message="Hello?",
        metadata={
            "simple_chat_runtime": {"api_key": "k", "model": "stub"},
            METADATA_CONTEXT_BUNDLE_KEY: bundle,
        },
    )
    state = payload.to_state()

    result = await run_simple_chat(state.as_graph_state())
    updated = InteractiveState.from_mapping(result)

    assert updated.trace.final_text == ""
    assert updated.trace.final_error == "boom"
    assert any("failed" in entry for entry in updated.trace.reasoning)


@pytest.mark.asyncio
async def test_run_langgraph_generation_streams_simple_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    streaming_changes: List[tuple[int, bool]] = []
    increments: List[tuple[str, int]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            streaming_changes.append((task_id, state))

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    stub_hub = _StubHub()

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: stub_hub
    )

    def _fake_safe_inc(name: str, value: int = 1) -> None:
        increments.append((name, value))

    monkeypatch.setattr(
        "backend.services.metrics.utils.safe_inc", _fake_safe_inc, raising=False
    )

    stream_events = [
        {
            "type": "assistant_final",
            "content": "Hi there",
            "metadata": {"role": "assistant", "streaming": False},
        },
    ]

    async def _event_generator():
        for event in stream_events:
            yield dict(event)

    def _event_iterator():
        return _event_generator()

    facts = FactsState(
        task_id=77,
        message="Hello",
        conversation_id="conv-77",
        capability="respond_only",
    )
    trace = TraceState(final_text="Hi there")
    interactive_state = InteractiveState(facts=facts, trace=trace)

    result = LangGraphChatResult(
        final_text="Hi there",
        conversation_id="conv-77",
        interactive_state=interactive_state,
        metadata={"role": "assistant", "streaming": False},
        _event_iterator=_event_iterator,
    )

    async def _fake_handle_turn(
        chat_inputs: ChatInputs, metadata: Dict[str, Any] | None = None, **_kwargs: Any
    ):
        return result

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_handle_turn),
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-77-msg-1", 1),
    )
    await run_langgraph_generation(
        task_id=77,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-77",
        history=[],
        anchor_sequence=None,
        requested_mode=None,
    )

    await asyncio.sleep(0)  # allow scheduled tasks to run

    assert streaming_changes[0] == (77, True)
    assert streaming_changes[-1] == (77, False)
    assert len(published) == 2
    snapshot_event = published[0]["event"]
    sentinel_event = published[1]["event"]
    assert snapshot_event["type"] == "message_delta"
    assert snapshot_event["content"] == "Hi there"
    assert snapshot_event["metadata"]["streaming"] is False
    assert snapshot_event["metadata"]["final_snapshot"] is True
    assert snapshot_event["metadata"]["boundary_source"] == "turn_boundary"
    assert snapshot_event["metadata"]["id"] == "task-77-msg-1"
    assert snapshot_event["metadata"]["turn_sequence"] == 1
    assert sentinel_event["type"] == "assistant_final"
    assert sentinel_event["metadata"]["internal_only"] is True
    assert sentinel_event["metadata"]["id"] == "task-77-msg-1"
    assert sentinel_event["metadata"]["turn_sequence"] == 1
    increment_names = [name for name, _ in increments]
    assert "final_messages_persisted" in increment_names
    assert "final_message_chars" in increment_names


@pytest.mark.asyncio
async def test_start_turn_generation_reserve_failure_routes_to_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    streaming_changes: List[tuple[int, bool]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            streaming_changes.append((task_id, state))

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    stub_hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: stub_hub
    )

    handle_turn_mock = AsyncMock(
        side_effect=RuntimeError("must not call handle_turn on reservation failure")
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        handle_turn_mock,
    )
    terminal_mock = AsyncMock(return_value=None)

    service = TurnExecutionService()
    service._error_service.handle_terminal_turn_error = terminal_mock
    await service.start_turn_generation(
        task_id=901,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-reserve-fail",
        history=[],
        reserve_chat_turn=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("reserve failed")
        ),
        start_turn_workflow=lambda **kwargs: 9011,
        turn_id="task-901-msg-1",
    )

    assert streaming_changes == [(901, True), (901, False)]
    assert published == []
    handle_turn_mock.assert_not_awaited()
    terminal_mock.assert_awaited_once()
    terminal_kwargs = terminal_mock.await_args.kwargs
    assert terminal_kwargs["failure_source"] == "initial_generation"
    assert terminal_kwargs["error_code"] == "generation_failed"
    assert terminal_kwargs["workflow_id"] is None


@pytest.mark.asyncio
async def test_start_compression_refusal_uses_declined_boundary_and_failed_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wired start compression branch preserves refusal outcome semantics."""
    lifecycle = _RefusalLifecycle()
    failed_workflows: List[Dict[str, Any]] = []
    boundary_calls: List[Dict[str, Any]] = []
    increments: List[str] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    refusal = LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(
            provider="openai",
            model="gpt-4o-mini",
            category="content_filter",
        ),
    )
    compression_exc = CompressionRequiredError("compression_required")
    compression_exc.__cause__ = refusal
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=compression_exc),
    )
    monkeypatch.setattr(
        "backend.services.metrics.utils.safe_inc",
        lambda name, *_args, **_kwargs: increments.append(name),
    )

    service = TurnExecutionService()

    async def _capture_boundary(**kwargs: Any) -> None:
        boundary_calls.append(kwargs)

    service._publish_boundary_completion_events = _capture_boundary  # type: ignore[method-assign]
    await service.start_turn_generation(
        task_id=902,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-start-refusal",
        history=[],
        reserve_chat_turn=lambda *args, **kwargs: (
            1,
            2,
            "task-902-turn-1",
            1,
        ),
        start_turn_workflow=lambda **_kwargs: 9021,
        turn_id="task-902-turn-1",
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
    )

    assert len(failed_workflows) == 1
    metadata = failed_workflows[0]["metadata"]
    assert metadata["outcome_type"] == "provider_refusal"
    assert metadata["retryable"] is False
    assert len(boundary_calls) == 1
    boundary_metadata = boundary_calls[0]["base_metadata"]
    assert boundary_metadata["status"] == "declined"
    assert boundary_metadata["stop_reason"] == "refusal"
    assert "error_code" not in boundary_metadata
    assert lifecycle.end_calls[-1]["status"] == "failed"
    assert "langgraph_simple_chat_errors" not in increments


@pytest.mark.asyncio
async def test_start_refusal_skips_generic_error_metric_and_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct provider refusal is terminal but not a generic start error."""
    lifecycle = _RefusalLifecycle()
    failed_workflows: List[Dict[str, Any]] = []
    increments: List[str] = []
    generic_error_log = Mock()

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    refusal = LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(
            provider="openai",
            model="gpt-4o-mini",
            category="content_filter",
        ),
    )
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=refusal),
    )
    monkeypatch.setattr(
        "backend.services.metrics.utils.safe_inc",
        lambda name, *_args, **_kwargs: increments.append(name),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.logger.exception",
        generic_error_log,
    )

    service = TurnExecutionService()
    service._publish_boundary_completion_events = AsyncMock()  # type: ignore[method-assign]
    await service.start_turn_generation(
        task_id=903,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-start-direct-refusal",
        history=[],
        reserve_chat_turn=lambda *args, **kwargs: (
            1,
            2,
            "task-903-turn-1",
            1,
        ),
        start_turn_workflow=lambda **_kwargs: 9031,
        turn_id="task-903-turn-1",
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
    )

    assert failed_workflows[0]["metadata"]["outcome_type"] == "provider_refusal"
    assert "langgraph_simple_chat_errors" not in increments
    generic_error_log.assert_not_called()


@pytest.mark.asyncio
async def test_run_langgraph_generation_streaming_events_are_published_without_prefetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    stream_done: Dict[str, bool] = {"done": False}
    stream_marker: Dict[str, bool | None] = {"first_nonfinal_after_completion": None}

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})
            if event["type"] == "message_delta" and not event["metadata"].get(
                "final_snapshot"
            ):
                stream_marker["first_nonfinal_after_completion"] = stream_done["done"]

    stream_events = [
        {
            "type": "message_delta",
            "content": "chunk",
            "metadata": {"role": "assistant", "streaming": True},
        },
        {
            "type": "assistant_final",
            "content": "Hi there",
            "metadata": {"role": "assistant", "streaming": False},
        },
    ]

    async def _event_generator():
        for event in stream_events:
            yield dict(event)
        stream_done["done"] = True

    def _event_iterator():
        return _event_generator()

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(
            return_value=LangGraphChatResult(
                final_text="Hi there",
                conversation_id="conv-87",
                metadata={"role": "assistant", "streaming": False},
                _event_iterator=_event_iterator,
            )
        ),
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-87-msg-1", 1),
    )

    await run_langgraph_generation(
        task_id=87,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-87",
        history=[],
        anchor_sequence=1,
        requested_mode=None,
    )

    assert stream_marker["first_nonfinal_after_completion"] is False
    assert len(published) == 3
    assert published[0]["event"]["type"] == "message_delta"
    assert published[0]["event"]["content"] == "chunk"
    assert published[1]["event"]["metadata"]["final_snapshot"] is True
    assert published[2]["event"]["type"] == "assistant_final"


@pytest.mark.asyncio
async def test_run_langgraph_generation_ceiling_reached_emits_status_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    context_events: List[Dict[str, Any]] = []
    captured_context_handoffs: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-78-msg-1", 1),
    )

    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=78,
            conversation_id="conv-78",
            max_tokens=128_000,
            used_tokens=128_000,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    compression_request = ContextCompressionRequest(
        task_id=78,
        conversation_id="conv-78",
        max_tokens=128_000,
        model="model",
        conversation_history=[],
        projected_user_message="Hello",
    )
    pass_result = CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text="compressed snapshot",
        output_tokens=600,
        target_max_tokens=38400,
        within_target=True,
    )
    compression_outcome = ContextCompressionOutcome(
        request=compression_request,
        original_tokens=128000,
        final_tokens=600,
        final_text="compressed snapshot",
        pass_results=(pass_result,),
        pass_count=1,
        degraded=False,
        fallback_reason=None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.compress",
        AsyncMock(return_value=compression_outcome),
    )

    result = LangGraphChatResult(
        final_text="Still runs",
        conversation_id="conv-78",
        metadata={"role": "assistant", "streaming": False},
        _event_iterator=lambda: _empty_async_iter(),
    )

    service = TurnExecutionService()

    async def _fake_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
        )
        captured_context_handoffs.append(dict(pre_classifier_context_handoff))
        return result

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_handle_turn),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.get_turn_execution_service",
        lambda: service,
    )

    await run_langgraph_generation(
        task_id=78,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-78",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
    )

    assert len(context_events) == 1
    assert context_events[0]["ceiling_reached"] is True
    assert context_events[0]["recommended_next_action"] == "compress"
    assert context_events[0]["compression_candidate"] is True
    assert len(captured_context_handoffs) == 1
    context_window = captured_context_handoffs[0]["context_window"]
    assert context_window["ceiling_reached"] is True
    assert context_window["recommended_next_action"] == "compress"
    assert context_window["compression_candidate"] is True
    assert len(published) == 2


@pytest.mark.asyncio
async def test_run_langgraph_generation_non_ceiling_emits_context_window_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_events: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-780-msg-1", 1),
    )

    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=780,
            conversation_id="conv-780",
            max_tokens=128_000,
            used_tokens=640,
            remaining_tokens=127_360,
            ratio=0.005,
            ceiling_reached=False,
        ),
        ceiling_reached=False,
        recommended_next_action="none",
        compression_candidate=False,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )

    result = LangGraphChatResult(
        final_text="Normal turn",
        conversation_id="conv-780",
        metadata={"role": "assistant", "streaming": False},
        _event_iterator=lambda: _empty_async_iter(),
    )
    service = TurnExecutionService()

    async def _fake_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
        )
        return result

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_handle_turn),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.get_turn_execution_service",
        lambda: service,
    )

    await run_langgraph_generation(
        task_id=780,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-780",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
    )

    assert len(context_events) == 1
    assert context_events[0]["task_id"] == 780
    assert context_events[0]["conversation_id"] == "conv-780"
    assert context_events[0]["ceiling_reached"] is False
    assert context_events[0]["recommended_next_action"] == "none"
    assert context_events[0]["compression_candidate"] is False


@pytest.mark.asyncio
async def test_run_langgraph_generation_ceiling_reached_compresses_inside_facade_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: List[str] = []
    captured_histories: List[List[Dict[str, Any]]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-781-msg-1", 1),
    )
    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=781,
            conversation_id="conv-781",
            max_tokens=128_000,
            used_tokens=128_050,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )

    compression_request = ContextCompressionRequest(
        task_id=781,
        conversation_id="conv-781",
        max_tokens=128_000,
        model="model",
        conversation_history=[{"role": "assistant", "content": "original"}],
        projected_user_message="Hello",
    )
    pass_result = CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text="compressed snapshot",
        output_tokens=1000,
        target_max_tokens=38400,
        within_target=True,
    )
    compression_outcome = ContextCompressionOutcome(
        request=compression_request,
        original_tokens=128050,
        final_tokens=1000,
        final_text="compressed snapshot",
        pass_results=(pass_result,),
        pass_count=1,
        degraded=False,
        fallback_reason=None,
    )

    async def _fake_compress(*args: Any, **kwargs: Any) -> ContextCompressionOutcome:
        call_order.append("compress")
        return compression_outcome

    service = TurnExecutionService()

    async def _fake_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        call_order.append("facade_enter")
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
        )
        call_order.append("classifier_boundary")
        captured_histories.append(list(chat_inputs.history))
        return LangGraphChatResult(
            final_text="compressed turn",
            conversation_id="conv-781",
            metadata={"role": "assistant", "streaming": False},
            _event_iterator=lambda: _empty_async_iter(),
        )

    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.compress",
        AsyncMock(side_effect=_fake_compress),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_handle_turn),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.get_turn_execution_service",
        lambda: service,
    )

    await run_langgraph_generation(
        task_id=781,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-781",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
    )

    assert call_order[:3] == ["facade_enter", "compress", "classifier_boundary"]
    assert len(captured_histories) == 1
    assert captured_histories[0] == _FACADE_CONTEXT_HISTORY


@pytest.mark.asyncio
async def test_run_langgraph_generation_passes_compression_status_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_context_handoffs: List[Dict[str, Any]] = []
    context_events: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-782-msg-1", 1),
    )
    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=782,
            conversation_id="conv-782",
            max_tokens=128_000,
            used_tokens=128_000,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )

    compression_request = ContextCompressionRequest(
        task_id=782,
        conversation_id="conv-782",
        max_tokens=128_000,
        model="model",
        conversation_history=[],
        projected_user_message="Hello",
    )
    pass_result = CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text="compressed snapshot",
        output_tokens=800,
        target_max_tokens=38400,
        within_target=True,
    )
    compression_outcome = ContextCompressionOutcome(
        request=compression_request,
        original_tokens=128000,
        final_tokens=800,
        final_text="compressed snapshot",
        pass_results=(pass_result,),
        pass_count=1,
        degraded=False,
        fallback_reason=None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.compress",
        AsyncMock(return_value=compression_outcome),
    )

    service = TurnExecutionService()

    async def _fake_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
        )
        captured_context_handoffs.append(dict(pre_classifier_context_handoff))
        return LangGraphChatResult(
            final_text="ok",
            conversation_id="conv-782",
            metadata={"role": "assistant", "streaming": False},
            _event_iterator=lambda: _empty_async_iter(),
        )

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_handle_turn),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.get_turn_execution_service",
        lambda: service,
    )

    await run_langgraph_generation(
        task_id=782,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-782",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
    )

    assert len(captured_context_handoffs) == 1
    compression_meta = captured_context_handoffs[0]["compression"]
    assert compression_meta["applied"] is True
    assert compression_meta["pass_count"] == 1
    assert compression_meta["final_tokens"] == 800
    assert compression_meta["source_tokens"] == 128000
    assert compression_meta["epoch_id"] == _build_compression_epoch_id(
        task_id=782,
        conversation_id="conv-782",
        source_tokens=128_000,
        source_message_ids=_FACADE_CONTEXT_SOURCE_IDS,
    )
    assert len(context_events) == 1
    assert context_events[0]["compression_pass_count"] == 1
    assert context_events[0]["compression_tokens_before"] == 128000
    assert context_events[0]["compression_tokens_after"] == 800
    assert context_events[0]["compression_degraded"] is False


@pytest.mark.asyncio
async def test_start_turn_generation_completion_metadata_includes_compression_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=783,
            conversation_id="conv-783",
            max_tokens=128_000,
            used_tokens=128_220,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )

    compression_request = ContextCompressionRequest(
        task_id=783,
        conversation_id="conv-783",
        max_tokens=128_000,
        model="model",
        conversation_history=[],
        projected_user_message="Hello",
    )
    pass_result = CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text="compressed snapshot",
        output_tokens=900,
        target_max_tokens=38400,
        within_target=True,
    )
    compression_outcome = ContextCompressionOutcome(
        request=compression_request,
        original_tokens=128220,
        final_tokens=900,
        final_text="compressed snapshot",
        pass_results=(pass_result,),
        pass_count=1,
        degraded=False,
        fallback_reason=None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.compress",
        AsyncMock(return_value=compression_outcome),
    )
    service = TurnExecutionService()
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(
            side_effect=_commit_facade_handler(service, "conv-783")
        ),
    )

    await service.start_turn_generation(
        task_id=783,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-783",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
        reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-783-msg-1", 1),
        start_turn_workflow=lambda **kwargs: 1001,
        mark_turn_workflow_completed=lambda **kwargs: completed_calls.append(kwargs),
    )

    assert len(completed_calls) == 1
    completion_metadata = completed_calls[0]["metadata"]
    assert completion_metadata["compression_applied"] is True
    assert completion_metadata["compression"]["applied"] is True
    assert completion_metadata["compression"]["pass_count"] == 1
    assert completion_metadata["compression"]["epoch_id"] == (
        _build_compression_epoch_id(
            task_id=783,
            conversation_id="conv-783",
            source_tokens=128_220,
            source_message_ids=_FACADE_CONTEXT_SOURCE_IDS,
        )
    )
    assert completion_metadata["compression"]["source_tokens"] == 128220


@pytest.mark.asyncio
async def test_start_turn_generation_ceiling_fails_closed_when_compression_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_events: List[Dict[str, Any]] = []
    failed_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.is_enabled",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=785,
            conversation_id="conv-785",
            max_tokens=128_000,
            used_tokens=128_000,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )
    async def _fake_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
        )
        raise AssertionError("disabled compression must fail before a result")

    facade_mock = AsyncMock(side_effect=_fake_handle_turn)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        facade_mock,
    )

    service = TurnExecutionService()
    await service.start_turn_generation(
        task_id=785,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-785",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
        reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-785-msg-1", 1),
        start_turn_workflow=lambda **kwargs: 7851,
        mark_turn_workflow_failed=lambda **kwargs: failed_calls.append(kwargs),
    )

    assert len(context_events) == 1
    assert context_events[0]["ceiling_reached"] is True
    assert context_events[0]["recommended_next_action"] == "compress"
    assert context_events[0]["compression_candidate"] is True
    assert facade_mock.await_count == 1
    assert len(failed_calls) == 1
    assert failed_calls[0]["metadata"]["error"] == "compression_required_failed"


@pytest.mark.asyncio
async def test_start_turn_generation_ceiling_compression_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_events: List[Dict[str, Any]] = []
    failed_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=786,
            conversation_id="conv-786",
            max_tokens=128_000,
            used_tokens=128_200,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.compress",
        AsyncMock(side_effect=RuntimeError("compress boom")),
    )
    async def _fake_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
        )
        raise AssertionError("failed compression must fail before a result")

    facade_mock = AsyncMock(side_effect=_fake_handle_turn)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        facade_mock,
    )

    service = TurnExecutionService()
    await service.start_turn_generation(
        task_id=786,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-786",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
        reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-786-msg-1", 1),
        start_turn_workflow=lambda **kwargs: 7861,
        mark_turn_workflow_failed=lambda **kwargs: failed_calls.append(kwargs),
    )

    assert len(context_events) == 1
    assert context_events[0]["ceiling_reached"] is True
    assert context_events[0]["recommended_next_action"] == "compress"
    assert context_events[0]["compression_candidate"] is True
    assert facade_mock.await_count == 1
    assert len(failed_calls) == 1
    assert failed_calls[0]["metadata"]["error"] == "compression_required_failed"


@pytest.mark.asyncio
async def test_start_turn_generation_does_not_recompress_at_success_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: List[str] = []
    completed_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=787,
            conversation_id="conv-787",
            max_tokens=128_000,
            used_tokens=128_220,
            remaining_tokens=0,
            ratio=1.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(decision),
    )

    preturn_request = ContextCompressionRequest(
        task_id=787,
        conversation_id="conv-787",
        max_tokens=128_000,
        model="model",
        conversation_history=[],
        projected_user_message="hello",
    )
    preturn_pass = CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text="compressed snapshot",
        output_tokens=900,
        target_max_tokens=38400,
        within_target=True,
    )
    preturn_outcome = ContextCompressionOutcome(
        request=preturn_request,
        original_tokens=128220,
        final_tokens=900,
        final_text="compressed snapshot",
        pass_results=(preturn_pass,),
        pass_count=1,
        degraded=False,
        fallback_reason=None,
    )

    async def _fake_compress(*args: Any, **kwargs: Any) -> ContextCompressionOutcome:
        call_order.append("compress")
        return preturn_outcome

    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextCompressionService.compress",
        AsyncMock(side_effect=_fake_compress),
    )

    persisted_calls: List[Dict[str, Any]] = []

    def _persist_snapshot(*args: Any, **kwargs: Any) -> None:
        call_order.append("persist_snapshot")
        persisted_calls.append(kwargs)

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.CompressionSnapshotRepository.persist_snapshot",
        _persist_snapshot,
    )

    async def _fake_publish_boundary(*args: Any, **kwargs: Any) -> None:
        call_order.append("boundary_publish")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.TurnExecutionService._publish_boundary_completion_events",
        AsyncMock(side_effect=_fake_publish_boundary),
    )
    service = TurnExecutionService()
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(
            side_effect=_commit_facade_handler(service, "conv-787")
        ),
    )

    await service.start_turn_generation(
        task_id=787,
        user_id=10,
        api_key="key",
        model="model",
        message="hello",
        conversation_id="conv-787",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
        reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-787-msg-1", 1),
        start_turn_workflow=lambda **kwargs: 7871,
        mark_turn_workflow_completed=lambda **kwargs: (
            call_order.append("workflow_completed"),
            completed_calls.append(kwargs),
        ),
    )

    # The pre-classifier compaction is the only compressor authority for one
    # user turn; successful completion must not invoke a second compression.
    assert call_order.count("compress") == 1
    assert persisted_calls == []
    assert len(completed_calls) == 1


@pytest.mark.asyncio
async def test_start_turn_failure_persists_already_streamed_measured_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload hydration can recover the same snapshot after graph failure."""
    failed_calls: List[Dict[str, Any]] = []
    context_events: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )

    service = TurnExecutionService()

    async def _fail_after_measurement(
        _chat_inputs: ChatInputs,
        *,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        snapshot = {
            "ceiling_reached": False,
            "recommended_next_action": "none",
            "compression_candidate": False,
            "max_tokens": 32_768,
            "used_tokens": 8_723,
            "remaining_tokens": 24_045,
            "ratio": 8_723 / 32_768,
            "conversation_id": "conv-failed-measurement",
            "turn_sequence": 1,
            "revision": 1,
            "snapshot_kind": "measured",
        }
        pre_classifier_context_handoff["context_window"] = snapshot
        service._emit_context_window_event(790, snapshot)
        raise RuntimeError("graph failed after context measurement")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fail_after_measurement),
    )

    await service.start_turn_generation(
        task_id=790,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-failed-measurement",
        history=[],
        reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-790-msg-1", 1),
        start_turn_workflow=lambda **kwargs: 7901,
        mark_turn_workflow_failed=lambda **kwargs: failed_calls.append(kwargs),
    )

    assert len(context_events) == 1
    assert len(failed_calls) == 1
    failed_snapshot = failed_calls[0]["metadata"]["context_window"]
    assert failed_snapshot["used_tokens"] == 8_723
    assert failed_snapshot["revision"] == 1
    assert failed_snapshot["snapshot_kind"] == "measured"


@pytest.mark.asyncio
async def test_start_turn_cancellation_persists_captured_measured_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot discard a measurement captured before completion."""
    failed_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )

    async def _cancel_after_measurement(
        _chat_inputs: ChatInputs,
        *,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        pre_classifier_context_handoff["context_window"] = {
            "ceiling_reached": False,
            "recommended_next_action": "none",
            "compression_candidate": False,
            "max_tokens": 32_768,
            "used_tokens": 8_723,
            "remaining_tokens": 24_045,
            "ratio": 8_723 / 32_768,
            "conversation_id": "conv-cancelled-measurement",
            "turn_sequence": 1,
            "revision": 1,
            "snapshot_kind": "measured",
        }
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_cancel_after_measurement),
    )

    service = TurnExecutionService()
    with pytest.raises(asyncio.CancelledError):
        await service.start_turn_generation(
            task_id=791,
            user_id=10,
            api_key="key",
            model="model",
            message="Hello",
            conversation_id="conv-cancelled-measurement",
            history=[],
            reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-791-msg-1", 1),
            start_turn_workflow=lambda **kwargs: 7911,
            mark_turn_workflow_failed=lambda **kwargs: failed_calls.append(kwargs),
        )

    assert len(failed_calls) == 1
    failed_metadata = failed_calls[0]["metadata"]
    assert failed_metadata["error"] == "run_cancelled"
    assert failed_metadata["context_window"]["used_tokens"] == 8_723
    assert failed_metadata["context_window"]["revision"] == 1


@pytest.mark.asyncio
async def test_start_turn_generation_keeps_preturn_snapshot_authoritative_over_checkpoint_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    context_events: List[Dict[str, Any]] = []
    completed_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )

    pre_turn_decision = ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=79,
            conversation_id="conv-79",
            max_tokens=128_000,
            used_tokens=100,
            remaining_tokens=127_900,
            ratio=100 / 128_000,
            ceiling_reached=False,
        ),
        ceiling_reached=False,
        recommended_next_action="none",
        compression_candidate=False,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service.ContextWindowManager.evaluate_classifier_prompt",
        lambda *args, **kwargs: _decision_with_prompt_budget(pre_turn_decision),
    )

    result = LangGraphChatResult(
        final_text="checkpoint-mapped",
        conversation_id="conv-79",
        metadata={
            "role": "assistant",
            "streaming": False,
            "context_window": {
                "ceiling_reached": True,
                "recommended_next_action": "compress",
                "compression_candidate": True,
                "max_tokens": 128_000,
                "used_tokens": 128_123,
                "remaining_tokens": 0,
                "ratio": 1.0,
                "conversation_id": "conv-79",
            },
        },
        _event_iterator=lambda: _empty_async_iter(),
    )
    service = TurnExecutionService()

    async def _fake_checkpoint_handle_turn(
        chat_inputs: ChatInputs,
        *,
        runtime_services: Any,
        pre_classifier_context_handoff: Dict[str, Any],
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        await _run_facade_context_decision(
            service=service,
            chat_inputs=chat_inputs,
            runtime_services=runtime_services,
            handoff=pre_classifier_context_handoff,
            turn_sequence=_kwargs["metadata"]["turn_sequence"],
        )
        return result

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_checkpoint_handle_turn),
    )

    await service.start_turn_generation(
        task_id=79,
        user_id=10,
        api_key="key",
        model="model",
        message="hello",
        conversation_id="conv-79",
        history=list(_FACADE_CONTEXT_HISTORY),
        history_source_message_ids=list(_FACADE_CONTEXT_SOURCE_IDS),
        reserve_chat_turn=lambda *args, **kwargs: (1, 2, "task-79-msg-1", 1),
        start_turn_workflow=lambda **kwargs: 999,
        mark_turn_workflow_completed=lambda **kwargs: completed_calls.append(kwargs),
    )

    assert len(context_events) == 1
    assert context_events[0]["ceiling_reached"] is False
    assert context_events[0]["recommended_next_action"] == "none"
    assert context_events[0]["compression_candidate"] is False
    assert context_events[0]["turn_sequence"] == 1
    assert context_events[0]["revision"] == 1
    assert context_events[0]["snapshot_kind"] == "measured"

    assert len(completed_calls) == 1
    completion_metadata = completed_calls[0]["metadata"]
    assert completion_metadata["completion_source"] == "initial_generation"
    assert completion_metadata["ceiling_reached"] is False
    assert completion_metadata["recommended_next_action"] == "none"
    assert completion_metadata["compression_candidate"] is False
    assert completion_metadata["context_window"]["used_tokens"] == 100
    assert completion_metadata["context_window"]["conversation_id"] == "conv-79"
    assert completion_metadata["context_window"]["turn_sequence"] == 1
    assert completion_metadata["context_window"]["revision"] == 1
    assert len(published) == 2


async def _empty_async_iter():
    if False:  # pragma: no cover - structural placeholder
        yield {}


@pytest.mark.asyncio
async def test_queued_dispatch_uses_start_turn_generation_context_tracking_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_start_calls: list[Dict[str, Any]] = []

    class _FakeUser:
        id = 55

    class _FakeQuery:
        def filter(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
            return self

        def first(self) -> _FakeUser:
            return _FakeUser()

    class _FakeDbSession:
        def query(self, _model: Any) -> _FakeQuery:
            return _FakeQuery()

        def close(self) -> None:
            return None

    class _FakeConversationHistoryReader:
        def __init__(self, _db: Any) -> None:
            return None

        def build_aligned_openai_conversation_history(self, **kwargs: Any) -> Any:
            assert kwargs["task_id"] == 500
            assert kwargs["conversation_id"] == "conv-500"
            assert kwargs["exclude_message_ids"] == {8001, 9001}
            assert "limit" not in kwargs
            return SimpleNamespace(
                messages=({"role": "assistant", "content": "queued history"},),
                source_message_ids=(7001,),
            )

    class _FakeTurnExecutionService:
        async def start_turn_generation(self, **kwargs: Any) -> None:
            captured_start_calls.append(kwargs)

    class _ServiceResolvedRuntimeSelection:
        provider = "openai"
        model = "gpt-5.2"
        credential_ref = SimpleNamespace(
            user_id=55,
            provider="openai",
            to_dict=lambda: {"user_id": 55, "provider": "openai"},
        )
        reasoning_effort = None

        def to_dict(self) -> Dict[str, Any]:
            return {
                "provider": self.provider,
                "model": self.model,
                "credential_ref": self.credential_ref.to_dict(),
                "reasoning_effort": self.reasoning_effort,
            }

    class _ServiceResolvedRuntimeConfigService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def build_runtime_selection(self, **kwargs: Any) -> Any:
            assert kwargs["user_id"] == 55
            assert kwargs["provider"] == "openai"
            assert kwargs["model"] == "gpt-5.2"
            return _ServiceResolvedRuntimeSelection()

    monkeypatch.setattr("backend.database.SessionLocal", lambda: _FakeDbSession())
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.SessionLocal", lambda: _FakeDbSession()
    )
    monkeypatch.setattr(
        "backend.routers.settings.get_user_openai_key", lambda user_id, db: "queued-key"
    )
    monkeypatch.setattr(
        "backend.routers.settings.get_user_openai_model", lambda user_id, db: "gpt-5.2"
    )
    monkeypatch.setattr(
        "backend.services.chat.conversation_history_reader.ConversationHistoryReader",
        _FakeConversationHistoryReader,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.get_turn_execution_service",
        lambda: _FakeTurnExecutionService(),
    )
    monkeypatch.setattr(
        "backend.services.llm_provider.LLMRuntimeConfigService",
        _ServiceResolvedRuntimeConfigService,
    )

    hub = InMemoryStreamHub()
    queued_msg = QueuedMessage(
        content="queued message",
        conversation_id="conv-500",
        user_id=55,
        created_at=datetime.utcnow(),
        task_id=500,
        user_message_id=8001,
        assistant_message_id=9001,
        turn_id="task-500-turn-7",
        turn_number=7,
        anchor_sequence=7,
        provider="openai",
        model="gpt-5.2",
        credential_ref={"user_id": 999, "provider": "openai"},
    )

    processed = await hub._process_queued_message_with_llm(queued_msg)

    assert processed is True
    assert len(captured_start_calls) == 1
    call = captured_start_calls[0]
    assert call["task_id"] == 500
    assert call["conversation_id"] == "conv-500"
    assert call["message"] == "queued message"
    assert call["history"] == [{"role": "assistant", "content": "queued history"}]
    assert call["history_source_message_ids"] == [7001]
    assert "queued message" not in str(call["history"])
    assert call["runtime_selection"] == {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 55, "provider": "openai"},
        "reasoning_effort": None,
    }
    assert "api_key" not in call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode_value",
    [ExecutionMode.SIMPLE_TOOL.value, ExecutionMode.DEEP_REASONING.value],
)
async def test_run_langgraph_generation_emits_boundary_for_skipped_summary_modes(
    monkeypatch: pytest.MonkeyPatch,
    mode_value: str,
) -> None:
    published: List[Dict[str, Any]] = []
    streaming_changes: List[tuple[int, bool]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            streaming_changes.append((task_id, state))

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    stub_hub = _StubHub()

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: stub_hub
    )

    async def _empty_event_generator():
        if False:  # pragma: no cover - structural placeholder
            yield {}

    def _event_iterator():
        return _empty_event_generator()

    result = LangGraphChatResult(
        final_text="Boundary event",
        conversation_id="conv-boundary",
        metadata={"role": "assistant", "streaming": False, "mode": mode_value},
        _event_iterator=_event_iterator,
    )

    async def _fake_handle_turn(
        chat_inputs: ChatInputs, metadata: Dict[str, Any] | None = None, **_kwargs: Any
    ):
        return result

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.handle_turn",
        AsyncMock(side_effect=_fake_handle_turn),
    )
    monkeypatch.setattr(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator.reserve_chat_turn_pair",
        lambda *args, **kwargs: (1, 2, "task-88-msg-123", 123),
    )

    await run_langgraph_generation(
        task_id=88,
        user_id=10,
        api_key="key",
        model="model",
        message="Hello",
        conversation_id="conv-boundary",
        history=[],
        anchor_sequence=123,
        requested_mode=None,
        turn_id="task-88-msg-123",
    )

    await asyncio.sleep(0)

    assert streaming_changes[0] == (88, True)
    assert streaming_changes[-1] == (88, False)
    assert len(published) == 2
    snapshot_event = published[0]["event"]
    sentinel_event = published[1]["event"]
    assert snapshot_event["type"] == "message_delta"
    assert snapshot_event["metadata"].get("turn_sequence") == 123
    assert snapshot_event["metadata"].get("id") == "task-88-msg-123"
    assert snapshot_event["metadata"].get("final_snapshot") is True
    assert sentinel_event["type"] == "assistant_final"
    assert sentinel_event["metadata"].get("turn_sequence") == 123
    assert sentinel_event["metadata"].get("id") == "task-88-msg-123"
    assert sentinel_event["metadata"].get("internal_only") is True


@pytest.mark.asyncio
async def test_run_resume_generation_emits_boundary_snapshot_on_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    streaming_changes: List[tuple[int, bool]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            streaming_changes.append((task_id, state))

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    stub_hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: stub_hub
    )

    result = LangGraphChatResult(
        final_text="Resume complete",
        conversation_id="conv-resume",
        metadata={
            "id": "task-99-turn-8",
            "turn_sequence": 8,
            "role": "assistant",
            "streaming": False,
        },
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(return_value=result),
    )

    await run_resume_generation(
        task_id=99,
        user_id=10,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id=11,
    )

    assert streaming_changes[0] == (99, True)
    assert streaming_changes[-1] == (99, False)
    assert len(published) == 2
    snapshot_event = published[0]["event"]
    sentinel_event = published[1]["event"]
    assert snapshot_event["type"] == "message_delta"
    assert snapshot_event["content"] == "Resume complete"
    assert snapshot_event["metadata"]["conversation_id"] == "conv-resume"
    assert snapshot_event["metadata"]["id"] == "task-99-turn-8"
    assert snapshot_event["metadata"]["turn_sequence"] == 8
    assert snapshot_event["metadata"]["final_snapshot"] is True
    assert sentinel_event["type"] == "assistant_final"
    assert sentinel_event["metadata"]["internal_only"] is True
    assert sentinel_event["metadata"]["id"] == "task-99-turn-8"
    assert sentinel_event["metadata"]["turn_sequence"] == 8


@pytest.mark.asyncio
async def test_resume_turn_generation_publishes_boundary_before_workflow_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: List[str] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **kwargs: None,
    )

    async def _fake_boundary(*args: Any, **kwargs: Any) -> None:
        call_order.append("boundary")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.TurnExecutionService._publish_boundary_completion_events",
        AsyncMock(side_effect=_fake_boundary),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(
            return_value=LangGraphChatResult(
                final_text="Resume complete",
                conversation_id="conv-resume-commit",
                metadata={
                    "id": "task-110-turn-8",
                    "turn_sequence": 8,
                    "role": "assistant",
                    "streaming": False,
                    "compression": {"applied": True, "epoch_id": "resume-epoch"},
                },
            )
        ),
    )

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=110,
        user_id=10,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id=15,
        workflow_id=9011,
        interrupt_id="intr-110",
        mark_turn_workflow_completed=lambda **kwargs: call_order.append(
            "workflow_completed"
        ),
        mark_interrupt_ticket_resumed=lambda **kwargs: None,
        mark_interrupt_ticket_completed=lambda **kwargs: None,
    )

    assert call_order[:2] == ["boundary", "workflow_completed"]


@pytest.mark.asyncio
async def test_resume_turn_generation_checkpoint_retry_completion_emits_retry_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_workflows: List[Dict[str, Any]] = []
    completed_retry_events: List[Dict[str, Any]] = []
    completed_rewind_events: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **kwargs: None,
    )
    retry_publish = AsyncMock(
        side_effect=lambda **kwargs: completed_retry_events.append(kwargs) or True
    )
    rewind_publish = AsyncMock(
        side_effect=lambda **kwargs: completed_rewind_events.append(kwargs) or True
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        retry_publish,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        rewind_publish,
    )

    def _retry_identity(**_kwargs: Any) -> Dict[str, Any]:
        return {
            "task_id": 220,
            "turn_id": "task-220-turn-4",
            "workflow_id": 9901,
            "graph_name": "deep_reasoning",
            "checkpoint_id": "ckpt-retry-hitl",
            "retry_mode": "checkpoint",
            "retry_attempt": 1,
            "retry_max_attempts": 2,
            "state": "waiting_for_human",
        }

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        _retry_identity,
    )
    resume_mock = AsyncMock(
        return_value=LangGraphChatResult(
            final_text="Retry HITL resume complete",
            conversation_id="conv-retry-hitl",
            metadata={
                "id": "task-220-turn-4",
                "turn_sequence": 4,
                "role": "assistant",
                "streaming": False,
            },
        )
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        resume_mock,
    )

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=220,
        user_id=22,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="deep_reasoning",
        checkpoint_id="ckpt-retry-hitl",
        workflow_id=9901,
        interrupt_id="intr-220",
        mark_turn_workflow_completed=lambda **kwargs: completed_workflows.append(
            kwargs
        ),
        mark_interrupt_ticket_resumed=lambda **kwargs: None,
        mark_interrupt_ticket_completed=lambda **kwargs: None,
    )

    assert len(completed_workflows) == 1
    assert resume_mock.await_args.kwargs["replace_turn_events"] is True
    completion_metadata = completed_workflows[0]["metadata"]
    assert completion_metadata["completion_source"] == "checkpoint_retry_resume"
    assert completion_metadata["active_retry"] is None
    assert completion_metadata["retry_state"] == "completed"

    assert len(completed_retry_events) == 1
    retry_event = completed_retry_events[0]
    assert retry_event["task_id"] == 220
    assert retry_event["state"] == "completed"
    assert retry_event["turn_id"] == "task-220-turn-4"
    assert retry_event["workflow_id"] == 9901
    assert retry_event["graph_name"] == "deep_reasoning"
    assert retry_event["transcript_resync_required"] is True
    assert retry_event["retry_identity"]["retry_attempt"] == 1
    assert retry_publish.await_count == 1

    assert len(completed_rewind_events) == 1
    rewind_event = completed_rewind_events[0]
    assert rewind_event["task_id"] == 220
    assert rewind_event["operation_kind"] == "retry"
    assert rewind_event["state"] == "completed"
    assert rewind_event["checkpoint_id"] == "ckpt-retry-hitl"
    assert rewind_event["transcript_resync_required"] is True
    assert rewind_publish.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_requested", "expected_state", "expected_error"),
    [
        (False, "failed", "resume_failed"),
        (True, "cancelled", "run_cancelled"),
    ],
)
async def test_resume_turn_generation_checkpoint_retry_terminals_clear_active_retry(
    monkeypatch: pytest.MonkeyPatch,
    cancel_requested: bool,
    expected_state: str,
    expected_error: str,
) -> None:
    failed_workflows: List[Dict[str, Any]] = []
    retry_events: List[Dict[str, Any]] = []
    rewind_events: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    class _StubLifecycle:
        def start_run(self, **_kwargs: Any) -> None:
            return None

        def end_run(self, **_kwargs: Any) -> None:
            return None

        def is_cancel_requested(self, **_kwargs: Any) -> bool:
            return cancel_requested

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: _StubLifecycle(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_turn_id_from_workflow_best_effort",
        lambda _workflow_id: "task-221-turn-4",
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **kwargs: {
                "conversation_id": "conv-retry-terminal",
                "turn_id": "task-221-turn-4",
                "turn_sequence": 4,
                "reserved_message_id": 704,
                "checkpoint_id": "ckpt-retry-hitl",
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **_kwargs: None),
    )

    async def _capture_retry_state(**kwargs: Any) -> bool:
        retry_events.append(kwargs)
        return True

    async def _capture_rewind_state(**kwargs: Any) -> bool:
        rewind_events.append(kwargs)
        return True

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        _capture_retry_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        _capture_rewind_state,
    )

    def _retry_identity(**_kwargs: Any) -> Dict[str, Any]:
        return {
            "task_id": 221,
            "turn_id": "task-221-turn-4",
            "workflow_id": 9902,
            "graph_name": "deep_reasoning",
            "checkpoint_id": "ckpt-retry-hitl",
            "retry_mode": "checkpoint",
            "retry_attempt": 1,
            "retry_max_attempts": 2,
            "state": "waiting_for_human",
        }

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        _retry_identity,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(side_effect=RuntimeError("retry resume terminal")),
    )

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=221,
        user_id=22,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="deep_reasoning",
        checkpoint_id="ckpt-retry-hitl",
        workflow_id=9902,
        interrupt_id="intr-221",
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
        mark_interrupt_ticket_resumed=lambda **kwargs: None,
        mark_interrupt_ticket_failed=lambda **kwargs: None,
    )

    terminal_metadata = failed_workflows[0]["metadata"]
    assert terminal_metadata["failure_source"] == "checkpoint_retry_resume"
    assert terminal_metadata["error"] == expected_error
    assert terminal_metadata["active_retry"] is None
    assert terminal_metadata["retry_state"] == expected_state
    terminal_retry_event = next(
        event for event in retry_events if event["state"] == expected_state
    )
    terminal_rewind_event = next(
        event for event in rewind_events if event["state"] == expected_state
    )
    assert terminal_retry_event["transcript_resync_required"] is True
    assert terminal_rewind_event["transcript_resync_required"] is True


@pytest.mark.asyncio
async def test_run_resume_generation_reinterrupt_emits_no_completion_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    streaming_changes: List[tuple[int, bool]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            streaming_changes.append((task_id, state))

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    stub_hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: stub_hub
    )

    result = LangGraphChatResult(
        final_text=None,
        conversation_id="conv-resume",
        metadata={"interrupt": True},
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(return_value=result),
    )

    await run_resume_generation(
        task_id=100,
        user_id=11,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id=12,
    )

    assert streaming_changes[0] == (100, True)
    assert streaming_changes[-1] == (100, False)
    assert published == []


@pytest.mark.asyncio
async def test_resume_turn_generation_maps_checkpoint_ceiling_to_waiting_workflow_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_events: List[Dict[str, Any]] = []
    waiting_calls: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.emit_context_window_event",
        lambda **kwargs: context_events.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **kwargs: None,
    )

    result = LangGraphChatResult(
        final_text=None,
        conversation_id="conv-resume-ctx",
        metadata={
            "interrupt": True,
            "graph_name": "simple_tool",
            "checkpoint_id": "cp-resume-ctx",
            "compression": {
                "applied": True,
                "pass_count": 2,
                "epoch_id": "resume-epoch-1",
                "source_tokens": 128321,
            },
            "context_window": {
                "ceiling_reached": True,
                "recommended_next_action": "compress",
                "compression_candidate": True,
                "max_tokens": 128_000,
                "used_tokens": 128_321,
                "remaining_tokens": 0,
                "ratio": 1.0,
                "conversation_id": "conv-resume-ctx",
            },
        },
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(return_value=result),
    )

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=103,
        user_id=14,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id="cp-resume-anchor",
        workflow_id=777,
        interrupt_id="intr-103",
        mark_turn_workflow_waiting=lambda **kwargs: waiting_calls.append(kwargs),
        mark_interrupt_ticket_resumed=lambda **kwargs: None,
    )

    assert len(context_events) == 1
    assert context_events[0]["ceiling_reached"] is True
    assert context_events[0]["recommended_next_action"] == "compress"
    assert context_events[0]["compression_candidate"] is True

    assert len(waiting_calls) == 1
    waiting_metadata = waiting_calls[0]["metadata"]
    assert waiting_metadata["resume_interrupted"] is True
    assert waiting_metadata["ceiling_reached"] is True
    assert waiting_metadata["recommended_next_action"] == "compress"
    assert waiting_metadata["compression_candidate"] is True
    assert waiting_metadata["context_window"]["used_tokens"] == 128_321
    assert waiting_metadata["context_window"]["conversation_id"] == "conv-resume-ctx"
    assert waiting_metadata["compression_applied"] is True
    assert waiting_metadata["compression"]["applied"] is True
    assert waiting_metadata["compression"]["pass_count"] == 2
    assert waiting_metadata["compression"]["epoch_id"] == "resume-epoch-1"
    assert waiting_calls[0]["checkpoint_id"] == "cp-resume-ctx"


@pytest.mark.asyncio
async def test_resume_turn_generation_emits_retryable_boundary_for_provider_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    failed_workflows: List[Dict[str, Any]] = []
    failed_tickets: List[Dict[str, Any]] = []
    persisted_errors: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **kwargs: {
                "conversation_id": "conv-retryable",
                    "turn_id": "task-201-turn-7",
                    "turn_sequence": 7,
                    "reserved_message_id": 701,
                    "checkpoint_id": "ckpt-resume-retryable",
                }
            ),
        )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **kwargs: persisted_errors.append(kwargs)),
    )

    retryable_exc = RetryablePostToolReasoningError(
        "Provider returned invalid structured output",
        error_code="provider_structured_output_parse",
        diagnostics={"response_id": "resp_201", "status": "incomplete"},
        graph_name="simple_tool",
    )

    async def _raise_retryable_failure(**_kwargs: Any) -> Any:
        raise HITLError("resume failed") from retryable_exc

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(side_effect=_raise_retryable_failure),
    )

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=201,
        user_id=22,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        reserved_message_id=701,
        workflow_id=8801,
        interrupt_id="intr-201",
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
        mark_interrupt_ticket_resumed=lambda **kwargs: None,
        mark_interrupt_ticket_failed=lambda **kwargs: failed_tickets.append(kwargs),
    )

    assert len(failed_workflows) == 1
    workflow_metadata = failed_workflows[0]["metadata"]
    assert workflow_metadata["failure_source"] == "resume_generation"
    assert workflow_metadata["error"] == "provider_structured_output_parse"
    assert workflow_metadata["retryable"] is True
    assert workflow_metadata["retry_mode"] == "checkpoint"
    assert workflow_metadata["graph_name"] == "simple_tool"
    assert (
        workflow_metadata["provider_error_message"]
        == "Provider returned invalid structured output"
    )
    assert failed_tickets == [{"task_id": 201, "interrupt_id": "intr-201"}]
    assert persisted_errors == [
        {
            "reserved_message_id": 701,
            "content": "[Error] A structured response failed validation. Retry to continue from the latest checkpoint.",
            "error_code": "provider_structured_output_parse",
        }
    ]

    assert [item["event"]["type"] for item in published] == [
        "message_delta",
        "assistant_final",
    ]
    snapshot_metadata = published[0]["event"]["metadata"]
    assert snapshot_metadata["status"] == "error"
    assert snapshot_metadata["retryable"] is True
    assert snapshot_metadata["retry_mode"] == "checkpoint"
    assert snapshot_metadata["error_code"] == "provider_structured_output_parse"
    assert snapshot_metadata["graph_name"] == "simple_tool"
    assert snapshot_metadata["id"] == "task-201-turn-7"
    assert snapshot_metadata["turn_id"] == "task-201-turn-7"
    assert snapshot_metadata["turn_sequence"] == 7


@pytest.mark.asyncio
async def test_run_checkpoint_retry_generation_emits_completion_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    completed_workflows: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub", lambda: _StubHub()
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_turn_workflow_completed_best_effort",
        lambda **kwargs: completed_workflows.append(kwargs),
    )

    retry_result = LangGraphChatResult(
        final_text="Retry complete",
        conversation_id="conv-checkpoint-retry",
        metadata={
            "id": "task-301-turn-9",
            "turn_sequence": 9,
            "role": "assistant",
            "streaming": False,
            "graph_name": "simple_tool",
            "compression": {"applied": True},
        },
    )
    retry_mock = AsyncMock(return_value=retry_result)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        retry_mock,
    )

    await run_checkpoint_retry_generation(
        task_id=301,
        user_id=12,
        workflow_id=991,
        graph_thread_id=GRAPH_THREAD_ID,
        turn_id="task-301-turn-9",
        turn_sequence=9,
        graph_name="simple_tool",
        reserved_message_id=909,
    )

    retry_mock.assert_awaited_once()
    assert retry_mock.await_args.kwargs["task_id"] == 301
    assert retry_mock.await_args.kwargs["graph_name"] == "simple_tool"
    assert retry_mock.await_args.kwargs["reserved_message_id"] == 909
    assert [item["event"]["type"] for item in published] == [
        "message_delta",
        "assistant_final",
    ]
    snapshot_metadata = published[0]["event"]["metadata"]
    assert snapshot_metadata["id"] == "task-301-turn-9"
    assert snapshot_metadata["turn_sequence"] == 9
    # Successful checkpoint-retry completion clears the in-flight
    # ``active_retry`` block and stamps ``retry_state=completed`` so transcript
    # bootstrap derives the post-retry overlay from one workflow read.
    assert completed_workflows == [
        {
            "workflow_id": 991,
            "metadata": {
                "completion_source": "checkpoint_retry",
                "compression": {"applied": True},
                "compression_applied": True,
                "active_retry": None,
                "retry_state": "completed",
            },
        }
    ]


@pytest.mark.asyncio
async def test_run_resume_generation_marks_failed_when_hub_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_workflows: List[Dict[str, Any]] = []
    failed_tickets: List[Dict[str, Any]] = []

    def _raise_hub_unavailable() -> Any:
        raise RuntimeError("hub unavailable")

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        _raise_hub_unavailable,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_turn_workflow_failed_best_effort",
        lambda **kwargs: failed_workflows.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_interrupt_ticket_failed_best_effort",
        lambda **kwargs: failed_tickets.append(kwargs),
    )

    await run_resume_generation(
        task_id=101,
        user_id=12,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        workflow_id=555,
        interrupt_id="intr-101",
    )

    assert failed_workflows == [
        {
            "workflow_id": 555,
            "metadata": {
                "failure_source": "resume_generation",
                "error": "resume_hub_unavailable",
            },
        }
    ]
    assert failed_tickets == [{"task_id": 101, "interrupt_id": "intr-101"}]


@pytest.mark.asyncio
async def test_run_resume_generation_empty_final_content_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: List[Dict[str, Any]] = []
    streaming_changes: List[tuple[int, bool]] = []
    failed_workflows: List[Dict[str, Any]] = []
    failed_tickets: List[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            streaming_changes.append((task_id, state))

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(
            return_value=LangGraphChatResult(
                final_text="   ",
                conversation_id="conv-empty",
                metadata={},
            )
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_interrupt_ticket_resumed_best_effort",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_turn_workflow_failed_best_effort",
        lambda **kwargs: failed_workflows.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_interrupt_ticket_failed_best_effort",
        lambda **kwargs: failed_tickets.append(kwargs),
    )

    await run_resume_generation(
        task_id=102,
        user_id=13,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id=13,
        workflow_id=556,
        interrupt_id="intr-102",
    )

    assert streaming_changes[0] == (102, True)
    assert streaming_changes[-1] == (102, False)
    assert len(failed_workflows) == 1
    assert failed_workflows[0]["workflow_id"] == 556
    assert failed_workflows[0]["metadata"]["failure_source"] == "resume_generation"
    assert failed_workflows[0]["metadata"]["error"] == "resume_failed"
    assert failed_tickets == [{"task_id": 102, "interrupt_id": "intr-102"}]
    # Error path emits boundary completion events for UI consistency.
    assert len(published) == 2
    assert published[0]["event"]["type"] == "message_delta"
    assert published[1]["event"]["type"] == "assistant_final"
