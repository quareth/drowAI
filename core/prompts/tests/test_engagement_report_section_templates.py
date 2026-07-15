"""Tests for engagement report section prompt registry entries and constraints."""

from __future__ import annotations

from core.prompts.registry import PromptRegistry


def test_engagement_report_section_templates_load_by_stable_id() -> None:
    registry = PromptRegistry()

    assert registry.get_latest_version("engagement_report_section") == "v1"
    assert "engagement report section generator" in registry.get_template(
        "engagement_report_section_system"
    )
    assert "{report_context_json}" in registry.get_template(
        "engagement_report_section_user"
    )
    assert "{section_plan_json}" in registry.get_template(
        "engagement_report_section_user"
    )


def test_engagement_report_section_templates_enforce_report_context_limits() -> None:
    registry = PromptRegistry()
    system_template = registry.get_template("engagement_report_section_system")
    user_template = registry.get_template("engagement_report_section_user")
    combined_template = f"{system_template}\n{user_template}"

    assert "bounded report context" in combined_template
    assert "fixed section plan" in combined_template
    assert "Return structured JSON" in combined_template
    assert "Cite only source refs supplied in the report context" in combined_template
    assert "Source refs are machine-only citations" in combined_template
    assert "never in Markdown prose" in combined_template
    assert "Limited memos may contribute only limitations or unsupported context" in (
        combined_template
    )
    assert (
        "Candidate-only findings are excluded unless include_candidate_findings is true"
        in combined_template
    )
    assert "Do not fetch files, browse, call tools" in combined_template


def test_engagement_report_section_templates_use_product_terms_only() -> None:
    registry = PromptRegistry()
    combined_template = "\n".join(
        (
            registry.get_template("engagement_report_section_system"),
            registry.get_template("engagement_report_section_user"),
        )
    )

    forbidden_term = "wa" + "ve"
    assert forbidden_term not in combined_template.lower()
