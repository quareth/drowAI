"""Define deterministic section plans for engagement report generation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping

from backend.services.reporting.contracts import (
    GENERATION_METADATA_SECTION_PLAN_VERSION_KEY,
    REPORT_SECTION_TYPE_APPENDIX,
    REPORT_SECTION_TYPE_FINDINGS,
    REPORT_SECTION_TYPE_LIMITATIONS,
    REPORT_SECTION_TYPE_NARRATIVE,
    REPORT_SECTION_TYPE_RECOMMENDATIONS,
    REPORT_SECTION_TYPE_SUMMARY,
    REPORT_TYPE_PENTEST,
    REPORT_TYPE_VULNERABILITY_ASSESSMENT,
    ReportSectionType,
    ReportType,
    validate_report_type,
)

SECTION_PLAN_VERSION = "engagement_report_section_plan.v1"


@dataclass(frozen=True, slots=True)
class ReportSectionPlanItem:
    """Single fixed report section supplied to section-scoped generation."""

    section_id: str
    title: str
    order: int
    section_type: ReportSectionType
    prompt_purpose: str

    def as_llm_input(self) -> Mapping[str, str | int]:
        """Return only this section's plan data for one section-generation call."""

        return MappingProxyType(
            {
                "section_plan_version": SECTION_PLAN_VERSION,
                "section_id": self.section_id,
                "title": self.title,
                "order": self.order,
                "section_type": self.section_type,
                "prompt_purpose": self.prompt_purpose,
            }
        )


@dataclass(frozen=True, slots=True)
class ReportSectionPlan:
    """Versioned fixed section order for one validated report type."""

    report_type: ReportType
    version: str
    sections: tuple[ReportSectionPlanItem, ...]

    def generation_metadata(self) -> Mapping[str, str]:
        """Return safe metadata identifying the fixed section plan version."""

        return MappingProxyType(
            {GENERATION_METADATA_SECTION_PLAN_VERSION_KEY: self.version}
        )

    def iter_llm_inputs(self) -> Iterator[Mapping[str, str | int]]:
        """Yield one section-plan item per later LLM invocation."""

        for section in self.sections:
            yield section.as_llm_input()


_PENTEST_SECTION_PLAN = ReportSectionPlan(
    report_type=REPORT_TYPE_PENTEST,
    version=SECTION_PLAN_VERSION,
    sections=(
        ReportSectionPlanItem(
            section_id="executive_summary",
            title="Executive Summary",
            order=1,
            section_type=REPORT_SECTION_TYPE_SUMMARY,
            prompt_purpose=(
                "Summarize the engagement outcome, business impact, and key risk themes."
            ),
        ),
        ReportSectionPlanItem(
            section_id="scope_and_methodology",
            title="Scope and Methodology",
            order=2,
            section_type=REPORT_SECTION_TYPE_NARRATIVE,
            prompt_purpose=(
                "Describe assessed scope, testing approach, constraints, and source coverage."
            ),
        ),
        ReportSectionPlanItem(
            section_id="findings_summary",
            title="Findings Summary",
            order=3,
            section_type=REPORT_SECTION_TYPE_FINDINGS,
            prompt_purpose=(
                "Summarize reportable findings by severity, affected area, and evidence support."
            ),
        ),
        ReportSectionPlanItem(
            section_id="detailed_findings",
            title="Detailed Findings",
            order=4,
            section_type=REPORT_SECTION_TYPE_FINDINGS,
            prompt_purpose=(
                "Detail each supported finding with impact, affected targets, and supplied citations."
            ),
        ),
        ReportSectionPlanItem(
            section_id="recommendations",
            title="Recommendations",
            order=5,
            section_type=REPORT_SECTION_TYPE_RECOMMENDATIONS,
            prompt_purpose=(
                "Provide prioritized remediation guidance grounded in selected memo sources."
            ),
        ),
        ReportSectionPlanItem(
            section_id="limitations",
            title="Limitations",
            order=6,
            section_type=REPORT_SECTION_TYPE_LIMITATIONS,
            prompt_purpose=(
                "List assessment limitations and unsupported context from selected limited memos."
            ),
        ),
        ReportSectionPlanItem(
            section_id="appendix_evidence_index",
            title="Appendix / Evidence Index",
            order=7,
            section_type=REPORT_SECTION_TYPE_APPENDIX,
            prompt_purpose=(
                "Provide the renderer-owned appendix shell for the evidence timeline."
            ),
        ),
    ),
)

_VULNERABILITY_ASSESSMENT_SECTION_PLAN = ReportSectionPlan(
    report_type=REPORT_TYPE_VULNERABILITY_ASSESSMENT,
    version=SECTION_PLAN_VERSION,
    sections=(
        ReportSectionPlanItem(
            section_id="executive_summary",
            title="Executive Summary",
            order=1,
            section_type=REPORT_SECTION_TYPE_SUMMARY,
            prompt_purpose=(
                "Summarize assessment outcome, exposure themes, and highest priority risks."
            ),
        ),
        ReportSectionPlanItem(
            section_id="assessment_scope",
            title="Assessment Scope",
            order=2,
            section_type=REPORT_SECTION_TYPE_NARRATIVE,
            prompt_purpose=(
                "Describe assessed systems, coverage boundaries, and assessment constraints."
            ),
        ),
        ReportSectionPlanItem(
            section_id="asset_coverage",
            title="Asset Coverage",
            order=3,
            section_type=REPORT_SECTION_TYPE_NARRATIVE,
            prompt_purpose=(
                "Summarize covered assets, services, and notable coverage gaps."
            ),
        ),
        ReportSectionPlanItem(
            section_id="vulnerability_summary",
            title="Vulnerability Summary",
            order=4,
            section_type=REPORT_SECTION_TYPE_FINDINGS,
            prompt_purpose=(
                "Summarize validated vulnerabilities by severity, asset, and evidence support."
            ),
        ),
        ReportSectionPlanItem(
            section_id="detailed_vulnerabilities",
            title="Detailed Vulnerabilities",
            order=5,
            section_type=REPORT_SECTION_TYPE_FINDINGS,
            prompt_purpose=(
                "Detail each supported vulnerability with impact, affected assets, and citations."
            ),
        ),
        ReportSectionPlanItem(
            section_id="remediation_priority",
            title="Remediation Priority",
            order=6,
            section_type=REPORT_SECTION_TYPE_RECOMMENDATIONS,
            prompt_purpose=(
                "Prioritize remediation actions using risk, exposure, and implementation urgency."
            ),
        ),
        ReportSectionPlanItem(
            section_id="limitations",
            title="Limitations",
            order=7,
            section_type=REPORT_SECTION_TYPE_LIMITATIONS,
            prompt_purpose=(
                "List assessment limitations and unsupported context from selected limited memos."
            ),
        ),
        ReportSectionPlanItem(
            section_id="appendix_evidence_index",
            title="Appendix / Evidence Index",
            order=8,
            section_type=REPORT_SECTION_TYPE_APPENDIX,
            prompt_purpose=(
                "Provide the renderer-owned appendix shell for the evidence timeline."
            ),
        ),
    ),
)

_SECTION_PLANS: Mapping[ReportType, ReportSectionPlan] = MappingProxyType(
    {
        REPORT_TYPE_PENTEST: _PENTEST_SECTION_PLAN,
        REPORT_TYPE_VULNERABILITY_ASSESSMENT: _VULNERABILITY_ASSESSMENT_SECTION_PLAN,
    }
)


def get_report_section_plan(report_type: str) -> ReportSectionPlan:
    """Return the fixed section plan for a validated report type."""

    return _SECTION_PLANS[validate_report_type(report_type)]


def get_section_plan_generation_metadata(report_type: str) -> Mapping[str, str]:
    """Return generation metadata for the selected fixed section plan."""

    return get_report_section_plan(report_type).generation_metadata()


def iter_section_plan_llm_inputs(
    report_type: str,
) -> Iterator[Mapping[str, str | int]]:
    """Yield one section plan item at a time for section-scoped LLM calls."""

    yield from get_report_section_plan(report_type).iter_llm_inputs()


__all__ = [
    "SECTION_PLAN_VERSION",
    "ReportSectionPlan",
    "ReportSectionPlanItem",
    "get_report_section_plan",
    "get_section_plan_generation_metadata",
    "iter_section_plan_llm_inputs",
]
