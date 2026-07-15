"""Negative transcript-fidelity regression for the planner prompt surface.

Phase 2 Task 2.2 narrowed the direct-executor tool-planning prompts
(``select_tools`` and ``tool_parameters``) away from recent-transcript
ingestion and onto the classifier-derived ``intent_brief``.

Phase 3 Task 3.2 completes the cutover by removing the transitional
``conversation_history_text`` kwarg from every public builder method.
The former fidelity guardrails — which accepted the kwarg and checked
that transcript content did not reach the prompt body — are now
replaced with a stricter contract: passing the removed kwarg must
raise ``TypeError``. This locks the post-cutover prompt-authority
boundary so future work cannot silently reintroduce a second
transcript channel into the tool-planning seam.

The classifier and the deep-reasoning finalizer remain the two
explicit full-history seams; this guardrail is scoped to the
tool-planning builder alone.
"""

from __future__ import annotations

import pytest

from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder


def test_system_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """``build_system_prompt`` must not accept the removed kwarg.

    After the Phase 3 Task 3.2 cutover the transitional
    ``conversation_history_text`` kwarg is gone. Callers that still
    pass it receive a ``TypeError`` rather than a silent no-op, which
    fails loudly if transcript fanout is reintroduced at this seam.
    """
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_system_prompt(  # type: ignore[call-arg]
            conversation_history_text="forbidden transcript",
        )


def test_select_tools_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """``build_select_tools_prompt`` must not accept the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_select_tools_prompt(  # type: ignore[call-arg]
            resolved_tools=["nmap"],
            catalog=[{"id": "nmap", "name": "nmap", "description": "scanner"}],
            target="5.5.5.5",
            phase="enumeration",
            constraints={},
            conversation_history_text="forbidden transcript",
        )


def test_tool_parameters_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """``build_tool_parameters_prompt`` must not accept the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_tool_parameters_prompt(  # type: ignore[call-arg]
            selected_tools=["nmap"],
            target="5.5.5.5",
            phase="enumeration",
            constraints={},
            conversation_history_text="forbidden transcript",
        )


def test_resolve_tools_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """``build_resolve_tools_prompt`` must not accept the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_resolve_tools_prompt(  # type: ignore[call-arg]
            target="5.5.5.5",
            phase="enumeration",
            constraints={},
            previous_open_ports=[],
            conversation_history_text="forbidden transcript",
        )


def test_select_tools_system_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """``build_select_tools_system_prompt`` must not accept the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_select_tools_system_prompt(  # type: ignore[call-arg]
            conversation_history_text="forbidden transcript",
        )


def test_tool_parameters_system_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """``build_tool_parameters_system_prompt`` must not accept the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_tool_parameters_system_prompt(  # type: ignore[call-arg]
            conversation_history_text="forbidden transcript",
        )
