"""Phase 4/5 authority-cutover regressions for hot-path prompt assembly.

Locks in the contract that after the Phase 5 cutover:

- Every hot-path prompt consumer (intent classifier, category
  selector, planner, planner request context) reads continuity
  exclusively from ``metadata[METADATA_CONTEXT_BUNDLE_KEY]``. When
  the bundle is missing each consumer raises ``RuntimeError`` rather
  than silently falling back to a legacy channel.
- Garbage values placed on legacy continuity keys
  (``metadata["conversation_history"]``) do **not** leak into the
  prompt surface — prompt consumers never read them anymore.
- ``metadata["long_term_memory_summary"]`` does not re-enter the
  hot-path prompt assembly for either the planner or simple-chat
  paths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Force full graph package init to break the pre-existing circular import
# between planner_service and agent.graph.nodes / agent.graph.builders.
import agent.graph.builders  # noqa: F401  # side-effect: break import cycle
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.nodes.select_tool_categories import select_tool_categories_node
from agent.graph.nodes.simple_chat import _build_simple_chat_messages
from agent.graph.subgraphs.tool_execution_runtime import planner_service
from agent.graph.subgraphs.tool_execution_runtime.request_context import (
    _resolve_planner_history,
)
from agent.tool_runtime import ToolExecutionRequest
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.intent.classifier import IntentClassifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_history(turn_count: int) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for i in range(turn_count):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
    return history


def _install_bundle(
    metadata: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-authority",
        turn_id="turn-authority",
        turn_sequence=0,
        messages=list(messages),
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    return bundle


class _CapturingClient:
    """Stub LLMClient recording the user prompt it received."""

    def __init__(self, response: str = "{}") -> None:
        self.response = response
        self.calls = 0
        self.last_user_prompt: Optional[str] = None

    async def chat_with_usage(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        **_: Any,
    ) -> Any:
        self.calls += 1
        self.last_user_prompt = user_prompt
        return SimpleNamespace(
            content=self.response,
            usage=None,
            structured_output=None,
        )


def _runtime_config(
    metadata: Dict[str, Any],
    history: List[Dict[str, Any]],
    *,
    message: str = "follow up on that target",
) -> LangGraphRuntimeConfig:
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=7,
        message=message,
        conversation_id="conv-authority",
        history=history,
        api_key="test-key",
        model="gpt-5.2",
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        metadata=metadata,
        execution_mode=ExecutionMode.NORMAL_CHAT,
    )


def _state_with_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "facts": {
            "task_id": 1,
            "message": "scan the host",
            "selected_tool": None,
            "tool_parameters": {},
            "metadata": metadata,
        },
        "trace": {
            "history": [],
            "reasoning": [],
        },
    }


async def _run_category_selector_capturing_prompt(
    state: Dict[str, Any],
) -> str:
    captured_prompt: Dict[str, str] = {"value": ""}

    async def _capture_prompt(**kwargs):  # noqa: ANN003
        captured_prompt["value"] = kwargs["prompt"]
        return ["information_gathering"]

    with patch(
        "agent.tools.category_utils.get_tool_categories",
        return_value=["information_gathering", "web_applications"],
    ), patch(
        "agent.tools.category_utils.get_category_descriptions",
        return_value={
            "information_gathering": "Network recon",
            "web_applications": "Web testing",
        },
    ), patch(
        "agent.graph.nodes.select_tool_categories._call_llm_for_categories",
        new=AsyncMock(side_effect=_capture_prompt),
    ):
        await select_tool_categories_node(state)

    return captured_prompt["value"]


def _make_tool_execution_request(
    message: str = "follow up",
    history: Optional[List[Dict[str, Any]]] = None,
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message=message,
        history=list(history or []),
    )


# ---------------------------------------------------------------------------
# Hard-fail invariant (bundle required; legacy fallbacks removed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifier_hard_fails_when_bundle_missing() -> None:
    """Intent classifier: missing bundle must raise ``RuntimeError``."""
    history = _make_history(turn_count=2)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(metadata, history)
    stub = _CapturingClient("{}")
    classifier = IntentClassifier(client_factory=lambda call_settings: stub)

    with pytest.raises(RuntimeError, match="context_bundle"):
        await classifier.enrich_runtime_config(config)

    assert stub.calls == 0


# NOTE: ``test_category_selector_hard_fails_when_bundle_missing`` was
# removed. Runner control narrowed the category selector off the
# ``ConversationContextBundle`` entirely — it now consumes the
# classifier-derived ``intent_brief`` and tolerates a missing
# brief with ``"(none)"`` placeholders. The hard-fail invariant no
# longer applies at this seam; the equivalent "narrowed seam must not
# reacquire transcript symbols" guardrail lives in
# ``backend/tests/langgraph_chat/test_prompt_authority_boundary.py``.


def test_request_context_hard_fails_when_bundle_missing() -> None:
    """request_context: missing bundle must raise ``RuntimeError``."""
    metadata: Dict[str, Any] = {
        "history": [{"role": "user", "content": "LEGACY_MUST_NOT_LEAK"}],
    }

    with pytest.raises(RuntimeError, match="context_bundle"):
        _resolve_planner_history(metadata)


# ---------------------------------------------------------------------------
# Legacy memory keys must not affect prompt outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifier_ignores_legacy_memory_keys_when_bundle_present() -> None:
    """Garbage in legacy keys must not leak into the classifier prompt."""
    history = _make_history(turn_count=2)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        # Legacy channels filled with garbage that must NOT leak.
        "history": [{"role": "assistant", "content": "GARBAGE_LEGACY_HISTORY"}],
        "conversation_history": [
            {"role": "assistant", "content": "GARBAGE_LEGACY_CONV_HISTORY"},
        ],
        "long_term_memory_summary": "GARBAGE_LONG_TERM_MEMORY_SUMMARY",
    }
    _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)
    stub = _CapturingClient("{}")
    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    prompt = stub.last_user_prompt or ""
    assert "GARBAGE_LEGACY_HISTORY" not in prompt
    assert "GARBAGE_LEGACY_CONV_HISTORY" not in prompt
    assert "GARBAGE_LONG_TERM_MEMORY_SUMMARY" not in prompt
    # The bundle-derived content is present.
    assert "user message 0" in prompt
    assert "assistant reply 1" in prompt


@pytest.mark.asyncio
async def test_category_selector_ignores_legacy_memory_keys_when_bundle_present() -> None:
    """Garbage in legacy keys must not leak into the selector prompt.

    Runner control narrowed the category selector off the bundle and
    onto the classifier-derived ``intent_brief``. The positive
    bundle-continuity assertion was retired with the narrowing; the
    remaining invariant is the negative one: legacy memory keys must
    not leak into the prompt body regardless of what they carry.
    """
    history = _make_history(turn_count=2)
    metadata: Dict[str, Any] = {
        "api_key": "test-key",
        "history": [{"role": "user", "content": "GARBAGE_LEGACY_HISTORY"}],
        "conversation_history": [
            {"role": "user", "content": "GARBAGE_LEGACY_CONV_HISTORY"},
        ],
        "long_term_memory_summary": "GARBAGE_LONG_TERM_MEMORY_SUMMARY",
    }
    _install_bundle(metadata, history)
    state = _state_with_metadata(metadata)

    prompt = await _run_category_selector_capturing_prompt(state)

    assert "GARBAGE_LEGACY_HISTORY" not in prompt
    assert "GARBAGE_LEGACY_CONV_HISTORY" not in prompt
    assert "GARBAGE_LONG_TERM_MEMORY_SUMMARY" not in prompt


# NOTE: ``test_planner_service_ignores_legacy_memory_keys_when_bundle_present``
# was removed by the runner control follow-up cleanup (Fix 1). The planner
# service no longer projects a prompt-history list from the bundle —
# ``_resolve_planner_prompt_history`` was deleted and downstream
# callsites read ``intent_brief`` instead. The replacement
# legacy-leak invariants now live at the builder / seam level and are
# asserted by the brief-contract test suites.


def test_request_context_ignores_legacy_history_when_bundle_present() -> None:
    """request_context must read bundle only; legacy history ignored."""
    history = _make_history(turn_count=2)
    metadata: Dict[str, Any] = {
        "history": [{"role": "user", "content": "GARBAGE_LEGACY_HISTORY"}],
    }
    _install_bundle(metadata, history)

    resolved = _resolve_planner_history(metadata)
    contents = [entry.get("content") for entry in resolved]

    assert "GARBAGE_LEGACY_HISTORY" not in contents
    assert "user message 0" in contents


# ---------------------------------------------------------------------------
# Long-term memory summary is out of hot-path prompt authority
# ---------------------------------------------------------------------------


def test_planner_context_drops_long_term_memory_summary_from_hot_path() -> None:
    """``build_planner_context`` must omit the LTM key entirely."""
    history = _make_history(turn_count=2)
    metadata: Dict[str, Any] = {
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
        "long_term_memory_summary": "LTM_GARBAGE_SHOULD_BE_DROPPED_IN_HOT_PATH",
    }
    _install_bundle(metadata, history)

    interactive = MagicMock()
    interactive.facts.metadata = metadata
    interactive.facts.message = "follow up"
    interactive.facts.plan = []
    interactive.facts.current_goal = ""
    interactive.facts.next_tool_hint = None
    interactive.facts.selected_tool = None
    interactive.facts.tool_parameters = {}
    interactive.facts.todo_list = []
    interactive.facts.intent_hints = {"targets": []}
    interactive.trace.reasoning = []
    interactive.trace.observations = []

    request = _make_tool_execution_request(history=history)

    def _fake_catalog(*_args, **_kwargs):
        return ["tool.a"]

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_fake_catalog,
        get_full_tool_catalog_for_planner=_fake_catalog,
        working_memory_summary_max_chars=900,
    )

    # Hot-path prompt assembly no longer depends on LTM summary — the
    # planner context omits it entirely regardless of metadata contents.
    assert "long_term_memory_summary" not in planner_context


def test_hot_path_runtime_modules_do_not_pass_ltm_into_prompt_builders() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    scan_dirs = (
        repo_root / "agent" / "graph" / "subgraphs" / "tool_execution_runtime",
        repo_root / "agent" / "reasoning",
    )
    offenders: list[str] = []
    for directory in scan_dirs:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "long_term_memory_summary" in text:
                offenders.append(str(path.relative_to(repo_root)))

    assert not offenders, (
        "Hot-path runtime modules must not pass or thread "
        "`long_term_memory_summary` into prompt builders. Offenders: "
        f"{sorted(offenders)}"
    )


def test_runtime_modules_do_not_read_or_write_metadata_history() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    scan_dirs = (
        repo_root / "agent",
        repo_root / "backend",
    )
    history_key = '"history"'
    patterns = (f"metadata[{history_key}]", f"metadata.get({history_key})")
    offenders: list[str] = []
    for directory in scan_dirs:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(pattern in text for pattern in patterns):
                offenders.append(str(path.relative_to(repo_root)))

    assert not offenders, (
        "Runtime modules must not read/write metadata['history'] after "
        f"Phase 5. Offenders: {sorted(offenders)}"
    )


def test_memory_manager_name_collision_is_removed_from_context_layer() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    agent_root = repo_root / "agent"
    context_root = agent_root / "context"
    tests_root = agent_root / "tests"

    class_hits: list[str] = []
    for path in agent_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "class MemoryManager" in text:
            class_hits.append(str(path.relative_to(repo_root)))

    assert class_hits == ["agent/graph/memory/memory_manager.py"], (
        "Only graph reducer should declare class MemoryManager. "
        f"Found: {class_hits}"
    )

    context_hits: list[str] = []
    for root in (context_root, tests_root):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "MemoryManager" in text:
                context_hits.append(str(path.relative_to(repo_root)))

    assert not context_hits, (
        "Context layer/tests must not reference MemoryManager after rename. "
        f"Offenders: {sorted(context_hits)}"
    )


def test_simple_chat_messages_drop_long_term_memory_summary() -> None:
    """``_build_simple_chat_messages`` never injects LTM summary.

    The LTM store is unchanged; the simple-chat prompt assembly just
    does not read it anymore.
    """
    history = [
        {"role": "user", "content": "earlier user turn"},
        {"role": "assistant", "content": "earlier assistant turn"},
    ]
    messages = _build_simple_chat_messages(
        history,
        {"role": "user", "content": "current user turn"},
    )

    rendered = "\n".join(
        f"{m.get('role')}: {m.get('content')}" for m in messages
    )
    assert "Long-Term Memory" not in rendered
    assert "earlier user turn" in rendered
    assert "current user turn" in rendered


@pytest.mark.asyncio
async def test_simple_chat_reads_transcript_from_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simple chat resolves prior-turn transcript from the bundle.

    Locks in the single transcript-window authority across graphs:
    after this cutover, ``simple_chat_runtime['history']`` is ignored
    and the prior-turn messages handed to the LLM come from
    ``metadata['context_bundle']['transcript_window']['turns']``. The
    rest of the hot path (classifier / category selector / planner /
    articulation) reads the same window via the shared serializer; only
    the output shape differs (rendered text vs OpenAI message list).
    """
    from agent.graph.nodes import simple_chat as simple_chat_module
    from agent.graph.nodes.simple_chat import run_simple_chat
    from agent.graph.state import InteractiveInput, InteractiveState

    bundle_history = [
        {"role": "user", "content": "first prior user turn"},
        {"role": "assistant", "content": "first assistant reply"},
        {"role": "user", "content": "second prior user turn"},
        {"role": "assistant", "content": "second assistant reply"},
    ]
    bundle = build_conversation_context_bundle(
        conversation_id="conv-simple-bundle",
        turn_id="turn-simple-bundle",
        turn_sequence=0,
        messages=list(bundle_history),
        current_message="current user turn",
    )

    captured_messages: List[List[Dict[str, Any]]] = []

    class _RecordingClient:
        async def chat_messages_with_usage(self, messages: List[Dict[str, Any]], **_: Any) -> Any:
            captured_messages.append(list(messages))
            return SimpleNamespace(content="ok", usage=None)

        async def chat_messages(self, messages: List[Dict[str, Any]], **_: Any) -> str:
            captured_messages.append(list(messages))
            return "ok"

    monkeypatch.setattr(
        simple_chat_module,
        "resolve_llm_client",
        lambda *args, **kwargs: _RecordingClient(),
    )
    monkeypatch.setattr(
        simple_chat_module,
        "get_stream_writer",
        lambda: (_ for _ in ()).throw(RuntimeError("no writer in unit test")),
    )

    payload = InteractiveInput(
        task_id=42,
        message="current user turn",
        conversation_id="conv-simple-bundle",
        metadata={
            # ``simple_chat_runtime['history']`` is intentionally a
            # garbage value here. After the cutover the node must ignore
            # it and read the prior transcript from the bundle.
            "simple_chat_runtime": {"model": "stub", "history": [{"role": "user", "content": "GARBAGE"}]},
            METADATA_CONTEXT_BUNDLE_KEY: bundle,
        },
    )
    state = payload.to_state().as_graph_state()

    result = await run_simple_chat(state, context=None, config={"configurable": {"thread_id": "t-simple-bundle"}})

    InteractiveState.from_mapping(result)  # validates state shape
    assert captured_messages, "simple chat did not invoke the LLMClient"
    sent = captured_messages[0]

    # System prompt + 4 prior turn messages + current user turn
    assert sent[0]["role"] == "system"
    assert [(m["role"], m["content"]) for m in sent[1:-1]] == [
        ("user", "first prior user turn"),
        ("assistant", "first assistant reply"),
        ("user", "second prior user turn"),
        ("assistant", "second assistant reply"),
    ]
    assert sent[-1] == {"role": "user", "content": "current user turn"}
    assert all(m["content"] != "GARBAGE" for m in sent), (
        "simple_chat must ignore simple_chat_runtime['history']; "
        "the bundle is the single transcript-window authority"
    )


@pytest.mark.asyncio
async def test_simple_chat_raises_when_bundle_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM path fails loudly when the bundle is absent (singular-authority discipline)."""
    from agent.graph.nodes import simple_chat as simple_chat_module
    from agent.graph.nodes.simple_chat import run_simple_chat
    from agent.graph.state import InteractiveInput

    monkeypatch.setattr(
        simple_chat_module,
        "resolve_llm_client",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        simple_chat_module,
        "get_stream_writer",
        lambda: (_ for _ in ()).throw(RuntimeError("no writer in unit test")),
    )

    payload = InteractiveInput(
        task_id=43,
        message="hi",
        conversation_id="conv-no-bundle",
        metadata={"simple_chat_runtime": {"model": "stub"}},
    )
    state = payload.to_state().as_graph_state()

    result = await run_simple_chat(state, context=None, config={"configurable": {"thread_id": "t-no-bundle"}})

    # The node catches the RuntimeError and records it as final_error.
    assert "ConversationContextBundle is missing" in (result.get("trace", {}).get("final_error") or "")


__all__: List[str] = []
