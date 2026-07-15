"""Guardrail tests: planner_service output is brief-only.

Phase 3 Task 3.2 completed the cutover of
``agent/graph/subgraphs/tool_execution_runtime/planner_service.py``
to produce brief-driven planner context rather than transcript text.
These tests lock the post-cutover invariants on
``build_planner_context``:

- The returned dict never carries ``conversation_history_text``. The
  legacy field has been removed and a silent reintroduction must not
  pass unnoticed.
- The classifier-derived ``intent_brief`` (read from
  ``metadata['working_memory']['intent_brief']``) is forwarded to
  downstream callers (``enhanced_planner_impl._try_llm_action_plan``)
  via the planner-context dict.
- Missing briefs resolve to ``None`` rather than a transcript
  fallback, preserving the prompt builder's graceful ``(none)``
  placeholder rendering.

These invariants are the planner-service side of the brief-only
prompt-authority boundary locked on the builder side by
``core/prompts/tests/test_tool_planning_brief_contract.py`` and on
the planner-call side by
``agent/tests/test_enhanced_planner_impl_brief_only.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

# Force full graph package init to sidestep the known circular import
# between planner_service and the graph builders/nodes — importing
# builders first lets planner_service resolve cleanly.
import agent.graph.builders  # noqa: F401
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.subgraphs.tool_execution_runtime import planner_service
from agent.tool_runtime import ToolExecutionRequest
def _populated_brief() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 10.0.0.5",
        "overall_goal": "Enumerate services on 10.0.0.5",
        "continuation_mode": "new_request",
        "next_operational_goal": "Run nmap -sV on 10.0.0.5",
        "success_condition": "List of open TCP ports",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "explicit_constraints": [],
        "suggested_category_focus": ["information_gathering"],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        "target": {
            "resolved_target": "10.0.0.5",
            "target_status": "resolved",
            "target_source": "explicit_current_message",
            "prior_target_reuse": "allow",
        },
    }


def _install_bundle(
    metadata: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> None:
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
        conversation_id="conv-brief",
        turn_id="turn-brief",
        turn_sequence=0,
        messages=list(messages),
    )


def _make_request(message: str) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message=message,
        history=[],
    )


def _make_interactive(metadata: Dict[str, Any], message: str) -> MagicMock:
    interactive = MagicMock()
    interactive.facts.metadata = metadata
    interactive.facts.message = message
    interactive.facts.plan = []
    interactive.facts.current_goal = ""
    interactive.facts.next_tool_hint = None
    interactive.facts.selected_tool = None
    interactive.facts.tool_parameters = {}
    interactive.facts.todo_list = []
    interactive.facts.intent_hints = {"targets": []}
    interactive.trace.reasoning = []
    interactive.trace.observations = []
    return interactive


def _stub_catalog(*_args, **_kwargs):
    return ["shell.exec"]


def test_build_planner_context_includes_intent_brief_from_metadata() -> None:
    """Brief read from working memory is forwarded."""
    brief = _populated_brief()
    metadata: Dict[str, Any] = {
        "working_memory": {"intent_brief": brief},
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
    }
    _install_bundle(metadata, [{"role": "user", "content": "scan 10.0.0.5"}])

    interactive = _make_interactive(metadata, "scan 10.0.0.5")
    request = _make_request("scan 10.0.0.5")

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_stub_catalog,
        get_full_tool_catalog_for_planner=_stub_catalog,
        working_memory_summary_max_chars=900,
    )

    assert "intent_brief" in planner_context
    # The brief is forwarded verbatim from metadata, not reconstructed.
    assert planner_context["intent_brief"] is brief


def test_build_planner_context_does_not_emit_conversation_history_text() -> None:
    """The legacy ``conversation_history_text`` field is gone post-cutover.

    Phase 3 Task 3.2: ``planner_service`` no longer produces a folded
    transcript string. A regression that reintroduces the key would
    silently re-expand the direct-executor prompt surface back onto
    transcript — this test fails fast if that happens.
    """
    metadata: Dict[str, Any] = {
        "working_memory": {"intent_brief": _populated_brief()},
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
    }
    _install_bundle(metadata, [{"role": "user", "content": "scan 10.0.0.5"}])

    interactive = _make_interactive(metadata, "scan 10.0.0.5")
    request = _make_request("scan 10.0.0.5")

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_stub_catalog,
        get_full_tool_catalog_for_planner=_stub_catalog,
        working_memory_summary_max_chars=900,
    )

    assert "conversation_history_text" not in planner_context, (
        "planner_service.build_planner_context must not emit "
        "conversation_history_text after the Phase 3 Task 3.2 cutover."
    )


def test_build_planner_context_handles_missing_brief_as_none() -> None:
    """Missing briefs resolve to ``None`` — no transcript fallback."""
    metadata: Dict[str, Any] = {
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
    }
    _install_bundle(metadata, [{"role": "user", "content": "scan 10.0.0.5"}])

    interactive = _make_interactive(metadata, "scan 10.0.0.5")
    request = _make_request("scan 10.0.0.5")

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_stub_catalog,
        get_full_tool_catalog_for_planner=_stub_catalog,
        working_memory_summary_max_chars=900,
    )

    assert planner_context["intent_brief"] is None
    assert "conversation_history_text" not in planner_context


def test_build_planner_context_rejects_non_mapping_brief_by_using_none() -> None:
    """Non-mapping brief payloads are treated as missing, not as text fallback."""
    metadata: Dict[str, Any] = {
        # Upstream type drift (e.g. a string was written instead of a mapping):
        # the planner service must not treat this as a transcript input.
        "working_memory": {"intent_brief": "not-a-mapping"},
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
    }
    _install_bundle(metadata, [{"role": "user", "content": "scan 10.0.0.5"}])

    interactive = _make_interactive(metadata, "scan 10.0.0.5")
    request = _make_request("scan 10.0.0.5")

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_stub_catalog,
        get_full_tool_catalog_for_planner=_stub_catalog,
        working_memory_summary_max_chars=900,
    )

    assert planner_context["intent_brief"] is None
    assert "conversation_history_text" not in planner_context


# ---------------------------------------------------------------------------
# Fix 1 (runner_control follow-up): planner context carries no ``history`` list.
# ---------------------------------------------------------------------------


def test_build_planner_context_does_not_emit_history_key() -> None:
    """Fix 1: the returned dict never carries a ``history`` key.

    Before the Fix, ``planner_service`` projected the bundle transcript
    into a ``history`` list and passed it into artifact-policy
    resolution plus the target resolver. After the Fix the artifact
    policy reads the classifier-derived brief instead and the target
    resolver discards the list. Asserting the key is absent fails fast
    if a regression re-expands the planner context surface back onto
    transcript.
    """
    metadata: Dict[str, Any] = {
        "working_memory": {"intent_brief": _populated_brief()},
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
    }
    _install_bundle(metadata, [{"role": "user", "content": "scan 10.0.0.5"}])

    interactive = _make_interactive(metadata, "scan 10.0.0.5")
    request = _make_request("scan 10.0.0.5")

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_stub_catalog,
        get_full_tool_catalog_for_planner=_stub_catalog,
        working_memory_summary_max_chars=900,
    )

    assert "history" not in planner_context, (
        "planner_service.build_planner_context must not emit a "
        "``history`` list after the Fix 1 cutover."
    )


def test_resolve_planner_prompt_history_symbol_is_gone() -> None:
    """Fix 1: the transcript-projection helper has been removed entirely."""
    assert not hasattr(planner_service, "_resolve_planner_prompt_history"), (
        "planner_service._resolve_planner_prompt_history must not be "
        "re-introduced; the transcript-projection helper was removed "
        "by Fix 1."
    )
