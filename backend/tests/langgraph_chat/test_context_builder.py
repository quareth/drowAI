"""Tests for LangGraph runtime context and metadata assembly."""

import json
from types import SimpleNamespace

import pytest

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from backend.services.langgraph_chat import context_builder as context_builder_module
from backend.services.langgraph_chat.context_builder import (
    ConflictingExecutionRouteError,
    LangGraphContextBuilder,
    _is_canonical_environment_info,
)
from backend.services.langgraph_chat.contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
)
from backend.services.langgraph_chat.facade_helpers import build_metadata
from backend.services.langgraph_chat.handlers.turn_runtime import (
    build_initial_interactive_state,
)
from backend.services.runtime_provider import RuntimeCallScope


def test_deterministic_e2e_runtime_projection_uses_test_scope_without_provider_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    context = SimpleNamespace(
        to_worker_payload=lambda: {
            "tenant_id": 7,
            "graph_thread_id": "a" * 32,
            "runtime_placement_mode": "local",
            "workspace_id": "task-41",
        }
    )

    class FakeDb:
        def close(self) -> None:
            calls["closed"] = True

    class FakeRuntimeOperationService:
        def __init__(self, _db) -> None:
            pass

        def context_for_internal_task(self, **kwargs):
            calls["context_kwargs"] = kwargs
            return context

        async def run_for_context(self, **_kwargs):
            raise AssertionError("deterministic E2E must not call a runtime provider")

    monkeypatch.setattr(context_builder_module, "E2E_DETERMINISTIC_MODE", True)
    monkeypatch.setattr(context_builder_module, "SessionLocal", FakeDb)
    monkeypatch.setattr(
        context_builder_module,
        "RuntimeOperationService",
        FakeRuntimeOperationService,
    )

    projection = context_builder_module._resolve_provider_runtime_projection(
        task_id=41,
        user_id=9,
    )

    assert calls["context_kwargs"]["runtime_call_scope"] is RuntimeCallScope.TEST
    assert calls["closed"] is True
    assert projection["graph_thread_id"] == "a" * 32
    assert projection["actor_type"] == "agent"
    assert projection["actor_id"] == "langgraph"


def test_agent_mode_propagated_to_metadata() -> None:
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=101,
        user_id=1,
        message="test",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    assert config.metadata["agent_mode"] == "agent"


def test_build_runtime_config_populates_context_bundle() -> None:
    """LangGraphContextBuilder is the single bundle-assembly authority.

    The wired runtime-config setup path must produce a bundle with the
    exact transcript passed in. No other layer (including the facade
    helpers) may rebuild the bundle — they must copy the one assembled
    here.
    """
    history = [
        {"role": "user", "content": "scan 5.5.5.5"},
        {"role": "assistant", "content": "Starting nmap on 5.5.5.5"},
        {"role": "user", "content": "enumerate it"},
    ]
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=202,
        user_id=1,
        message="enumerate it",
        conversation_id="conv-202",
        history=history,
        agent_mode=AgentMode.FULL_ACCESS,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    bundle = config.metadata[METADATA_CONTEXT_BUNDLE_KEY]

    assert bundle["conversation_id"] == "conv-202"
    transcript_turns = bundle["transcript_window"]["turns"]
    assert [turn["content"] for turn in transcript_turns] == [
        "scan 5.5.5.5",
        "Starting nmap on 5.5.5.5",
        "enumerate it",
    ]
    # At turn start, runtime state is explicit-empty (populated by the
    # working-memory node on first hit inside the graph).
    assert bundle["runtime_state"]["active_target"] is None
    assert bundle["runtime_state"]["handles"] == {}


def test_agent_mode_plan_derives_deep_reasoning_route_policy() -> None:
    """`agent_mode=plan` emits `execution_route_policy` forcing DR.

    Task 1.1: the context builder is the one place where `agent_mode`
    enters runtime metadata, so route-policy derivation lives here. The
    policy object is the single durable forced-route authority consumed
    downstream by the classifier prompt, facade branch selection, and
    the deep-reasoning graph-entry override.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=401,
        user_id=1,
        message="plan a multi-step enumeration",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    policy = config.metadata["execution_route_policy"]

    assert policy == {
        "source": "agent_mode",
        "agent_mode": "plan",
        "forced_execution_mode": "deep_reasoning",
        "forced_classifier_label": "plan_executor",
    }
    assert config.metadata["plan_review_required"] is True
    assert "agent_execution_profile" not in config.metadata
    assert "todo_bootstrap_mode" not in config.metadata
    assert "plan_visibility" not in config.metadata


def test_agent_mode_chat_derives_normal_chat_route_policy() -> None:
    """`agent_mode=chat` emits `execution_route_policy` forcing normal chat."""
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=402,
        user_id=1,
        message="just chat",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.CHAT,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    policy = config.metadata["execution_route_policy"]

    assert policy == {
        "source": "agent_mode",
        "agent_mode": "chat",
        "forced_execution_mode": "normal_chat",
        "forced_classifier_label": "simple_chat",
    }


def test_agent_mode_agent_preserves_no_route_policy() -> None:
    """`agent_mode=agent` must not emit `execution_route_policy`.

    Task 1.2: `agent` is not a branch selector — it only changes HITL
    tool-approval behavior (see `agent/graph/nodes/hitl_helpers.py`).
    The route-policy metadata key is reserved for `plan` / `chat` as
    the single durable forced-route authority, so downstream consumers
    can treat "key absent" as "no forced route". Preserving this
    invariant keeps existing `agent` tier behavior unchanged.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=501,
        user_id=1,
        message="execute with approvals",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)

    assert "execution_route_policy" not in config.metadata
    assert config.metadata["agent_mode"] == "agent"


def test_agent_mode_full_access_preserves_no_route_policy() -> None:
    """`agent_mode=full_access` must not emit `execution_route_policy`.

    Task 1.2: `agent_full` is the default autonomous behavior and is
    not a branch selector. Omitting the route-policy key for this tier
    (and `agent`) keeps "policy present?" checks trivial for
    downstream consumers and preserves existing autonomous behavior.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=502,
        user_id=1,
        message="do the thing",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)

    assert "execution_route_policy" not in config.metadata
    assert config.metadata["agent_mode"] == "full_access"


def test_safety_pattern_drops_plan_route_policy() -> None:
    """Safety guardrails outrank `plan` tier selection.

    Task 1.3: safety-triggered `forced_capability` (set by
    `intent_signals.collect_intent_signals` via `SAFETY_PATTERNS`) must
    outrank a user-facing `agent_mode=plan` selection. The
    `execution_route_policy` must be dropped so downstream consumers
    fall back to the safety-driven forced-capability path; allowing
    both keys simultaneously would re-introduce two conflicting
    forced-route authorities.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=601,
        user_id=1,
        # Matches `dangerous_shell_command` pattern in intent_signals.
        message="please run rm -rf /",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)

    assert config.metadata["forced_capability"] == "respond_only"
    assert "execution_route_policy" not in config.metadata
    # `agent_mode` is still surfaced as audit metadata.
    assert config.metadata["agent_mode"] == "plan"


def test_force_simple_chat_flag_drops_plan_route_policy() -> None:
    """Global `force_simple_chat` outranks `plan` tier selection.

    Task 1.3: when the caller pre-sets `force_simple_chat=True` in
    metadata (the global deployment toggle surface), the resulting
    safety-style `forced_capability=respond_only` must outrank any
    `agent_mode=plan` selection. This preserves the single-authority
    invariant — the already-in-effect `forced_capability` path wins,
    the route-policy key is dropped.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=602,
        user_id=1,
        message="plan an enumeration",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
    )

    config = builder.build_runtime_config(
        chat_inputs=chat_inputs,
        metadata={"force_simple_chat": True},
    )

    assert config.metadata["forced_capability"] == "respond_only"
    assert "execution_route_policy" not in config.metadata


def test_plan_route_policy_retained_without_safety_or_force_simple_chat() -> None:
    """Negative control: non-safety `plan` turn keeps its route policy.

    Task 1.3: the precedence drop is gated strictly on
    `forced_capability` being present. A benign `plan` turn with no
    safety trigger and no `force_simple_chat` flag must retain its
    `execution_route_policy` — otherwise the drop logic would corrupt
    the happy path wired in Task 1.1.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=603,
        user_id=1,
        message="plan a multi-step enumeration",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)

    assert "forced_capability" not in config.metadata
    assert config.metadata["execution_route_policy"]["agent_mode"] == "plan"


def test_conflict_plan_requested_normal_chat_raises() -> None:
    """`agent_mode=plan` + `requested_mode=NORMAL_CHAT` must raise.

    Task 1.4: both inputs are authoritative forced-route surfaces
    (`agent_mode` user-surface, `requested_mode` internal-caller). When
    they disagree the caller is miswired — silent precedence would
    hide the bug behind whichever consumer happens to read first. The
    builder raises `ConflictingExecutionRouteError` before doing any
    other setup so the failure surfaces at the true source.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=701,
        user_id=1,
        message="plan something",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
        requested_mode=ExecutionMode.NORMAL_CHAT,
    )
    with pytest.raises(ConflictingExecutionRouteError):
        builder.build_runtime_config(chat_inputs=chat_inputs)


def test_conflict_chat_requested_deep_reasoning_raises() -> None:
    """`agent_mode=chat` + `requested_mode=DEEP_REASONING` must raise."""
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=702,
        user_id=1,
        message="just chat",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.CHAT,
        requested_mode=ExecutionMode.DEEP_REASONING,
    )
    with pytest.raises(ConflictingExecutionRouteError):
        builder.build_runtime_config(chat_inputs=chat_inputs)


def test_conflict_plan_requested_simple_tool_raises() -> None:
    """`agent_mode=plan` + `requested_mode=SIMPLE_TOOL` must raise."""
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=703,
        user_id=1,
        message="plan something",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
        requested_mode=ExecutionMode.SIMPLE_TOOL,
    )
    with pytest.raises(ConflictingExecutionRouteError):
        builder.build_runtime_config(chat_inputs=chat_inputs)


def test_agreeing_plan_and_requested_deep_reasoning_does_not_raise() -> None:
    """Matching `agent_mode=plan` + `requested_mode=DEEP_REASONING` is accepted.

    Task 1.4: the check only fires on disagreement. When an internal
    caller happens to set `requested_mode` to the same value
    `agent_mode` would have forced, the invocation is well-formed and
    must pass through unchanged.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=704,
        user_id=1,
        message="plan something",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.PLAN,
        requested_mode=ExecutionMode.DEEP_REASONING,
    )
    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    assert config.metadata["execution_route_policy"]["forced_execution_mode"] == "deep_reasoning"


def test_agent_mode_without_route_policy_passes_through_requested_mode() -> None:
    """`agent`/`agent_full` bypass the conflict check.

    Task 1.4: only `plan` / `chat` derive a route policy — they are
    the only modes that carry a "forced execution mode" expectation.
    `agent` and `agent_full` are passthrough for routing, so internal
    callers that set `requested_mode` alongside them are not
    conflicting; the pre-existing `requested_mode or NORMAL_CHAT`
    initialization path continues to work unchanged.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=705,
        user_id=1,
        message="hello",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
        requested_mode=ExecutionMode.DEEP_REASONING,
    )
    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    assert config.execution_mode is ExecutionMode.DEEP_REASONING
    assert "execution_route_policy" not in config.metadata


def test_plan_mode_with_agent_derives_deep_reasoning_route_policy() -> None:
    """Phase 6 Task 6.3: ``plan_mode=True`` + ``agent_mode=agent`` forces DR.

    Plan is a route overlay, not a primary mode. When stacked on top of
    ``agent`` it must emit the same deep-reasoning route policy as the
    legacy ``agent_mode=plan`` path did, while keeping ``agent_mode``
    audit metadata intact so tool approval still keys off ``agent``.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=801,
        user_id=1,
        message="plan something",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=True,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    policy = config.metadata["execution_route_policy"]

    assert policy == {
        "source": "plan_mode",
        "agent_mode": "agent",
        "plan_mode": True,
        "forced_execution_mode": "deep_reasoning",
        "forced_classifier_label": "plan_executor",
    }
    # ``agent_mode`` is preserved as-is so downstream tool-approval
    # semantics remain ``agent`` (HITL prompts) rather than collapsing
    # to legacy ``plan``.
    assert config.metadata["agent_mode"] == "agent"
    assert config.metadata["plan_mode"] is True
    assert config.metadata["plan_review_required"] is True
    assert "agent_execution_profile" not in config.metadata
    assert "todo_bootstrap_mode" not in config.metadata
    assert "plan_visibility" not in config.metadata


def test_plan_mode_with_full_access_derives_deep_reasoning_route_policy() -> None:
    """Phase 6 Task 6.3: ``plan_mode=True`` + ``agent_mode=full_access`` forces DR.

    The new UX contract explicitly supports ``Full Access + Plan``:
    deep-reasoning execution with no tool-use approval prompts. The
    route policy must force DR, and ``agent_mode`` must stay
    ``full_access`` so ``should_require_approval`` keeps returning
    False for the turn.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=802,
        user_id=1,
        message="plan something autonomous",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
        plan_mode=True,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    policy = config.metadata["execution_route_policy"]

    assert policy["source"] == "plan_mode"
    assert policy["agent_mode"] == "full_access"
    assert policy["forced_execution_mode"] == "deep_reasoning"
    assert policy["forced_classifier_label"] == "plan_executor"
    assert config.metadata["agent_mode"] == "full_access"
    assert config.metadata["plan_mode"] is True


def test_plan_mode_false_with_agent_preserves_no_route_policy() -> None:
    """Phase 6 Task 6.3: ``plan_mode=False`` + ``agent_mode=agent`` is passthrough.

    Turning the overlay off on a vanilla ``agent`` turn must restore
    the pre-Phase-6 behavior — no forced route policy.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=803,
        user_id=1,
        message="do things with approvals",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=False,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)

    assert "execution_route_policy" not in config.metadata
    assert config.metadata["agent_mode"] == "agent"
    assert config.metadata["plan_mode"] is False
    assert config.metadata["plan_review_required"] is False
    assert "agent_execution_profile" not in config.metadata
    assert "todo_bootstrap_mode" not in config.metadata
    assert "plan_visibility" not in config.metadata


def test_build_metadata_omits_removed_quick_profile_audit_keys() -> None:
    """Initial graph state metadata no longer carries DR quick experiment keys."""
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=807,
        user_id=1,
        message="check the target",
        conversation_id="conv-807",
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=False,
    )
    config = builder.build_runtime_config(
        chat_inputs=chat_inputs,
        metadata={
            "original_capability": "simple_tool_execution",
            "original_execution_mode": "simple_tool_execution",
        },
    )

    initial_state_metadata = build_metadata(chat_inputs, config)

    assert initial_state_metadata["plan_mode"] is False
    assert initial_state_metadata["plan_review_required"] is False
    assert "agent_execution_profile" not in initial_state_metadata
    assert "todo_bootstrap_mode" not in initial_state_metadata
    assert "plan_visibility" not in initial_state_metadata
    assert "original_capability" not in initial_state_metadata
    assert "original_execution_mode" not in initial_state_metadata
    assert "intent_router_graph_entry_override" not in initial_state_metadata


def test_build_metadata_does_not_add_quick_dr_graph_entry_override() -> None:
    """Removed DR quick audit keys must not create a graph-entry override."""
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=810,
        user_id=1,
        message="check the target",
        conversation_id="conv-810",
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=False,
        requested_mode=ExecutionMode.DEEP_REASONING,
    )
    config = builder.build_runtime_config(
        chat_inputs=chat_inputs,
        metadata={
            "original_capability": "simple_tool_execution",
            "original_execution_mode": "simple_tool_execution",
        },
    )

    initial_state_metadata = build_metadata(chat_inputs, config)

    assert initial_state_metadata["plan_review_required"] is False
    assert "agent_execution_profile" not in initial_state_metadata
    assert "todo_bootstrap_mode" not in initial_state_metadata
    assert "original_capability" not in initial_state_metadata
    assert "original_execution_mode" not in initial_state_metadata
    assert "intent_router_graph_entry_override" not in initial_state_metadata


def test_build_initial_interactive_state_exposes_plan_mode_metadata() -> None:
    """Initial interactive state exposes route policy metadata under facts."""
    builder = LangGraphContextBuilder()
    plan_inputs = ChatInputs(
        task_id=808,
        user_id=1,
        message="make a plan",
        conversation_id="conv-808",
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=True,
    )
    non_plan_inputs = ChatInputs(
        task_id=809,
        user_id=1,
        message="check the target",
        conversation_id="conv-809",
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=False,
    )

    plan_config = builder.build_runtime_config(chat_inputs=plan_inputs)
    non_plan_config = builder.build_runtime_config(chat_inputs=non_plan_inputs)
    plan_state, plan_injected_tokens = build_initial_interactive_state(plan_config)
    non_plan_state, non_plan_injected_tokens = build_initial_interactive_state(non_plan_config)
    plan_metadata = plan_state["facts"]["metadata"]
    non_plan_metadata = non_plan_state["facts"]["metadata"]

    assert plan_injected_tokens is None
    assert non_plan_injected_tokens is None
    assert plan_metadata["plan_mode"] is True
    assert plan_metadata["plan_review_required"] is True
    assert "agent_execution_profile" not in plan_metadata
    assert "todo_bootstrap_mode" not in plan_metadata
    assert "plan_visibility" not in plan_metadata
    assert non_plan_metadata["plan_mode"] is False
    assert non_plan_metadata["plan_review_required"] is False
    assert "agent_execution_profile" not in non_plan_metadata
    assert "todo_bootstrap_mode" not in non_plan_metadata
    assert "plan_visibility" not in non_plan_metadata
    json.dumps(plan_metadata)
    json.dumps(non_plan_metadata)


def test_chat_plus_plan_mode_raises_conflicting_route_error() -> None:
    """Phase 6 Task 6.4: ``agent_mode=chat`` + ``plan_mode=True`` is rejected.

    Chat and Plan are mutually exclusive in the new UX contract. The
    context builder raises ``ConflictingExecutionRouteError`` as a
    defense-in-depth check for service-layer callers — the HTTP
    boundary rejects the same combo with 422 before reaching here.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=804,
        user_id=1,
        message="chat AND plan?",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.CHAT,
        plan_mode=True,
    )

    with pytest.raises(ConflictingExecutionRouteError):
        builder.build_runtime_config(chat_inputs=chat_inputs)


def test_plan_mode_conflict_with_requested_normal_chat_raises() -> None:
    """Phase 6 Task 6.3: ``plan_mode=True`` + conflicting ``requested_mode`` raises.

    When a service-layer caller sets ``plan_mode=True`` alongside
    ``requested_mode=NORMAL_CHAT`` the two inputs disagree on the
    forced branch. Silent precedence would hide the miswiring — the
    builder raises the same ``ConflictingExecutionRouteError`` used
    for the legacy ``agent_mode=plan`` path.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=805,
        user_id=1,
        message="plan something",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=True,
        requested_mode=ExecutionMode.NORMAL_CHAT,
    )

    with pytest.raises(ConflictingExecutionRouteError):
        builder.build_runtime_config(chat_inputs=chat_inputs)


def test_safety_pattern_drops_plan_mode_route_policy() -> None:
    """Phase 6: safety guardrails outrank ``plan_mode`` overlay.

    The precedence order is: safety -> user-tier route policy ->
    classifier label -> heuristic fallback. A safety-triggered
    ``forced_capability=respond_only`` must drop the Plan-overlay
    route policy so downstream consumers fall back to the safety
    path, matching the behavior pinned for legacy ``agent_mode=plan``
    in Task 1.3.
    """
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=806,
        user_id=1,
        # Matches `dangerous_shell_command` pattern in intent_signals.
        message="please run rm -rf /",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
        plan_mode=True,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)

    assert config.metadata["forced_capability"] == "respond_only"
    assert "execution_route_policy" not in config.metadata
    # Audit metadata still surfaces both the tier and the overlay.
    assert config.metadata["agent_mode"] == "agent"
    assert config.metadata["plan_mode"] is True


def test_build_metadata_reuses_context_builder_bundle_without_rebuilding() -> None:
    """Single assembly authority: facade_helpers.build_metadata copies, not rebuilds.

    The bundle placed in ``runtime_config.metadata`` by the context
    builder must be the exact same dict reference exposed on the
    initial graph state metadata — proving no duplicate assembly path
    exists (Phase 6 cleanup).
    """
    history = [{"role": "user", "content": "hello"}]
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=303,
        user_id=1,
        message="hello",
        conversation_id="conv-303",
        history=history,
        agent_mode=AgentMode.FULL_ACCESS,
    )
    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    bundle_from_builder = config.metadata[METADATA_CONTEXT_BUNDLE_KEY]

    initial_state_metadata = build_metadata(chat_inputs, config)

    # Identity check: same object, no rebuild.
    assert initial_state_metadata[METADATA_CONTEXT_BUNDLE_KEY] is bundle_from_builder


def test_build_metadata_preserves_provider_identity_for_graph_resolver() -> None:
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=305,
        user_id=1,
        message="hello",
        conversation_id="conv-305",
        history=[],
        api_key="key",
        model="fake-chat",
        agent_mode=AgentMode.FULL_ACCESS,
    )
    config = builder.build_runtime_config(
        chat_inputs=chat_inputs,
        metadata={"provider": "fake"},
    )

    initial_state_metadata = build_metadata(chat_inputs, config)

    assert initial_state_metadata["provider"] == "fake"
    assert initial_state_metadata["model"] == "fake-chat"
    assert "api_key" not in initial_state_metadata
    assert initial_state_metadata["graph_runtime_context"]["provider"] == "fake"
    assert initial_state_metadata["graph_runtime_context"]["model"] == "fake-chat"
    assert "api_key" not in initial_state_metadata["graph_runtime_context"]
    assert isinstance(config.metadata["graph_runtime_context"], dict)
    assert config.metadata["graph_runtime_context"]["provider"] == "fake"


def test_build_runtime_config_projects_runtime_identity_into_graph_context(monkeypatch) -> None:
    runtime_projection = {
        "task_id": 306,
        "user_id": 1,
        "tenant_id": 11,
        "graph_thread_id": "task-306:conv-306",
        "runtime_placement_mode": "local",
        "workspace_id": "task-306",
        "actor_type": "agent",
        "actor_id": "langgraph",
        "runner_id": "runner-a",
        "execution_site_id": "site-a",
        "workspace_path": "/tmp/task-306",
    }
    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_provider_runtime_projection",
        lambda *, task_id, user_id: runtime_projection,
    )
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=306,
        user_id=1,
        message="hello",
        conversation_id="conv-306",
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    initial_state_metadata = build_metadata(chat_inputs, config)

    assert config.metadata["runtime_provider_projection"] == runtime_projection
    for key in (
        "tenant_id",
        "runtime_placement_mode",
        "workspace_id",
        "actor_type",
        "actor_id",
        "runner_id",
        "execution_site_id",
        "workspace_path",
    ):
        assert config.metadata[key] == runtime_projection[key]
        assert initial_state_metadata["graph_runtime_context"][key] == runtime_projection[key]


def test_build_runtime_config_seeds_environment_info_for_graph_metadata(monkeypatch) -> None:
    """Runtime environment info is shared turn metadata, not graph-owned setup."""
    runtime_projection = {
        "task_id": 309,
        "user_id": 1,
        "tenant_id": 11,
        "graph_thread_id": "task-309:conv-309",
        "runtime_placement_mode": "local",
        "workspace_id": "task-309",
        "actor_type": "agent",
        "actor_id": "langgraph",
    }
    environment_info = {
        "hostname": "kali-task-309",
        "network": {
            "interfaces": [
                {"name": "eth0", "ipv4": "172.17.0.2/16", "state": "UP"}
            ],
            "default_gateway": "172.17.0.1",
            "dns_servers": ["8.8.8.8"],
        },
        "routes": [{"destination": "default", "gateway": "172.17.0.1", "interface": "eth0"}],
    }
    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_provider_runtime_projection",
        lambda *, task_id, user_id: runtime_projection,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_runtime_environment_info",
        lambda *, task_id, user_id: environment_info,
    )
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=309,
        user_id=1,
        message="scan the reachable network",
        conversation_id="conv-309",
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    initial_state_metadata = build_metadata(chat_inputs, config)

    assert config.metadata["environment_info"] == environment_info
    assert initial_state_metadata["environment_info"] == environment_info


def test_build_runtime_config_skips_non_environment_metadata(monkeypatch) -> None:
    """Flat runtime metadata must not masquerade as container environment info."""
    assert _is_canonical_environment_info({"agent.version": "4.0.0"}) is False

    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_provider_runtime_projection",
        lambda *, task_id, user_id: {},
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_runtime_environment_info",
        lambda *, task_id, user_id: None,
    )
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=310,
        user_id=1,
        message="hello",
        conversation_id="conv-310",
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    initial_state_metadata = build_metadata(chat_inputs, config)

    assert "environment_info" not in config.metadata
    assert "environment_info" not in initial_state_metadata


def test_build_runtime_config_runner_projection_drops_workspace_path(monkeypatch) -> None:
    runtime_projection = {
        "task_id": 307,
        "user_id": 1,
        "tenant_id": 11,
        "graph_thread_id": "task-307:conv-307",
        "runtime_placement_mode": "runner",
        "workspace_id": "task-307",
        "actor_type": "agent",
        "actor_id": "langgraph",
        "runner_id": "runner-a",
        "execution_site_id": "site-a",
        "workspace_path": "/tmp/task-307",
    }
    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_provider_runtime_projection",
        lambda *, task_id, user_id: runtime_projection,
    )
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=307,
        user_id=1,
        message="hello",
        conversation_id="conv-307",
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
    )

    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    initial_state_metadata = build_metadata(chat_inputs, config)

    assert "workspace_path" not in config.metadata
    assert initial_state_metadata["graph_runtime_context"]["workspace_path"] is None


def test_build_runtime_config_rejects_partial_runtime_identity_projection(monkeypatch) -> None:
    runtime_projection = {
        "task_id": 308,
        "user_id": 1,
        "tenant_id": 11,
        "runtime_placement_mode": "runner",
        # Missing workspace_id/actor_type/actor_id should fail closed.
    }
    monkeypatch.setattr(
        "backend.services.langgraph_chat.context_builder._resolve_provider_runtime_projection",
        lambda *, task_id, user_id: runtime_projection,
    )
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=308,
        user_id=1,
        message="hello",
        conversation_id="conv-308",
        history=[],
        agent_mode=AgentMode.FULL_ACCESS,
    )

    with pytest.raises(RuntimeError, match="workspace_id"):
        builder.build_runtime_config(chat_inputs=chat_inputs)


def test_build_metadata_keeps_prior_turn_references_inside_bundle() -> None:
    """Canonical prior-turn text must not become a top-level prompt carrier."""
    builder = LangGraphContextBuilder()
    chat_inputs = ChatInputs(
        task_id=304,
        user_id=1,
        message="continue that",
        conversation_id="conv-304",
        history=[{"role": "user", "content": "Run service enumeration."}],
        agent_mode=AgentMode.FULL_ACCESS,
    )
    config = builder.build_runtime_config(chat_inputs=chat_inputs)
    config.metadata["prior_turn_references"] = {
        "operation": "continuation",
        "status": "ok",
        "materialized_turns": [
            {
                "turn_number": 1,
                "speaker": "user",
                "message_id": 9,
                "text": "Run service enumeration.",
            }
        ],
        "unresolved_hints": [],
    }

    initial_state_metadata = build_metadata(chat_inputs, config)
    bundle = initial_state_metadata[METADATA_CONTEXT_BUNDLE_KEY]

    assert "prior_turn_references" not in initial_state_metadata
    assert bundle["prior_turn_references"]["materialized_turns"][0]["text"] == (
        "Run service enumeration."
    )
