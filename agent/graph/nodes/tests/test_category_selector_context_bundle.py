"""Phase 3 Task 3.1 cutover — category selector is a brief consumer.

Earlier phases required the shared ``ConversationContextBundle``'s
projected transcript to appear verbatim inside the category-selector
prompt. Phase 2 Task 2.1 narrowed the builder contract to the
classifier-derived ``intent_brief`` while keeping a
transitional ``history_text`` resolver on the node. Phase 3 Task 3.1
(see ``docs/plans/intent_interpretation_wiring.md``) finishes the
cutover: the transcript resolver is deleted, the node no longer
touches the bundle, and ``metadata["working_memory"]["intent_brief"]`` is the
sole source of turn-interpretation context.

This module locks the post-cutover invariants:

- Guardrail: ``_resolve_category_selector_history_text`` is no longer
  importable from the node module.
- Guardrail: the node module does not import
  ``SECTION_RECENT_TRANSCRIPT`` or any bundle transcript projection.
- Happy path: when the brief is populated, the rendered prompt carries
  brief fields and NO transcript markers — even if a bundle with
  transcript content is present in metadata.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.contracts import RuntimeStateSnapshot
from agent.graph.nodes import select_tool_categories as selector_module
from agent.graph.nodes.select_tool_categories import select_tool_categories_node


def _make_history(turn_count: int) -> List[Dict[str, Any]]:
    """Return ``turn_count`` user/assistant turns with distinctive content."""
    history: List[Dict[str, Any]] = []
    for i in range(turn_count):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
    return history


def _install_bundle(
    metadata: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    runtime_state: RuntimeStateSnapshot | None = None,
) -> Dict[str, Any]:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=list(messages),
        runtime_state=runtime_state,
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    return bundle


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
            "history": [{"role": "user", "content": "TRACE_ONLY_SHOULD_NOT_APPEAR"}],
            "reasoning": [],
        },
    }


async def _run_node_capturing_prompt(
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


# ---------------------------------------------------------------------------
# Guardrail: deleted helpers and imports must not silently come back.
# ---------------------------------------------------------------------------


def test_transcript_resolver_is_removed_from_module() -> None:
    """Task 3.1 deleted ``_resolve_category_selector_history_text``.

    A future commit that reintroduces a bundle-transcript resolver on
    this node will re-expose this symbol and make the guardrail fail.
    """
    assert (
        getattr(selector_module, "_resolve_category_selector_history_text", None)
        is None
    ), (
        "select_tool_categories must not re-expose a bundle-transcript "
        "resolver after the Phase 3 Task 3.1 cutover"
    )


def test_transcript_projection_symbols_are_not_imported_by_module() -> None:
    """The node must not import bundle transcript projection surfaces.

    Re-importing ``SECTION_RECENT_TRANSCRIPT`` or the category-selector
    projection here would indicate the node is reading the transcript
    window again — a regression that this guardrail catches.
    """
    assert getattr(selector_module, "SECTION_RECENT_TRANSCRIPT", None) is None
    assert getattr(selector_module, "project_for_category_selector", None) is None
    assert (
        getattr(selector_module, "serialize_projection_to_section_map", None)
        is None
    )


# ---------------------------------------------------------------------------
# Happy path: brief drives the prompt; no transcript markers ever appear.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_selector_prompt_is_brief_driven(caplog) -> None:
    """Primary path: the brief drives the prompt, not bundle transcript."""
    history = _make_history(turn_count=3)
    metadata: Dict[str, Any] = {
        "api_key": "test-key",
        "working_memory": {
            "intent_brief": {
                "resolved_user_intent": "Enumerate services on 10.0.0.5",
                "overall_goal": "Map exposed services on 10.0.0.5",
                "continuation_mode": "new_request",
                "next_operational_goal": "Run TCP service detection",
                "success_condition": "Return open-port / service banner list",
                "execution_readiness": "ready",
                "blocking_reason": None,
                "resolved_target": "10.0.0.5",
                "target_status": "resolved",
                "target_source": "explicit_current_message",
                "explicit_constraints": [],
                "suggested_category_focus": ["information_gathering"],
                "retrieval_hints": ["service detection"],
                "relevant_memory_fragments": [],
                "request_contract": {
                    "question_type": "multi_step",
                    "answer_style": "normal",
                    "terminal_when": "all_steps_done",
                },
            }
        },
    }
    # A bundle is deliberately installed to prove the node ignores it.
    _install_bundle(
        metadata,
        history,
        runtime_state=RuntimeStateSnapshot(
            active_target={"value": "10.0.0.5"},
            current_goal=None,
            current_decision={"action": "enumerate", "reason": "follow up"},
            in_flight_tool=None,
            handles={},
        ),
    )
    state = _state_with_metadata(metadata)

    with caplog.at_level(
        "WARNING",
        logger="agent.graph.nodes.select_tool_categories",
    ):
        prompt = await _run_node_capturing_prompt(state)

    # Brief content appears.
    assert "Turn Execution Brief" in prompt
    assert "Enumerate services on 10.0.0.5" in prompt
    assert "Run TCP service detection" in prompt
    assert "information_gathering" in prompt
    assert "10.0.0.5" in prompt

    # No transcript marker leaks into the narrowed prompt.
    for marker in (
        "<turn",
        "</turn>",
        "assistant reply",
        "role=user",
        "role=assistant",
        "latest=true",
    ):
        assert marker not in prompt, (
            f"transcript marker {marker!r} leaked into the narrowed "
            "category-selector prompt"
        )

    # Trace-only content must never leak.
    assert "TRACE_ONLY_SHOULD_NOT_APPEAR" not in prompt


@pytest.mark.asyncio
async def test_category_selector_ignores_bundle_transcript_content() -> None:
    """Bundle transcript must NOT drive the prompt after the Task 3.1 cutover.

    Even when a bundle is present with distinctive recent-transcript
    content, that text must be absent from the rendered prompt. The
    node no longer reads the bundle on this hot path.
    """
    history = [
        {"role": "user", "content": "CURRENT_USER_TURN_FROM_BUNDLE"},
        {"role": "assistant", "content": "CURRENT_ASSISTANT_TURN_FROM_BUNDLE"},
    ]
    metadata: Dict[str, Any] = {
        "api_key": "test-key",
        "working_memory": {"intent_brief": {}},
    }
    _install_bundle(metadata, history)
    state = _state_with_metadata(metadata)

    prompt = await _run_node_capturing_prompt(state)

    assert "CURRENT_USER_TURN_FROM_BUNDLE" not in prompt
    assert "CURRENT_ASSISTANT_TURN_FROM_BUNDLE" not in prompt
    # Empty brief still renders a structured block.
    assert "Turn Execution Brief" in prompt
    assert "(none)" in prompt
