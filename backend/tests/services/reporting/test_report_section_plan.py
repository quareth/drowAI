"""Tests for deterministic engagement report section plans."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from backend.services.reporting.contracts import (
    GENERATION_METADATA_SECTION_PLAN_VERSION_KEY,
)
from backend.services.reporting.report_section_plan import (
    SECTION_PLAN_VERSION,
    get_report_section_plan,
    get_section_plan_generation_metadata,
    iter_section_plan_llm_inputs,
)


def test_pentest_section_plan_matches_fixed_order_and_purpose() -> None:
    plan = get_report_section_plan("pentest")

    assert plan.report_type == "pentest"
    assert plan.version == SECTION_PLAN_VERSION
    assert [
        (
            section.section_id,
            section.title,
            section.order,
            section.section_type,
            bool(section.prompt_purpose),
        )
        for section in plan.sections
    ] == [
        ("executive_summary", "Executive Summary", 1, "summary", True),
        ("scope_and_methodology", "Scope and Methodology", 2, "narrative", True),
        ("findings_summary", "Findings Summary", 3, "findings", True),
        ("detailed_findings", "Detailed Findings", 4, "findings", True),
        ("recommendations", "Recommendations", 5, "recommendations", True),
        ("limitations", "Limitations", 6, "limitations", True),
        ("appendix_evidence_index", "Appendix / Evidence Index", 7, "appendix", True),
    ]


def test_vulnerability_assessment_plan_matches_fixed_order_and_purpose() -> None:
    plan = get_report_section_plan("vulnerability_assessment")

    assert plan.report_type == "vulnerability_assessment"
    assert plan.version == SECTION_PLAN_VERSION
    assert [
        (
            section.section_id,
            section.title,
            section.order,
            section.section_type,
            bool(section.prompt_purpose),
        )
        for section in plan.sections
    ] == [
        ("executive_summary", "Executive Summary", 1, "summary", True),
        ("assessment_scope", "Assessment Scope", 2, "narrative", True),
        ("asset_coverage", "Asset Coverage", 3, "narrative", True),
        ("vulnerability_summary", "Vulnerability Summary", 4, "findings", True),
        (
            "detailed_vulnerabilities",
            "Detailed Vulnerabilities",
            5,
            "findings",
            True,
        ),
        (
            "remediation_priority",
            "Remediation Priority",
            6,
            "recommendations",
            True,
        ),
        ("limitations", "Limitations", 7, "limitations", True),
        ("appendix_evidence_index", "Appendix / Evidence Index", 8, "appendix", True),
    ]


def test_unknown_report_type_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported reporting report_type"):
        get_report_section_plan("executive_overview")


def test_section_plan_version_is_available_for_generation_metadata() -> None:
    metadata = get_section_plan_generation_metadata("pentest")

    assert metadata == MappingProxyType(
        {GENERATION_METADATA_SECTION_PLAN_VERSION_KEY: SECTION_PLAN_VERSION}
    )


def test_llm_inputs_are_single_section_plan_items_without_report_structure() -> None:
    llm_inputs = list(iter_section_plan_llm_inputs("pentest"))

    assert len(llm_inputs) == 7
    assert llm_inputs[0] == MappingProxyType(
        {
            "section_plan_version": SECTION_PLAN_VERSION,
            "section_id": "executive_summary",
            "title": "Executive Summary",
            "order": 1,
            "section_type": "summary",
            "prompt_purpose": (
                "Summarize the engagement outcome, business impact, and key risk themes."
            ),
        }
    )
    for section_input in llm_inputs:
        assert set(section_input) == {
            "section_plan_version",
            "section_id",
            "title",
            "order",
            "section_type",
            "prompt_purpose",
        }
        assert "sections" not in section_input
        assert "report_structure" not in section_input
