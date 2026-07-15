"""Bundle-cutover tests for articulation and deep-reasoning finalizer prompts.

Phase 3 Task 3.4 finishes the articulation cutover: articulation no
longer reads the shared ``ConversationContextBundle`` on its hot
path. The only two full-history consumers after runner_control narrowing are the
intent classifier and the deep-reasoning finalizer. These tests lock
both halves of that boundary:

- the articulation node helper that resolved bundle transcript +
  runtime-state has been removed and the builder no longer accepts a
  transitional ``conversation_context`` kwarg;
- the finalizer still projects the bundle for its user prompt.

Phase 3 Task 3.6 is the positive counterpart to every
"narrowed node X does NOT import transcript symbols" test added in
Tasks 3.1, 3.3, 3.4, and 3.5. The finalizer is explicitly NOT
narrowed in runner_control, so the F-1/F-2/F-3 guardrails in this file lock
that exception: the finalizer module must retain its bundle-transcript
imports, its rendered prompt must carry the bundle's transcript slice,
and its runtime path must invoke ``project_for_articulation`` on the
populated bundle. Future cleanup that tries to silently narrow the
finalizer will fail these tests and force an explicit plan update.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
    update_prior_turn_references,
)
from agent.graph.context.contracts import RuntimeStateSnapshot


def _bundle_with_context() -> Dict[str, Any]:
    return build_conversation_context_bundle(
        conversation_id="conv-d",
        turn_id="turn-d",
        turn_sequence=2,
        messages=[
            {"role": "user", "content": "scan 5.5.5.5"},
            {"role": "assistant", "content": "Starting nmap on 5.5.5.5"},
            {"role": "user", "content": "also check web ports"},
        ],
        runtime_state=RuntimeStateSnapshot(
            active_target={"value": "5.5.5.5", "kind": "ip"},
            current_goal=None,
            current_decision={"action": "enumerate", "reason": "follow up"},
            in_flight_tool=None,
            handles={"target_id": "target:intent:target"},
        ),
    )


def _populated_brief() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 5.5.5.5",
        "overall_goal": "Map service surface on 5.5.5.5",
        "continuation_mode": "continuation",
        "next_operational_goal": "Run TCP port scan on 5.5.5.5",
        "success_condition": "Return list of open ports with banners",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "explicit_constraints": ["No UDP scan"],
        "suggested_category_focus": ["information_gathering"],
        "retrieval_hints": ["tcp scan"],
        "relevant_memory_fragments": ["prior finding: 5.5.5.5 responds to ICMP"],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        "target": {
            "resolved_target": "5.5.5.5",
            "target_status": "resolved",
            "target_source": "explicit_current_message",
            "prior_target_reuse": "allow",
        },
    }


# ---------------------------------------------------------------------------
# Phase 3 Task 3.4 guardrails on the articulation node module.
# ---------------------------------------------------------------------------


def test_articulation_module_has_no_bundle_transcript_resolver() -> None:
    """The transitional bundle-context resolver is gone after the cutover."""
    from agent.graph.nodes import tool_articulation as articulation_module

    # Resolver that projected bundle transcript + runtime-state no longer exists.
    assert getattr(articulation_module, "_resolve_articulation_bundle_context", None) is None


def test_articulation_module_no_longer_imports_transcript_symbols() -> None:
    """No bundle / transcript serialization symbols survive on the module."""
    from agent.graph.nodes import tool_articulation as articulation_module

    forbidden = (
        "METADATA_CONTEXT_BUNDLE_KEY",
        "SECTION_RECENT_TRANSCRIPT",
        "SECTION_RUNTIME_STATE",
        "project_for_articulation",
        "serialize_projection_to_section_map",
    )
    for name in forbidden:
        assert getattr(articulation_module, name, None) is None, (
            f"tool_articulation unexpectedly re-exports transcript-wiring symbol {name}"
        )


def test_articulation_builder_rejects_transitional_conversation_context() -> None:
    """The builder signature no longer accepts ``conversation_context``.

    Phase 2 Task 2.4 kept the kwarg as a transitional shim so the
    Phase 3 Task 3.4 cutover could land in a single patch without
    breaking the callsite mid-migration. After Phase 3 Task 3.4 the
    builder must fail fast if any caller tries to resurrect transcript
    input.
    """
    from core.prompts.constants import build_tool_articulation_prompt

    with pytest.raises(TypeError):
        build_tool_articulation_prompt(  # type: ignore[call-arg]
            selected_tool="nmap",
            tool_params="{}",
            intent_brief=_populated_brief(),
            conversation_context="<turn n=1 role=user>leak</turn>",
        )


def test_articulation_node_prompt_carries_brief_not_transcript(monkeypatch) -> None:
    """Even with a populated bundle, the rendered prompt stays brief-only.

    Running the node with a bundle that contains distinctive transcript
    markers AND a populated ``intent_brief`` must render a
    prompt that carries the brief fields and zero transcript markers.
    This locks the hot path: the articulation node cannot accidentally
    reintroduce bundle-transcript fanout without failing this test.
    """
    import agent.graph.nodes.tool_articulation as articulation_module
    from agent.graph.nodes.tool_articulation import articulate_tool_intent

    captured_prompts: list[str] = []

    class _CapturingLLM:
        model = "stub-articulation"

        async def chat_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            **kwargs: Any,
        ) -> Any:
            captured_prompts.append(user_prompt)
            return type(
                "_Response",
                (),
                {
                    "content": "To scan 5.5.5.5 open ports, I will run nmap.",
                    "usage": None,
                },
            )()

    monkeypatch.setattr(
        articulation_module,
        "resolve_llm_client",
        lambda *_a, **_kw: _CapturingLLM(),
    )
    monkeypatch.setattr(articulation_module, "get_stream_writer", lambda: None)

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-d",
            "message": "also check web ports",
            "selected_tool": "nmap",
            "tool_parameters": {"nmap": {"target": "5.5.5.5", "ports": "80,443"}},
            "metadata": {
                "api_key": "test-key",
                "model": "stub-articulation",
                METADATA_CONTEXT_BUNDLE_KEY: _bundle_with_context(),
                "working_memory": {"intent_brief": _populated_brief()},
            },
        },
        "trace": {"reasoning": []},
    }

    import asyncio

    asyncio.run(articulate_tool_intent(state))

    assert captured_prompts, "expected articulation to invoke the LLM path"
    prompt = captured_prompts[0]

    # Brief fields reach the prompt.
    assert "Scan open ports on 5.5.5.5" in prompt
    assert "Run TCP port scan on 5.5.5.5" in prompt
    assert "Return list of open ports with banners" in prompt
    assert "No UDP scan" in prompt

    # Transcript markers from the bundle do not reach the prompt.
    forbidden_markers = (
        "<turn",
        "</turn>",
        "role=user",
        "role=assistant",
        "latest=true",
        "Conversation (oldest -> newest",
        "Recent conversation",
        "assistant reply",
        "Starting nmap on 5.5.5.5",
        "also check web ports",
    )
    for marker in forbidden_markers:
        assert marker not in prompt, (
            f"transcript marker {marker!r} leaked into narrowed articulation prompt"
        )


# ---------------------------------------------------------------------------
# Finalizer half of the boundary (unchanged by Task 3.4).
# ---------------------------------------------------------------------------


def test_finalizer_helper_returns_bundle_sections() -> None:
    """The finalizer helper extracts the same projection sections as before."""
    from agent.graph.nodes.finalize import _resolve_finalizer_bundle_sections

    metadata: Dict[str, Any] = {METADATA_CONTEXT_BUNDLE_KEY: _bundle_with_context()}

    transcript, runtime_state, referenced_prior_turns = _resolve_finalizer_bundle_sections(metadata)

    assert "scan 5.5.5.5" in transcript
    assert "active_target" in runtime_state
    assert referenced_prior_turns == ""


def test_finalizer_prompt_includes_unified_conversation_section() -> None:
    """The finalizer user prompt exposes one bundle-driven conversation stream.

    The in-flight user turn rides inside the transcript tagged
    ``latest=true``; the finalizer does not emit a separate
    ``## User Request`` section (which would duplicate the latest
    block).
    """
    from agent.graph.nodes.finalize import _build_prompts
    from agent.graph.state import InteractiveState

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-d",
            "message": "also check web ports",
            "plan": ["scan 5.5.5.5"],
            # The full-history seam (bundle transcript + runtime state)
            # is the deep-reasoning capability of the unified finalizer.
            "capability": "deep_reasoning",
            "metadata": {METADATA_CONTEXT_BUNDLE_KEY: _bundle_with_context()},
        },
        "trace": {"history": [], "reasoning": [], "observations": []},
    }
    interactive = InteractiveState.from_mapping(state)

    from core.prompts.constants import CONVERSATION_SECTION_LABEL

    _, user_prompt = _build_prompts(interactive)

    assert f"## {CONVERSATION_SECTION_LABEL}" in user_prompt
    assert "scan 5.5.5.5" in user_prompt
    assert "## Runtime State" in user_prompt
    # Bundle's active_target surfaces in the runtime-state block.
    assert "active_target" in user_prompt
    # No duplicate ``## User Request`` section -- the latest=true tag
    # on the transcript's final turn is the single source of truth.
    assert "## User Request" not in user_prompt


def test_finalizer_prompt_includes_referenced_prior_turns_when_materialized() -> None:
    from agent.graph.nodes.finalize import _build_prompts
    from agent.graph.state import InteractiveState

    bundle = _bundle_with_context()
    update_prior_turn_references(
        bundle,
        {
            "operation": "reference_resolution",
            "status": "ok",
            "materialized_turns": [
                {
                    "turn_number": 2,
                    "speaker": "assistant",
                    "message_id": 77,
                    "text": "Earlier canonical assistant statement.",
                }
            ],
            "unresolved_hints": [{"anchor_text": "MODEL ONLY ANCHOR"}],
        },
    )
    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-d",
            "message": "explain that statement",
            "metadata": {METADATA_CONTEXT_BUNDLE_KEY: bundle},
        },
        "trace": {"history": [], "reasoning": [], "observations": []},
    }
    interactive = InteractiveState.from_mapping(state)

    _, user_prompt = _build_prompts(interactive)

    assert "## Referenced Prior Turns" in user_prompt
    assert user_prompt.count("Referenced Prior Turns") == 1
    assert "Earlier canonical assistant statement." in user_prompt
    assert "MODEL ONLY ANCHOR" not in user_prompt


# ---------------------------------------------------------------------------
# Phase 3 Task 3.6 guardrails — lock the finalizer as a full-history
# consumer. These are the positive counterparts to the negative
# "no transcript symbol" tests added for every narrowed node in Tasks
# 3.1 / 3.3 / 3.4 / 3.5. If a future refactor silently narrows the
# finalizer off the bundle, these fail fast and force a plan update.
# ---------------------------------------------------------------------------


class TestFinalizerFullHistoryExceptionF1:
    """Guardrail F-1: finalizer module DOES import bundle-transcript symbols.

    Every narrowed node in this cutover has a negative test asserting it
    does NOT import the bundle-transcript wiring symbols. The finalizer
    is the explicit exception; this test is the positive-lock that
    proves those symbols remain on the finalizer module's import
    surface. If any of them disappear, the finalizer has been silently
    narrowed — that is forbidden by Task 3.6's acceptance criteria.
    """

    # Names the narrowed-node tests forbid. The finalizer must keep them.
    _REQUIRED_MODULE_ATTRS = (
        "METADATA_CONTEXT_BUNDLE_KEY",
        "SECTION_RECENT_TRANSCRIPT",
        "SECTION_RUNTIME_STATE",
        "project_for_articulation",
        "serialize_projection_to_section_map",
    )

    @pytest.mark.parametrize("attr_name", _REQUIRED_MODULE_ATTRS)
    def test_finalizer_module_retains_transcript_symbol(
        self, attr_name: str
    ) -> None:
        from agent.graph.nodes import finalize as finalizer_module

        assert hasattr(finalizer_module, attr_name), (
            f"agent.graph.nodes.finalize must keep bundle-transcript symbol "
            f"{attr_name!r} — the finalizer is one of the two intentional "
            "full-history seams per runner_control phase of "
            "docs/plans/intent_interpretation_wiring.md. See Task 3.6 "
            "('finalizer-exception-is-documented-and-tested')."
        )


def test_finalizer_prompt_contains_bundle_transcript_content() -> None:
    """Guardrail F-2: the finalizer prompt exposes the bundle's transcript.

    Drive ``_build_prompts`` with a bundle containing distinctive
    markers and assert the rendered finalizer user prompt carries BOTH
    the verbatim markers AND the unified-transcript section header
    (``<turn ...>`` blocks surfaced through
    ``serialize_projection_to_section_map``). This is the positive
    counterpart to the narrowed-node tests that assert transcript
    markers do NOT leak into their prompts.
    """
    from agent.graph.nodes.finalize import _build_prompts
    from agent.graph.state import InteractiveState

    marker_user_turn_x = "FINALIZER_BUNDLE_USER_TURN_X"
    marker_assistant_reply_y = "FINALIZER_BUNDLE_ASSISTANT_REPLY_Y"

    bundle = build_conversation_context_bundle(
        conversation_id="conv-f2",
        turn_id="turn-f2",
        turn_sequence=3,
        messages=[
            {"role": "user", "content": marker_user_turn_x},
            {"role": "assistant", "content": marker_assistant_reply_y},
            {"role": "user", "content": "finalize please"},
        ],
        runtime_state=RuntimeStateSnapshot(
            active_target={"value": "9.9.9.9", "kind": "ip"},
            current_goal=None,
            current_decision=None,
            in_flight_tool=None,
            handles={},
        ),
    )

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-f2",
            "message": "finalize please",
            # The full-history bundle seam is the deep-reasoning capability
            # of the unified finalizer.
            "capability": "deep_reasoning",
            "metadata": {METADATA_CONTEXT_BUNDLE_KEY: bundle},
        },
        "trace": {"history": [], "reasoning": [], "observations": []},
    }
    interactive = InteractiveState.from_mapping(state)

    _, user_prompt = _build_prompts(interactive)

    # Bundle's distinctive markers reach the rendered prompt.
    assert marker_user_turn_x in user_prompt, (
        "Finalizer prompt must contain the bundle's user-turn marker; "
        "the finalizer is the full-history seam."
    )
    assert marker_assistant_reply_y in user_prompt, (
        "Finalizer prompt must contain the bundle's assistant-reply "
        "marker; the finalizer is the full-history seam."
    )

    # Transcript shape from the bundle serializer reaches the prompt:
    # the unified-conversation section label and at least one
    # ``<turn ...>`` block must render in the finalizer user prompt.
    from core.prompts.constants import CONVERSATION_SECTION_LABEL

    assert f"## {CONVERSATION_SECTION_LABEL}" in user_prompt
    assert "<turn" in user_prompt, (
        "Finalizer prompt must expose the bundle's <turn ...> transcript "
        "shape — that is how it consumes full history."
    )


def test_finalizer_runtime_path_invokes_projection_on_bundle(monkeypatch) -> None:
    """Guardrail F-3: the finalizer reads the bundle via the projection.

    Monkeypatch ``project_for_articulation`` on the finalizer module
    and drive the helper that assembles the user prompt. Asserts the
    projection was called with the populated bundle. This locks the
    runtime wiring — not just the static import — so a regression that
    routes finalizer context around the projection fails fast.
    """
    from agent.graph.nodes import finalize as finalizer_module
    from agent.graph.nodes.finalize import _build_prompts
    from agent.graph.state import InteractiveState

    bundle = _bundle_with_context()
    captured_bundles: list[Dict[str, Any]] = []

    original_projection = finalizer_module.project_for_articulation

    def _spy_projection(arg: Any) -> Any:
        captured_bundles.append(dict(arg))
        return original_projection(arg)

    monkeypatch.setattr(
        finalizer_module, "project_for_articulation", _spy_projection
    )

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-f3",
            "message": "finalize please",
            # The full-history bundle seam is the deep-reasoning capability
            # of the unified finalizer.
            "capability": "deep_reasoning",
            "metadata": {METADATA_CONTEXT_BUNDLE_KEY: bundle},
        },
        "trace": {"history": [], "reasoning": [], "observations": []},
    }
    interactive = InteractiveState.from_mapping(state)

    _build_prompts(interactive)

    assert captured_bundles, (
        "Finalizer must invoke project_for_articulation on the bundle; "
        "runtime read was not observed. The finalizer is the "
        "full-history seam per Task 3.6."
    )
    # The projection received the exact bundle carried in metadata.
    assert captured_bundles[0].get("conversation_id") == bundle["conversation_id"]
    assert captured_bundles[0].get("turn_id") == bundle["turn_id"]


def test_only_finalizer_among_nodes_uses_project_for_articulation() -> None:
    """Scoped symmetric lock: among wired graph nodes, only the finalizer
    imports ``project_for_articulation``.

    This is the counterpart claim to the per-node negative guardrails
    (PTR, articulation, category selector, DR planner) already in place
    for runner_control. Walking every file under ``agent/graph/nodes/`` and
    asserting the finalizer module is the ONLY match for
    ``project_for_articulation`` is cheap, pinpointed, and fails fast
    if a future edit reintroduces the symbol on another node.

    The scan is scoped to ``agent/graph/nodes/**/*.py`` (non-test) on
    purpose — a repo-wide sweep would be flaky, and the per-node
    negative tests already lock each narrowed node individually.
    """
    import pathlib

    nodes_root = (
        pathlib.Path(__file__).resolve().parent.parent
    )  # agent/graph/nodes/
    assert nodes_root.name == "nodes", nodes_root

    offenders: list[str] = []
    for path in nodes_root.rglob("*.py"):
        # Skip test files and __pycache__; scope is production node code.
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        if "project_for_articulation" in source:
            offenders.append(str(path.relative_to(nodes_root)))

    # Unified-finalizer migration (Phase 6): ``finalize.py`` is the
    # single runtime home of the deep-reasoning full-history seam. The
    # legacy ``deep_reasoning_finalizer.py`` shim has been removed.
    assert offenders == ["finalize.py"], (
        "Only the unified finalizer (`finalize.py`) may import or "
        "reference project_for_articulation among wired graph nodes "
        "(Task 3.6 'two-full-history-seams-only'). Unexpected occurrences: "
        f"{offenders}"
    )
