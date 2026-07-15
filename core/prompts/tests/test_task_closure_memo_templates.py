"""Tests for task closure memo prompt registry entries and grounding text."""

from __future__ import annotations

from core.prompts.registry import PromptRegistry


def test_task_closure_memo_templates_load_by_stable_id() -> None:
    registry = PromptRegistry()

    assert registry.get_latest_version("task_closure_memo") == "v1"
    assert "task closure memo generator" in registry.get_template(
        "task_closure_memo_system"
    )
    assert "{memo_context_json}" in registry.get_template("task_closure_memo_user")


def test_task_closure_memo_templates_separate_transcript_from_reportable_claims() -> (
    None
):
    registry = PromptRegistry()
    system_template = registry.get_template("task_closure_memo_system")
    user_template = registry.get_template("task_closure_memo_user")
    combined_template = f"{system_template}\n{user_template}"

    assert (
        "Use transcript context only to understand actions performed"
        in combined_template
    )
    assert (
        "A claim supported only by transcript context is not reportable"
        in combined_template
    )
    assert "unsupported_notes or limitations" in combined_template
    assert (
        "Use evidence packet refs and knowledge packet refs to decide reportable observations"
        in (combined_template)
    )
    assert "Never invent evidence refs, knowledge refs" in combined_template
    forbidden_term = "wa" + "ve"
    assert forbidden_term not in combined_template.lower()
