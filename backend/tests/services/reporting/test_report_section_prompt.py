"""Tests for engagement report section prompt rendering."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from backend.services.reporting.contracts import REPORT_SECTION_SCHEMA_VERSION
from backend.services.reporting.report_context_builder import (
    ReportCandidateFindingPolicy,
    ReportContext,
    ReportEngagementMetadata,
    ReportEvidenceRef,
    ReportKnowledgeRef,
    ReportMemoPartitions,
    ReportMemoWatermark,
    ReportSelectedMemoBody,
    ReportSelectedMemoContext,
    ReportSelectedTaskMetadata,
    ReportSourceWatermark,
)
from backend.services.reporting.report_section_plan import get_report_section_plan
from backend.services.reporting.report_section_prompt import (
    ReportSectionPromptRenderer,
    render_report_section_context_json,
)


class FakePromptRegistry:
    """Minimal registry double that exposes the prompt registry contract."""

    def __init__(self) -> None:
        self.latest_version_requests: list[str] = []
        self.template_requests: list[tuple[str, str | None]] = []

    def get_latest_version(self, family: str) -> str:
        self.latest_version_requests.append(family)
        return "v-test"

    def get_template(self, template_id: str, version: str | None = None) -> str:
        self.template_requests.append((template_id, version))
        if template_id == "engagement_report_section_system":
            return "section system prompt"
        if template_id == "engagement_report_section_user":
            return (
                "context={report_context_json}\n"
                "section={section_plan_json}"
            )
        raise KeyError(template_id)


def test_renderer_uses_registry_and_includes_required_metadata() -> None:
    registry = FakePromptRegistry()
    context = _report_context(include_candidate_findings=True)
    section = get_report_section_plan("pentest").sections[0]

    rendered = ReportSectionPromptRenderer(prompt_registry=registry).render(
        context=context,
        section_plan_item=section,
        report_type="pentest",
        candidate_policy=context.candidate_policy,
        section_schema_name="engagement_report_section",
        section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
    )

    assert registry.latest_version_requests == ["engagement_report_section"]
    assert registry.template_requests == [
        ("engagement_report_section_system", "v-test"),
        ("engagement_report_section_user", "v-test"),
    ]
    assert rendered.system_prompt == "section system prompt"
    assert rendered.metadata["prompt_family"] == "engagement_report_section"
    assert rendered.metadata["prompt_version"] == "v-test"
    assert rendered.metadata["prompt_template_ids"] == [
        "engagement_report_section_system",
        "engagement_report_section_user",
    ]
    assert rendered.metadata["section_id"] == section.section_id
    assert rendered.metadata["report_type"] == "pentest"
    assert rendered.metadata["section_schema_name"] == "engagement_report_section"
    assert rendered.metadata["section_schema_version"] == REPORT_SECTION_SCHEMA_VERSION


def test_rendered_context_is_deterministic_bounded_and_section_scoped() -> None:
    context = _report_context(include_candidate_findings=False)
    section = get_report_section_plan("pentest").sections[3]

    first = ReportSectionPromptRenderer(prompt_registry=FakePromptRegistry()).render(
        context=context,
        section_plan_item=section,
        report_type="pentest",
        candidate_policy=context.candidate_policy,
        section_schema_name="engagement_report_section",
        section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
    )
    second = ReportSectionPromptRenderer(prompt_registry=FakePromptRegistry()).render(
        context=context,
        section_plan_item=section,
        report_type="pentest",
        candidate_policy=context.candidate_policy,
        section_schema_name="engagement_report_section",
        section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
    )

    assert first.report_context_json == second.report_context_json
    assert first.section_plan_json == second.section_plan_json
    context_payload = json.loads(first.report_context_json)
    section_payload = json.loads(first.section_plan_json)
    assert context_payload["include_candidate_findings"] is False
    assert context_payload["section_schema"] == {
        "name": "engagement_report_section",
        "version": REPORT_SECTION_SCHEMA_VERSION,
    }
    assert [item["memo_id"] for item in context_payload["selected_memos"]] == [
        "memo-supported",
        "memo-limited",
    ]
    limited_body = context_payload["selected_memos"][1]["body"]
    assert limited_body["actions_performed"] == []
    assert limited_body["limitations"] == [{"summary": "No credentialed access"}]
    assert limited_body["unsupported_notes"] == [
        {"summary": "Scanner output incomplete"}
    ]
    assert section_payload["section_id"] == "detailed_findings"
    assert section_payload["title"] == "Detailed Findings"


def test_prompt_context_omits_raw_evidence_excerpt() -> None:
    raw_tool_output = "RAW_TOOL_OUTPUT:" + ("x" * 3_000)
    context = _report_context(evidence_excerpt=raw_tool_output)

    context_json = render_report_section_context_json(
        context=context,
        candidate_policy=context.candidate_policy,
        section_schema_name="engagement_report_section",
        section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
    )

    assert raw_tool_output not in context_json
    assert "RAW_TOOL_OUTPUT" not in context_json
    payload = json.loads(context_json)
    evidence_ref = payload["compatible_evidence_refs"][0]
    assert "excerpt" not in evidence_ref
    assert "summary" not in evidence_ref
    assert evidence_ref["tool_display_name"] == "HTTP probe"
    assert evidence_ref["ref"] == "evidence:web:1"


def test_renderer_rejects_mismatched_report_type_or_candidate_policy() -> None:
    context = _report_context(include_candidate_findings=False)
    section = get_report_section_plan("pentest").sections[0]
    renderer = ReportSectionPromptRenderer(prompt_registry=FakePromptRegistry())

    with pytest.raises(ValueError, match="report type"):
        renderer.render(
            context=context,
            section_plan_item=section,
            report_type="vulnerability_assessment",
            candidate_policy=context.candidate_policy,
            section_schema_name="engagement_report_section",
            section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
        )

    with pytest.raises(ValueError, match="candidate policy"):
        renderer.render(
            context=context,
            section_plan_item=section,
            report_type="pentest",
            candidate_policy=ReportCandidateFindingPolicy(
                include_candidate_findings=True
            ),
            section_schema_name="engagement_report_section",
            section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
        )


def test_prompt_renderer_has_no_llm_client_imports_or_file_reads() -> None:
    path = Path("backend/services/reporting/report_section_prompt.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    disallowed_calls: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                disallowed_calls.add("open")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "read_text",
                "read_bytes",
            }:
                disallowed_calls.add(node.func.attr)

    assert "core.prompts.registry" in imported_modules
    assert not {
        module
        for module in imported_modules
        if module.startswith(("openai", "anthropic", "langchain", "langgraph"))
    }
    assert disallowed_calls == set()


def _report_context(
    *,
    include_candidate_findings: bool = False,
    evidence_excerpt: str = "bounded service banner excerpt",
) -> ReportContext:
    candidate_policy = ReportCandidateFindingPolicy(
        include_candidate_findings=include_candidate_findings
    )
    supported_body = ReportSelectedMemoBody(
        actions_performed=(MappingProxyType({"summary": "Checked exposed HTTP"}),),
        reportable_observations=(
            MappingProxyType({"summary": "Weak admin interface exposed"}),
        ),
        possible_findings=(MappingProxyType({"summary": "Missing access control"}),),
        limitations=(),
        unsupported_notes=(),
        evidence_refs=("evidence:web:1",),
        knowledge_refs=("finding:1",),
    )
    limited_body = ReportSelectedMemoBody(
        actions_performed=(),
        reportable_observations=(),
        possible_findings=(),
        limitations=(MappingProxyType({"summary": "No credentialed access"}),),
        unsupported_notes=(MappingProxyType({"summary": "Scanner output incomplete"}),),
        evidence_refs=(),
        knowledge_refs=(),
    )
    source_watermark = ReportSourceWatermark(
        schema_version=1,
        report_type="pentest",
        candidate_policy=candidate_policy,
        selected_memos=(
            ReportMemoWatermark(
                memo_id="memo-supported",
                task_id=10,
                version=2,
                source_watermark=MappingProxyType({"hash": "a"}),
            ),
            ReportMemoWatermark(
                memo_id="memo-limited",
                task_id=11,
                version=1,
                source_watermark=MappingProxyType({"hash": "b"}),
            ),
        ),
        hash_algorithm="sha256",
        hash="report-input-hash",
        job_source_watermark=MappingProxyType({"hash": "report-input-hash"}),
        generation_metadata=MappingProxyType(
            {"source_watermark_hash": "report-input-hash"}
        ),
    )
    return ReportContext(
        engagement=ReportEngagementMetadata(
            engagement_id=1,
            tenant_id=2,
            user_id=3,
            name="Acme External Assessment",
            description="Internet-facing systems",
            status="active",
            created_at="2026-06-09T12:00:00+00:00",
        ),
        report_type="pentest",
        selected_memos=(
            ReportSelectedMemoContext(
                memo_id="memo-limited",
                task_id=11,
                version=1,
                memo_mode="limited",
                generated_at="2026-06-09T12:10:00+00:00",
                summary="limited memo",
                body=limited_body,
                source_watermark=MappingProxyType({"hash": "b"}),
            ),
            ReportSelectedMemoContext(
                memo_id="memo-supported",
                task_id=10,
                version=2,
                memo_mode="supported",
                generated_at="2026-06-09T12:05:00+00:00",
                summary="supported memo",
                body=supported_body,
                source_watermark=MappingProxyType({"hash": "a"}),
            ),
        ),
        memo_partitions=ReportMemoPartitions(
            supported_memo_ids=("memo-supported",),
            limited_memo_ids=("memo-limited",),
        ),
        selected_tasks=(
            ReportSelectedTaskMetadata(
                task_id=11,
                memo_id="memo-limited",
                name="Credentialed review",
                description="Credentialed checks",
                scope="internal",
                status="stopped",
                created_at="2026-06-09T11:00:00+00:00",
                stopped_at="2026-06-09T12:00:00+00:00",
            ),
            ReportSelectedTaskMetadata(
                task_id=10,
                memo_id="memo-supported",
                name="External web test",
                description="External checks",
                scope="external",
                status="stopped",
                created_at="2026-06-09T10:00:00+00:00",
                stopped_at="2026-06-09T12:00:00+00:00",
            ),
        ),
        compatible_knowledge_refs=(
            ReportKnowledgeRef(
                ref="finding:1",
                task_id=10,
                record_type="finding",
                summary="Missing access control",
                authoritative=True,
                source_execution_ids=("exec-1",),
                evidence_archive_refs=("evidence_archive:1",),
            ),
        ),
        compatible_evidence_refs=(
            ReportEvidenceRef(
                ref="evidence:web:1",
                task_id=10,
                evidence_type="http_response",
                summary="Admin endpoint returned without authentication",
                excerpt=evidence_excerpt,
                source_tool="http_probe",
                target="https://example.test/admin",
                observed_at="2026-06-09T12:20:00+00:00",
                created_at="2026-06-09T12:21:00+00:00",
                linked_knowledge_refs=("finding:1",),
            ),
        ),
        candidate_policy=candidate_policy,
        source_watermark=source_watermark,
        allowed_task_memo_ids=frozenset({"memo-supported", "memo-limited"}),
        allowed_knowledge_refs=frozenset({"finding:1"}),
        allowed_evidence_refs=frozenset({"evidence:web:1"}),
        truncated=False,
    )
