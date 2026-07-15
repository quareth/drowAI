"""Phase 3 Task 3.3: facade tests for conflicting classifier vs route-policy.

These tests pin the guide's invariant that the user-surface tier
selection (``agent_mode=plan`` / ``chat``) is authoritative for
backend branch selection — even when the intent classifier emits a
disagreeing routing label. The classifier stays on the path so its
interpretation feeds the briefs, but its label does NOT decide the
backend handler when an ``execution_route_policy`` is in effect.

Setup uses the real ``LangGraphContextBuilder`` so the
``execution_route_policy`` derivation in
``backend/services/langgraph_chat/context_builder.py`` is exercised in
the same code path the production wiring uses; only the LLMClient and
the per-branch handlers are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from backend.services.langgraph_chat.contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.exceptions import PlanModeUnavailableError
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.intent.classifier import IntentClassifier
from backend.services.langgraph_chat.routing.selectors import ChatBranch


class _StubHub:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
        self.events.append({"task_id": task_id, "event": event})


def _stub_classifier_returning(label: str) -> IntentClassifier:
    """Return an IntentClassifier wired to a deterministic LLM stub.

    The stub emits the given canonical routing ``label`` so the test
    can simulate a classifier that disagrees with the user-surface tier
    forced by ``agent_mode``.
    """
    response = (
        '{"label": "' + label + '", "confidence": 0.99, '
        '"reasoning": "stub", "suggested_capabilities": [], '
        '"risk_flags": []}'
    )

    class _StubClient:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return SimpleNamespace(content=response, usage=None, structured_output=None)

    return IntentClassifier(client_factory=lambda *_: _StubClient())


def _build_facade_with_branch_capture(
    *,
    intent_classifier: IntentClassifier,
) -> tuple[LangGraphChatFacade, Dict[str, ChatBranch]]:
    """Wire a facade where every handler records the branch it was hit on."""
    capture: Dict[str, ChatBranch] = {}
    facade = LangGraphChatFacade(intent_classifier=intent_classifier)

    def _make_capture(branch: ChatBranch):
        async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
            capture["branch"] = branch
            capture["execution_mode"] = config.execution_mode
            capture["metadata_snapshot"] = dict(config.metadata)
            return LangGraphChatResult(
                final_text="stub",
                conversation_id=config.chat_inputs.conversation_id,
            )

        return SimpleNamespace(handle=_handle)

    facade._handlers = {branch: _make_capture(branch) for branch in facade._handlers}
    return facade, capture


@pytest.fixture(autouse=True)
def _stub_in_memory_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the streaming hub so the facade's pre-branch reasoning never blocks."""
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )


@pytest.mark.asyncio
async def test_plan_with_classifier_simple_chat_lands_on_deep_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent_mode=plan` + classifier emits `simple_chat` -> DeepReasoningHandler.

    Phase 3 Task 3.3 / guide §"Recommended Test Matrix > Facade Branch
    Selection". The classifier is on the path (its label is preserved
    on metadata as `intent_classifier_raw_label`) but the branch
    selection follows the user-surface route policy.
    """
    facade, capture = _build_facade_with_branch_capture(
        intent_classifier=_stub_classifier_returning("simple_chat"),
    )

    chat_inputs = ChatInputs(
        task_id=8001,
        user_id=1,
        message="please plan it",
        conversation_id="conv-8001",
        history=[{"role": "user", "content": "please plan it"}],
        api_key="test-key",
        agent_mode=AgentMode.PLAN,
    )

    await facade.handle_turn(chat_inputs)

    assert capture["branch"] is ChatBranch.DEEP_REASONING
    assert capture["execution_mode"] is ExecutionMode.DEEP_REASONING
    snapshot = capture["metadata_snapshot"]
    # Classifier label was preserved (it disagreed with the policy).
    assert snapshot["intent_classifier_label"] == "simple_chat"
    assert snapshot["intent_classifier_raw_label"] == "simple_chat"
    # Route policy was applied to drive the backend branch.
    assert snapshot["intent_classifier_route_forced"] is True
    assert snapshot["intent_classifier_route_force_source"] == "agent_mode=plan"
    assert snapshot["execution_route_policy_applied"] is True


@pytest.mark.asyncio
async def test_chat_with_classifier_plan_executor_lands_on_normal_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent_mode=chat` + classifier emits `plan_executor` -> NormalChatHandler."""
    facade, capture = _build_facade_with_branch_capture(
        intent_classifier=_stub_classifier_returning("plan_executor"),
    )

    chat_inputs = ChatInputs(
        task_id=8002,
        user_id=1,
        message="just chat please",
        conversation_id="conv-8002",
        history=[{"role": "user", "content": "just chat please"}],
        api_key="test-key",
        agent_mode=AgentMode.CHAT,
    )

    await facade.handle_turn(chat_inputs)

    assert capture["branch"] is ChatBranch.NORMAL_CHAT
    assert capture["execution_mode"] is ExecutionMode.NORMAL_CHAT
    snapshot = capture["metadata_snapshot"]
    assert snapshot["intent_classifier_label"] == "plan_executor"
    assert snapshot["intent_classifier_raw_label"] == "plan_executor"
    assert snapshot["intent_classifier_route_forced"] is True
    assert snapshot["intent_classifier_route_force_source"] == "agent_mode=chat"
    assert snapshot["execution_route_policy_applied"] is True


@pytest.mark.asyncio
async def test_plan_with_deep_reasoning_disabled_raises_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 5 Task 5.2: `agent_mode=plan` + DR disabled -> raise.

    Guide §"Feature-Flag And Misconfiguration Policy". The facade must
    fail closed before classifier invocation, not silently downgrade to
    normal chat. The user explicitly selected Plan; silent fallback
    re-introduces the original Plan-tier bug in a harder-to-debug way.
    """
    facade, capture = _build_facade_with_branch_capture(
        intent_classifier=_stub_classifier_returning("plan_executor"),
    )

    chat_inputs = ChatInputs(
        task_id=8101,
        user_id=1,
        message="please plan it",
        conversation_id="conv-8101",
        history=[{"role": "user", "content": "please plan it"}],
        api_key="test-key",
        agent_mode=AgentMode.PLAN,
    )

    # Disable deep reasoning at the deployment level. The context
    # builder forwards this into runtime metadata, and the facade's
    # gate reads from `feature_flags["deep_reasoning_enabled"]`.
    with pytest.raises(PlanModeUnavailableError):
        await facade.handle_turn(
            chat_inputs,
            metadata={"feature_flags": {"deep_reasoning_enabled": False}},
        )

    # Critical guarantee: no handler was invoked, no silent downgrade.
    assert "branch" not in capture


@pytest.mark.asyncio
async def test_chat_with_deep_reasoning_disabled_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 5 Task 5.2 negative control: `chat` with DR disabled is fine.

    The fail-closed gate must be scoped to plan-tier requests. Chat
    mode targets normal_chat, which is the fallback branch — it must
    not be rejected just because deep reasoning is unavailable.
    """
    facade, capture = _build_facade_with_branch_capture(
        intent_classifier=_stub_classifier_returning("simple_chat"),
    )

    chat_inputs = ChatInputs(
        task_id=8102,
        user_id=1,
        message="hello",
        conversation_id="conv-8102",
        history=[{"role": "user", "content": "hello"}],
        api_key="test-key",
        agent_mode=AgentMode.CHAT,
    )
    await facade.handle_turn(
        chat_inputs,
        metadata={"feature_flags": {"deep_reasoning_enabled": False}},
    )
    assert capture["branch"] is ChatBranch.NORMAL_CHAT


@pytest.mark.asyncio
async def test_full_access_with_deep_reasoning_disabled_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 5 Task 5.2 negative control: non-plan tiers bypass the gate.

    `agent_full` carries no route policy, so the fail-closed gate must
    NOT fire for it. Pre-existing branch behavior (silent downgrade
    handled by the global feature flag check further down) is left
    untouched — the only contract being pinned here is "no exception
    is raised for non-plan tiers".
    """
    facade, capture = _build_facade_with_branch_capture(
        intent_classifier=_stub_classifier_returning("plan_executor"),
    )

    chat_inputs = ChatInputs(
        task_id=8103,
        user_id=1,
        message="do the thing",
        conversation_id="conv-8103",
        history=[{"role": "user", "content": "do the thing"}],
        api_key="test-key",
        agent_mode=AgentMode.FULL_ACCESS,
    )
    # No PlanModeUnavailableError is raised even though the feature
    # flag is off, because no `execution_route_policy` was emitted.
    await facade.handle_turn(
        chat_inputs,
        metadata={"feature_flags": {"deep_reasoning_enabled": False}},
    )
    assert "branch" in capture, "facade must still invoke a handler for non-plan tiers"


@pytest.mark.asyncio
async def test_full_access_without_route_policy_still_follows_classifier_label() -> None:
    """`agent_mode=full_access` has no route policy — classifier label decides.

    Negative control: the route-policy enforcement must NOT activate
    when no policy was emitted. `agent` and `agent_full` are the two
    tiers that intentionally do not derive a policy (Task 1.2), so the
    classifier-derived `execution_mode` continues to drive the branch.
    """
    facade, capture = _build_facade_with_branch_capture(
        intent_classifier=_stub_classifier_returning("plan_executor"),
    )

    chat_inputs = ChatInputs(
        task_id=8003,
        user_id=1,
        message="do the thing",
        conversation_id="conv-8003",
        history=[{"role": "user", "content": "do the thing"}],
        api_key="test-key",
        agent_mode=AgentMode.FULL_ACCESS,
    )

    await facade.handle_turn(chat_inputs)

    # Classifier label `plan_executor` -> deep reasoning, no policy override.
    assert capture["branch"] is ChatBranch.DEEP_REASONING
    snapshot = capture["metadata_snapshot"]
    assert "execution_route_policy" not in snapshot
    assert snapshot["intent_classifier_route_forced"] is False
