"""Regression tests guarding the DR ``decision_router`` prompt restoration.

Phase 2 Task 2.1 of the *wire-think-more-reflect-synthesis-into-simple-tool*
plan re-enables the ``think_more`` action in
:func:`DeepReasoningPromptBuilder.build_decision_prompt`. Two changes were
required:

* Drop the disable hack
  (``"think_more is temporarily disabled and MUST NOT be selected"``) and
* Restore ``think_more`` in the action enum so the LLM sees the full
  vocabulary (``call_tool`` | ``think_more`` | ``reflect`` | ``finalize``).

These tests lock both edits at the prompt-builder seam so a future refactor
cannot silently re-disable ``think_more`` again.
"""

from __future__ import annotations

from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder


def _render_decision_prompt() -> str:
    """Render ``build_decision_prompt`` against a minimal state mapping.

    The minimal state is sufficient because the action-enum block is
    static text emitted regardless of facts/trace contents.
    """
    builder = DeepReasoningPromptBuilder()
    return builder.build_decision_prompt({"facts": {}, "trace": {}})


def test_decision_prompt_lists_think_more_in_action_enum() -> None:
    """``think_more`` must appear in the JSON action enum offered to the LLM."""
    prompt = _render_decision_prompt()

    expected_enum_line = (
        '"action": "call_tool" | "think_more" | "reflect" | "finalize"'
    )
    assert expected_enum_line in prompt, (
        "DR decision prompt must list think_more in the action enum; "
        "found prompt without the expected enum line."
    )


def test_decision_prompt_does_not_contain_disable_hack_string() -> None:
    """The disable-hack sentence must be gone from the DR decision prompt."""
    prompt = _render_decision_prompt()

    forbidden_fragments = (
        "temporarily disabled",
        "MUST NOT be selected",
    )
    for fragment in forbidden_fragments:
        assert fragment not in prompt, (
            f"Disable-hack fragment {fragment!r} must not appear in the "
            "DR decision prompt after Phase 2 Task 2.1."
        )


def test_decision_prompt_action_count_is_four() -> None:
    """The Available Actions block must enumerate exactly four labels."""
    prompt = _render_decision_prompt()

    # Each Available Actions entry uses the bolded header `**<label>**`.
    expected_headers = (
        "**call_tool**",
        "**think_more**",
        "**reflect**",
        "**finalize**",
    )
    for header in expected_headers:
        assert header in prompt, (
            f"Action header {header!r} missing from DR decision prompt."
        )

    action_header_count = sum(prompt.count(header) for header in expected_headers)
    assert action_header_count == 4, (
        "Expected exactly four bolded action headers in the DR decision "
        f"prompt, found {action_header_count}."
    )
