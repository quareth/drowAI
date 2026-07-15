"""Failure and retry behavior tests for post_tool_reasoning node."""

from typing import Any, Dict, List

import pytest

from agent.graph.nodes.post_tool_reasoning import node
from agent.graph.nodes.post_tool_reasoning.models import (
    PostToolReasoningOutput,
    TodoProgress,
    ToolIntent,
)
from agent.graph.nodes.post_tool_reasoning.node import _update_active_decision_memory
from agent.graph.state import FactsState, InteractiveState, TodoItem, TodoStatus, TraceState
from agent.graph.utils.todo_stall_guard import TODO_STALL_METADATA_KEY


def _make_state(
    metadata: Dict[str, Any],
    *,
    capability: str = "deep_reasoning",
    message: str = "test goal",
    selected_tool: str | None = None,
    tool_parameters: Dict[str, Any] | None = None,
    intent_hints: Dict[str, Any] | None = None,
) -> InteractiveState:
    facts = FactsState(
        task_id=1,
        message=message,
        capability=capability,
        conversation_id="conv-1",
        metadata=metadata,
        selected_tool=selected_tool,
        tool_parameters=tool_parameters or {},
        intent_hints=intent_hints or {},
    )
    return InteractiveState(facts=facts, trace=TraceState())


def test_detect_network_failure() -> None:
    state = _make_state(
        {
            "synthesized_output": {"success": False},
            "last_tool_result_compact": {
                "success": False,
                "status": "failed",
                "errors": ["Connection refused"],
            },
            "last_tool_result": {"stderr": "Connection refused", "success": False},
        }
    )
    detected, category = node._detect_tool_failure(state)
    assert detected is True
    assert category == "network_error"


def test_detect_permission_failure() -> None:
    state = _make_state(
        {
            "synthesized_output": {"success": False},
            "last_tool_result_compact": {
                "success": False,
                "status": "failed",
                "errors": ["Permission denied"],
            },
            "last_tool_result": {"stderr": "Permission denied", "success": False},
        }
    )
    detected, category = node._detect_tool_failure(state)
    assert detected is True
    assert category == "permission_denied"


def test_detect_timeout_failure() -> None:
    state = _make_state(
        {
            "synthesized_output": {"success": False},
            "last_tool_result": {"stderr": "operation timeout", "exit_code": 124},
        }
    )
    detected, category = node._detect_tool_failure(state)
    assert detected is True
    assert category == "timeout"


def test_detect_empty_output() -> None:
    state = _make_state(
        {
            "synthesized_output": {},
            "last_tool_result": {"stdout": "", "stderr": ""},
        }
    )
    detected, category = node._detect_tool_failure(state)
    assert detected is True
    assert category == "empty_output"


def test_retry_budget_tracking() -> None:
    """Retry counter increments correctly and ``_can_retry`` flips to False
    only when ``count >= MAX_RETRIES``.

    Uses ``node.MAX_RETRIES`` so the test stays correct if the budget is
    re-tuned in ``core/retry_logic.py``.
    """
    state = _make_state({"synthesized_output": {"summary": "x"}})

    assert node._get_retry_count(state) == 0
    assert node._can_retry(state) is True

    # Increment up to (but not past) the budget; ``_can_retry`` stays True.
    for expected_count in range(1, node.MAX_RETRIES):
        node._increment_retry_count(state)
        assert node._get_retry_count(state) == expected_count
        assert node._can_retry(state) is True

    # The boundary increment exhausts the budget.
    node._increment_retry_count(state)
    assert node._get_retry_count(state) == node.MAX_RETRIES
    assert node._can_retry(state) is False


def test_retry_budget_exhausted() -> None:
    state = _make_state(
        {
            "retry_tracking": {"count": node.MAX_RETRIES},
            "synthesized_output": {"summary": "x"},
        }
    )
    assert node._can_retry(state) is False


@pytest.mark.asyncio
async def test_retry_event_emission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-suggesting decisions emit ``retry_start`` and ``retry_attempt``
    events through the streaming pipeline.
    """
    events: List[Dict[str, Any]] = []

    class DummyAdapter:
        def get_stream_identifiers(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            return ("conv-1", "turn-1")

        async def stream_observation_text(self, **_: Any) -> tuple[str, bool, None]:
            return (
                "Observed network failure; proposing retry.",
                False,
                None,
            )

    async def fake_decision_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Tool failed; retry suggested.",
            next_action="call_tool",
            action_reasoning="Network error detected, retry",
            tool_intent=ToolIntent(
                description="Retry connectivity check",
                target=None,
                focus="network reachability",
            ),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=True,
            failure_category="network_error",
            retry_suggested=True,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    monkeypatch.setattr(node.StreamingAdapterFactory, "create", lambda *_args, **_kwargs: DummyAdapter())
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)
    monkeypatch.setattr(node, "analyze_tool_result", fake_decision_call)

    metadata = {
        "synthesized_output": {"summary": "failure summary", "success": False},
        "last_tool_result": {"stderr": "connection refused", "success": False},
    }
    state = _make_state(metadata)

    def writer(event: Dict[str, Any]) -> None:
        events.append(event)

    await node.post_tool_reasoning(state, context=None, config={}, writer=writer)

    event_types = [event.get("type") for event in events]
    assert "retry_start" in event_types
    assert "retry_attempt" in event_types


@pytest.mark.asyncio
async def test_simple_streaming_uses_retry_count_for_sub_turn_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In simple_tool_execution, the streaming sub_turn_index reflects the
    current retry counter so retry attempts render as distinct stream
    segments on the frontend.
    """
    captured: Dict[str, Any] = {}

    class DummyAdapter:
        def get_stream_identifiers(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            return ("conv-1", "turn-1")

        async def stream_observation_text(self, **kwargs: Any) -> tuple[str, bool, None]:
            captured["sub_turn_index"] = kwargs.get("sub_turn_index")
            return (
                "Observed failure; retry context applied.",
                True,
                None,
            )

    async def fake_decision_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Failure observed.",
            next_action="finalize",
            action_reasoning="Streaming identity check",
            tool_intent=None,
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    monkeypatch.setattr(node.StreamingAdapterFactory, "create", lambda *_args, **_kwargs: DummyAdapter())
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)
    monkeypatch.setattr(node, "analyze_tool_result", fake_decision_call)

    state = _make_state(
        {
            "retry_tracking": {"count": 1},
            "synthesized_output": {"summary": "failure summary", "success": False},
            "last_tool_result": {"stderr": "connection refused", "success": False},
        },
        capability="simple_tool_execution",
    )

    await node.post_tool_reasoning(state, context=None, config={}, writer=lambda _event: None)

    assert captured.get("sub_turn_index") == 1


@pytest.mark.asyncio
async def test_simple_streaming_prefers_explicit_sub_turn_index_over_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``metadata["sub_turn_index"]`` is set explicitly, streaming uses
    it instead of falling back to the retry counter. This is the precedence
    rule used by direct-executor / multi-step flows.
    """
    captured: Dict[str, Any] = {}

    class DummyAdapter:
        def get_stream_identifiers(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            return ("conv-1", "turn-1")

        async def stream_observation_text(self, **kwargs: Any) -> tuple[str, bool, None]:
            captured["sub_turn_index"] = kwargs.get("sub_turn_index")
            return (
                "Observed direct executor result with explicit step identity.",
                True,
                None,
            )

    async def fake_decision_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Step succeeded.",
            next_action="finalize",
            action_reasoning="Explicit sub_turn_index precedence check",
            tool_intent=None,
            user_goal_achieved=True,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    monkeypatch.setattr(node.StreamingAdapterFactory, "create", lambda *_args, **_kwargs: DummyAdapter())
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)
    monkeypatch.setattr(node, "analyze_tool_result", fake_decision_call)

    state = _make_state(
        {
            "sub_turn_index": 2,
            "retry_tracking": {"count": 1},
            "synthesized_output": {"summary": "step summary", "success": True},
            "last_tool_result": {"stdout": "scan done", "success": True},
        },
        capability="simple_tool_execution",
    )

    await node.post_tool_reasoning(state, context=None, config={}, writer=lambda _event: None)

    assert captured.get("sub_turn_index") == 2


@pytest.mark.asyncio
async def test_streaming_sub_turn_prefers_dr_counter_over_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In deep_reasoning, the DR iteration counter
    (``dr_iteration_meta.active_iteration``) takes precedence over
    ``retry_tracking.count`` when computing sub_turn_index.
    """
    captured: Dict[str, Any] = {}

    class DummyAdapter:
        def get_stream_identifiers(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            return ("conv-1", "turn-1")

        async def stream_observation_text(self, **kwargs: Any) -> tuple[str, bool, None]:
            captured["sub_turn_index"] = kwargs.get("sub_turn_index")
            return (
                "Observed result for DR precedence check.",
                True,
                None,
            )

    async def fake_decision_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="DR step observed.",
            next_action="finalize",
            action_reasoning="DR sub_turn_index precedence check",
            tool_intent=None,
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    monkeypatch.setattr(node.StreamingAdapterFactory, "create", lambda *_args, **_kwargs: DummyAdapter())
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)
    monkeypatch.setattr(node, "analyze_tool_result", fake_decision_call)

    state = _make_state(
        {
            "dr_iteration_meta": {"counter": 4, "active_iteration": 4},
            "retry_tracking": {"count": 1},
            "synthesized_output": {"summary": "failure summary", "success": False},
            "last_tool_result": {"stderr": "connection refused", "success": False},
        },
        capability="deep_reasoning",
    )

    await node.post_tool_reasoning(state, context=None, config={}, writer=lambda _event: None)

    assert captured.get("sub_turn_index") == 4


@pytest.mark.asyncio
async def test_streaming_observation_flag_written_to_active_metadata_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When streaming runs, ``metadata["observation_streamed"]`` must be set
    on the LIVE metadata reference visible to the caller \u2014 not on a copy
    \u2014 so downstream nodes see the flag.
    """

    class DummyAdapter:
        def get_stream_identifiers(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            return ("conv-1", "turn-1")

        async def stream_observation_text(self, **_kwargs: Any) -> tuple[str, bool, None]:
            return (
                "Observed output for metadata reference stability test.",
                True,
                None,
            )

    async def fake_decision_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Streamed observation.",
            next_action="finalize",
            action_reasoning="Metadata reference stability check",
            tool_intent=None,
            user_goal_achieved=True,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    monkeypatch.setattr(node.StreamingAdapterFactory, "create", lambda *_args, **_kwargs: DummyAdapter())
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)
    monkeypatch.setattr(node, "analyze_tool_result", fake_decision_call)

    state = _make_state(
        {
            "synthesized_output": {"summary": "streaming metadata flag", "success": True},
            "last_tool_result": {"stdout": "ok", "success": True},
        },
        capability="deep_reasoning",
    )

    await node.post_tool_reasoning(state, context=None, config={}, writer=lambda _event: None)

    assert state.facts.metadata.get("observation_streamed") is True


@pytest.mark.asyncio
async def test_failure_metadata_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Tool failed and will not retry.",
            next_action="finalize",
            action_reasoning="Budget exhausted",
            tool_intent=None,
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=True,
            failure_category="timeout",
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        class _Dummy:
            pass

        return _Dummy()

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    metadata = {
        "synthesized_output": {"summary": "failure summary", "success": False},
        "last_tool_result": {"stderr": "timed out", "success": False},
    }
    state = _make_state(metadata)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    assert state.facts.metadata.get("failure_detected") is True
    assert state.facts.metadata.get("failure_category") == "timeout"
    assert state.facts.metadata.get("retry_suggested") is False


@pytest.mark.asyncio
async def test_retry_budget_consumed_non_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Tool failed; retrying.",
            next_action="call_tool",
            action_reasoning="Retry recommended",
            tool_intent=ToolIntent(description="Retry action", target=None, focus=None),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=True,
            failure_category="network_error",
            retry_suggested=True,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        class _Dummy:
            pass

        return _Dummy()

    metadata = {
        "synthesized_output": {"summary": "failure summary", "success": False},
        "last_tool_result": {"stderr": "connection refused", "success": False},
    }
    state = _make_state(metadata)

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    assert node._get_retry_count(state) == 1
    assert state.facts.metadata.get("retry_tracking", {}).get("count") == 1


@pytest.mark.asyncio
async def test_retry_suggestion_rejected_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Tool failed; retrying.",
            next_action="call_tool",
            action_reasoning="Retry recommended",
            tool_intent=ToolIntent(description="Retry action", target=None, focus=None),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=True,
            failure_category="timeout",
            retry_suggested=True,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        class _Dummy:
            pass

        return _Dummy()

    metadata = {
        "retry_tracking": {"count": node.MAX_RETRIES},
        "synthesized_output": {"summary": "failure summary", "success": False},
        "last_tool_result": {"stderr": "timed out", "success": False},
    }
    state = _make_state(metadata)

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    assert node._get_retry_count(state) == node.MAX_RETRIES
    assert state.facts.metadata.get("retry_tracking", {}).get("count") == node.MAX_RETRIES
    assert state.facts.metadata.get("retry_suggested") is False


@pytest.mark.asyncio
async def test_simple_tool_success_allows_bounded_followup_call_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the bounded direct-executor contract, a first follow-up call_tool
    on successful tool output is allowed to continue: none of the deterministic
    stop criteria (goal achieved, budget exhausted, repeated no-progress) fire
    on the first iteration, so the decision must survive as ``call_tool``.

    This replaces the legacy single-step policy assertion which coerced any
    non-recovery follow-up to ``finalize``. See
    ``policies.direct_executor.apply_direct_executor_policy``.
    """
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Scan found open PostgreSQL service and recommends extra enumeration.",
            next_action="call_tool",
            action_reasoning="Need additional service enumeration",
            tool_intent=ToolIntent(
                description="Enumerate PostgreSQL",
                target="127.0.0.1:5432",
                focus="service details",
            ),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "synthesized_output": {"summary": "postgres detected", "success": True},
            "last_tool_result": {"success": True, "status": "success"},
        },
        capability="simple_tool_execution",
    )

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    decision_entry = state.facts.decision_history[-1]
    assert decision_entry.startswith("call_tool:")
    assert state.facts.metadata.get("last_post_tool_action") == "call_tool"
    # The direct-executor policy must NOT falsely mark the goal achieved when
    # the LLM explicitly asked for a follow-up step and stop criteria do not fire.
    assert state.facts.metadata.get("user_goal_achieved") is not True
    # The structured tool_intent must be preserved so the builder can route to
    # the follow-up selection step.
    assert state.facts.metadata.get("tool_intent") is not None


@pytest.mark.asyncio
async def test_active_todo_stall_guard_candidate_decision_reflects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTR call_tool output is coerced before candidate_decision is recorded."""

    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Hostname resolution still failed and no IP was found.",
            next_action="call_tool",
            action_reasoning="Try one more resolver",
            tool_intent=ToolIntent(
                description="Try another hostname lookup",
                target="cve-2018-7600-web-1",
                focus="DNS resolution",
            ),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "turn_sequence": 7,
            "synthesized_output": {"summary": "hostname unresolved", "success": False},
            "last_tool_result": {"success": False, "status": "failed"},
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 2,
                "threshold": 3,
            },
        }
    )
    state.facts.todo_list = [
        TodoItem(description="Resolve target hostname", status=TodoStatus.IN_PROGRESS)
    ]

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    assert state.facts.decision_history[-1].startswith("reflect:")
    candidate = state.facts.metadata.get("candidate_decision")
    assert candidate is not None
    assert candidate["next_action"] == "reflect"
    assert "active todo stalled without progress" in candidate["action_reasoning"]


@pytest.mark.asyncio
async def test_active_todo_stall_guard_candidate_decision_synthesizes_after_reflect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-reflect no-progress call_tool decision is recorded as synthesis."""

    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Hostname resolution still failed after reflection.",
            next_action="call_tool",
            action_reasoning="Try the same resolver again",
            tool_intent=ToolIntent(
                description="Retry hostname lookup",
                target="cve-2018-7600-web-1",
                focus="DNS resolution",
            ),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "turn_sequence": 8,
            "synthesized_output": {"summary": "hostname unresolved", "success": False},
            "last_tool_result": {"success": False, "status": "failed"},
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 3,
                "threshold": 3,
                "forced_action": "reflect",
                "post_reflect_awaiting_progress": True,
            },
        }
    )
    state.facts.todo_list = [
        TodoItem(description="Resolve target hostname", status=TodoStatus.IN_PROGRESS)
    ]

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    assert state.facts.decision_history[-1].startswith("synthesis:")
    candidate = state.facts.metadata.get("candidate_decision")
    assert candidate is not None
    assert candidate["next_action"] == "synthesis"
    assert "active todo still stalled after reflection" in candidate["action_reasoning"]


@pytest.mark.asyncio
async def test_simple_tool_failure_retry_call_tool_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Scan timed out; retrying with adjusted timeout.",
            next_action="call_tool",
            action_reasoning="Timeout detected; retry is appropriate",
            tool_intent=ToolIntent(
                description="Retry with longer timeout",
                target="127.0.0.1",
                focus="port scan",
            ),
            user_goal_achieved=False,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=True,
            failure_category="timeout",
            retry_suggested=True,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "synthesized_output": {"summary": "timeout", "success": False},
            "last_tool_result": {"stderr": "timed out", "success": False},
        },
        capability="simple_tool_execution",
    )

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    decision_entry = state.facts.decision_history[-1]
    assert decision_entry.startswith("call_tool:")
    assert node._get_retry_count(state) == 1


@pytest.mark.asyncio
async def test_simple_tool_intent_contract_records_mismatch_but_does_not_override_llm_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM is the sole authority for intent classification.

    When the LLM returns ``next_action="finalize"`` with
    ``user_goal_achieved=True``, the post-tool pipeline must NOT override that
    decision because a regex/keyword evaluator detects a mismatch between the
    literal user message ("scan ... for port 5000") and the executed
    parameters (``ports=5432``). The evaluator still runs and surfaces the
    gap via ``metadata["intent_contract_evaluation"]`` so the next-turn LLM
    prompt can read it and the LLM can decide for itself.
    """

    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="Port scan completed successfully.",
            next_action="finalize",
            action_reasoning="Scan complete",
            tool_intent=None,
            user_goal_achieved=True,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    selected_tool = "information_gathering.network_discovery.nmap"
    state = _make_state(
        {
            "synthesized_output": {"summary": "postgres detected", "success": True},
            "last_tool_result": {"success": True, "status": "success"},
        },
        capability="simple_tool_execution",
        message="scan 127.0.0.1 with nmap for port 5000",
        selected_tool=selected_tool,
        tool_parameters={selected_tool: {"target": "127.0.0.1", "ports": "5432"}},
    )

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    decision_entry = state.facts.decision_history[-1]
    assert decision_entry.startswith("finalize:")
    assert state.facts.metadata.get("failure_detected") is not True
    assert state.facts.metadata.get("retry_suggested") is not True
    assert state.facts.metadata.get("last_post_tool_action") == "finalize"

    contract = state.facts.metadata.get("intent_contract_evaluation")
    assert isinstance(contract, dict)
    assert contract.get("satisfied") is False
    assert contract.get("ports_match") is False


@pytest.mark.asyncio
async def test_simple_tool_intent_contract_allows_helper_step_when_prior_turn_step_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_decision_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation="The redirect destination is now confirmed.",
            next_action="finalize",
            action_reasoning="The redirect target was extracted from current-turn evidence.",
            tool_intent=None,
            user_goal_achieved=True,
            todo_progress=[],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    async def fake_observation(*_: Any, **__: Any) -> str:
        return "I confirmed the redirect destination from the saved response."

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "turn_sequence": 11,
            "synthesized_output": {"summary": "Location header points to /data/2", "success": True},
            "last_tool_result": {"success": True, "status": "success"},
            "last_tool_result_compact": {
                "summary": "Location header points to http://10.129.31.138/data/2",
                "key_findings": ["Location: http://10.129.31.138/data/2"],
                "errors": [],
            },
            "action_history": [
                {
                    "tool_id": "information_gathering.web_enumeration.http_request",
                    "params": {"target": "http://10.129.31.138/capture"},
                    "turn_sequence": 11,
                }
            ],
        },
        capability="simple_tool_execution",
        message="Then lets try to find this redirect thing again",
        selected_tool="filesystem.read_file",
        tool_parameters={"filesystem.read_file": {"path": "artifacts/redirect.txt"}},
        intent_hints={"targets": ["10.129.31.138"]},
    )

    monkeypatch.setattr(node, "analyze_tool_result", fake_decision_call)
    monkeypatch.setattr(node, "_generate_observation_text", fake_observation)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    decision_entry = state.facts.decision_history[-1]
    assert decision_entry.startswith("finalize:")
    assert state.facts.metadata.get("failure_detected") is not True
    assert state.facts.metadata.get("retry_suggested") is not True

    contract = state.facts.metadata.get("intent_contract_evaluation")
    assert isinstance(contract, dict)
    assert contract.get("satisfied") is True
    assert contract.get("matched_via") == "prior_step"
    assert contract.get("executed_targets") == ["10.129.31.138"]


@pytest.mark.asyncio
async def test_dr_binary_contract_partial_progress_does_not_force_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation=(
                "Host discovery found two hosts. The targeted PostgreSQL check "
                "verified port 5432 is closed on the selected host."
            ),
            next_action="call_tool",
            action_reasoning="Need more probing before finalizing",
            tool_intent=ToolIntent(
                description="Run additional host checks",
                target="10.0.0.5",
                focus="service discovery",
            ),
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(
                    index=1,
                    status="completed",
                    completion_type="negative",
                    completion_reason="port 5432 verified closed on chosen host",
                )
            ],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "synthesized_output": {
                "summary": "Nmap found two hosts; 5432/tcp closed on selected host",
                "success": True,
            },
            "last_tool_result": {"success": True, "status": "success"},
            "request_contract": {
                "question_type": "binary_check",
                "answer_style": "short",
                "terminal_when": "determined",
            },
        },
        capability="deep_reasoning",
        message=(
            "Discover hosts and determine whether PostgreSQL port 5432 is open. "
            "Give a short answer."
        ),
    )
    state.facts.todo_list = [
        "Discover hosts on 10.0.0.0/24",
        "Determine whether 5432 is open on selected host",
    ]

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    decision_entry = state.facts.decision_history[-1]
    assert decision_entry.startswith("call_tool:")
    assert state.facts.metadata.get("user_goal_achieved") is not True
    assert state.facts.metadata.get("request_contract_terminal") is not True


@pytest.mark.asyncio
async def test_dr_binary_contract_all_todos_terminal_still_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_non_streaming_call(**_: Any) -> PostToolReasoningOutput:
        return PostToolReasoningOutput(
            observation=(
                "Host discovery completed and port 5432 status was conclusively "
                "verified on the selected host."
            ),
            next_action="call_tool",
            action_reasoning="Would continue unless contract marks request determined",
            tool_intent=ToolIntent(
                description="Extra verification pass",
                target="10.0.0.5",
                focus="service validation",
            ),
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Host discovery completed with concrete host list",
                ),
                TodoProgress(
                    index=1,
                    status="completed",
                    completion_type="negative",
                    completion_reason="port 5432 verified closed on chosen host",
                ),
            ],
            effective_next_goal=None,
            failure_detected=False,
            failure_category=None,
            retry_suggested=False,
        )

    def fake_resolve_llm_client(*_: Any, **__: Any) -> Any:
        return object()

    state = _make_state(
        {
            "synthesized_output": {
                "summary": "Nmap found two hosts; 5432/tcp closed on selected host",
                "success": True,
            },
            "last_tool_result": {"success": True, "status": "success"},
            "request_contract": {
                "question_type": "binary_check",
                "answer_style": "short",
                "terminal_when": "determined",
            },
        },
        capability="deep_reasoning",
        message=(
            "Discover hosts and determine whether PostgreSQL port 5432 is open. "
            "Give a short answer."
        ),
    )
    state.facts.todo_list = [
        "Discover hosts on 10.0.0.0/24",
        "Determine whether 5432 is open on selected host",
    ]

    monkeypatch.setattr(node, "analyze_tool_result", fake_non_streaming_call)
    monkeypatch.setattr(node, "resolve_llm_client", fake_resolve_llm_client)

    await node.post_tool_reasoning(state, context=None, config=None, writer=None)

    decision_entry = state.facts.decision_history[-1]
    assert decision_entry.startswith("finalize:")
    assert state.facts.metadata.get("user_goal_achieved") is True
    assert state.facts.metadata.get("request_contract_terminal") is True


def test_active_decision_lifecycle_call_tool_creates_active_record() -> None:
    state = _make_state({"synthesized_output": {"summary": "x", "success": True}})
    output = PostToolReasoningOutput(
        observation="Host discovery complete.",
        next_action="call_tool",
        action_reasoning="Only one feasible host candidate remains.",
        tool_intent=ToolIntent(
            description="Scan PostgreSQL port 5432 on selected host",
            target="172.17.0.1",
            focus="tcp/5432",
        ),
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal="Determine whether 5432 is open on selected host",
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )

    _update_active_decision_memory(state, output)

    working_memory = state.facts.metadata.get("working_memory")
    assert isinstance(working_memory, dict)
    active_decision = working_memory.get("active_decision")
    assert isinstance(active_decision, dict)
    assert active_decision.get("source") == "post_tool_reasoning"
    assert active_decision.get("authority") == "llm_proposal"
    assert active_decision.get("status") == "active"
    assert active_decision.get("next_action") == "call_tool"
    assert active_decision.get("tool_intent", {}).get("target") == "172.17.0.1"


def test_active_decision_lifecycle_terminal_todo_marks_resolved() -> None:
    state = _make_state({"synthesized_output": {"summary": "x", "success": True}})
    initial_output = PostToolReasoningOutput(
        observation="Initial decision.",
        next_action="call_tool",
        action_reasoning="Need one follow-up scan.",
        tool_intent=ToolIntent(
            description="Scan selected host",
            target="172.17.0.1",
            focus="tcp/5432",
        ),
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal="Check 5432",
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )
    _update_active_decision_memory(state, initial_output)

    followup_output = PostToolReasoningOutput(
        observation="PostgreSQL check finished.",
        next_action="think_more",
        action_reasoning="Evaluate result for terminal response.",
        tool_intent=None,
        user_goal_achieved=False,
        todo_progress=[
            TodoProgress(
                index=1,
                status="completed",
                completion_type="negative",
                completion_reason="TCP/5432 returned closed (reset)",
            )
        ],
        effective_next_goal=None,
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )

    _update_active_decision_memory(state, followup_output)

    active_decision = state.facts.metadata.get("working_memory", {}).get("active_decision")
    assert isinstance(active_decision, dict)
    assert active_decision.get("status") == "resolved"
    assert active_decision.get("status_reason") == "todo_terminal_update"


def test_active_decision_lifecycle_goal_change_marks_superseded() -> None:
    state = _make_state({"synthesized_output": {"summary": "x", "success": True}})
    initial_output = PostToolReasoningOutput(
        observation="Initial decision.",
        next_action="call_tool",
        action_reasoning="Need one follow-up scan.",
        tool_intent=ToolIntent(
            description="Scan selected host",
            target="172.17.0.1",
            focus="tcp/5432",
        ),
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal="Check 5432",
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )
    _update_active_decision_memory(state, initial_output)

    followup_output = PostToolReasoningOutput(
        observation="Pivoting to new objective.",
        next_action="reflect",
        action_reasoning="Current evidence suggests a different phase.",
        tool_intent=None,
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal="Re-evaluate host selection strategy",
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )

    _update_active_decision_memory(state, followup_output)

    active_decision = state.facts.metadata.get("working_memory", {}).get("active_decision")
    assert isinstance(active_decision, dict)
    assert active_decision.get("status") == "superseded"
    assert active_decision.get("status_reason") == "goal_phase_changed"


def test_active_decision_lifecycle_finalize_clears_memory() -> None:
    state = _make_state({"synthesized_output": {"summary": "x", "success": True}})
    initial_output = PostToolReasoningOutput(
        observation="Initial decision.",
        next_action="call_tool",
        action_reasoning="Need one follow-up scan.",
        tool_intent=ToolIntent(
            description="Scan selected host",
            target="172.17.0.1",
            focus="tcp/5432",
        ),
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal="Check 5432",
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )
    _update_active_decision_memory(state, initial_output)

    finalize_output = PostToolReasoningOutput(
        observation="Objective completed.",
        next_action="finalize",
        action_reasoning="All required evidence gathered.",
        tool_intent=None,
        user_goal_achieved=True,
        todo_progress=[
            TodoProgress(
                index=1,
                status="completed",
                completion_type="negative",
                completion_reason="TCP/5432 is closed on selected online host",
            )
        ],
        effective_next_goal=None,
        failure_detected=False,
        failure_category=None,
        retry_suggested=False,
    )

    _update_active_decision_memory(state, finalize_output)

    working_memory = state.facts.metadata.get("working_memory")
    assert isinstance(working_memory, dict)
    assert working_memory.get("active_decision") is None
