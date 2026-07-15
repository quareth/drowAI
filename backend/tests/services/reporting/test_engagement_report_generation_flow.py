"""End-to-end regression tests for engagement report generation flow."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
)
from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.models.tenant import Tenant
from backend.services.llm_provider.types import LLMCredentialRef, LLMRuntimeSelection
from backend.services.reporting.contracts import (
    GENERATION_METADATA_RENDERER_VERSION_KEY,
    GENERATION_METADATA_SECTION_PLAN_VERSION_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
    MEMO_MODE_LIMITED,
    MEMO_MODE_SUPPORTED,
    MEMO_STATUS_READY,
    REPORT_JOB_STATUS_QUEUED,
    REPORT_JOB_STATUS_READY,
    REPORT_SECTION_SCHEMA_VERSION,
    REPORT_SECTION_STATUS_READY,
    REPORT_SECTION_TYPE_APPENDIX,
    REPORT_SECTION_TYPE_FINDINGS,
    REPORT_SECTION_TYPE_LIMITATIONS,
    REPORT_STATUS_READY,
    REPORT_TYPE_PENTEST,
)
from backend.services.reporting.report_generation_service import (
    ReportGenerationService,
)
from backend.services.reporting.report_read_service import ReportReadService
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationResult,
)
from backend.services.reporting.report_section_plan import get_report_section_plan
from backend.services.reporting.report_section_prompt import RenderedReportSectionPrompt
from backend.services.reporting.report_worker import ReportWorker
from backend.services.reporting.source_watermark_service import SourceWatermarkService


SUPPORTED_FINDING_TEXT = "SUPPORTED-RCE-FINDING"
SUPPORTED_EVIDENCE_TEXT = "SUPPORTED-EVIDENCE-OPENSSH-7"
LIMITED_ALLOWED_TEXT = "LIMITED-CREDENTIAL-GAP"
LIMITED_FORBIDDEN_TEXT = "LIMITED-FORBIDDEN-REPORTABLE"
CANDIDATE_ONLY_TEXT = "CANDIDATE-ONLY-SHOULD-NOT-APPEAR"


def _build_session() -> Session:
    assert backend_models.__all__
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return factory()


def _seed_scope(db: Session) -> tuple[Tenant, User, Engagement]:
    suffix = uuid.uuid4().hex[:8]
    tenant = Tenant(slug=f"tenant-flow-{suffix}", name="Tenant flow")
    user = User(username=f"user-flow-{suffix}", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="External Assessment",
        description="End-to-end report generation regression scope.",
    )
    db.add(engagement)
    db.flush()
    return tenant, user, engagement


def _add_stopped_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    name: str,
) -> Task:
    task = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        name=name,
        description=f"{name} description",
        scope=f"{name} scope",
        status=TaskStatus.STOPPED.value,
        stopped_at=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
    )
    db.add(task)
    db.flush()
    return task


def _add_evidence(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    excerpt: str,
) -> KnowledgeEvidenceArchive:
    row = KnowledgeEvidenceArchive(
        tenant_id=task.tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task.id,
        source_execution_id=uuid.uuid4(),
        storage_mode="inline_excerpt",
        inline_excerpt=excerpt,
        lineage_snapshot={"target": f"task-{task.id}:443", "source_tool": "nmap"},
        archive_metadata={"type": "service", "target": f"task-{task.id}:443"},
        created_at=datetime(2026, 6, 9, 12, 30, tzinfo=UTC),
    )
    db.add(row)
    db.flush()
    return row


def _add_finding_with_provenance(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    evidence: KnowledgeEvidenceArchive,
    title: str,
    candidate: bool,
) -> KnowledgeFinding:
    now = datetime(2026, 6, 9, 12, 35, tzinfo=UTC)
    finding = KnowledgeFinding(
        tenant_id=task.tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        finding_key=f"finding-{task.id}-{uuid.uuid4().hex[:8]}",
        finding_type="service",
        subject_type="service",
        subject_key=f"task-{task.id}:443",
        title=title,
        severity="high" if not candidate else "medium",
        status="candidate" if candidate else "open",
        assertion_level="candidate" if candidate else "confirmed",
        confidence="high",
        first_seen_at=now,
        last_seen_at=now,
        evidence_summary={"evidence_archive_id": str(evidence.id)},
        finding_metadata={},
    )
    db.add(finding)
    db.flush()

    provenance = KnowledgeEntityProvenance(
        tenant_id=task.tenant_id,
        user_id=user_id,
        entity_type="finding",
        entity_id=finding.id,
        engagement_id=engagement_id,
        task_id=task.id,
        execution_id=evidence.source_execution_id,
        tool_name="nmap",
        observed_at=now,
        confidence="high",
        evidence_archive_id=evidence.id,
    )
    db.add(provenance)
    db.flush()
    return finding


def _add_current_ready_memo(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    memo_mode: str,
    memo: dict[str, Any],
    source_watermark: dict[str, Any],
) -> TaskClosureMemo:
    row = TaskClosureMemo(
        tenant_id=task.tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task.id,
        version=1,
        is_current=True,
        status=MEMO_STATUS_READY,
        memo_mode=memo_mode,
        source_watermark=source_watermark,
        memo=memo,
        generated_at=datetime(2026, 6, 9, 13, 0, tzinfo=UTC),
    )
    db.add(row)
    db.flush()
    return row


def _supported_memo_body(
    *,
    knowledge_ref: str,
    evidence_ref: str,
) -> dict[str, Any]:
    return {
        "summary": "Supported task memo summary.",
        "actions_performed": [{"text": "Ran service enumeration."}],
        "reportable_observations": [
            {
                "text": SUPPORTED_EVIDENCE_TEXT,
                "knowledge_refs": [knowledge_ref],
                "evidence_refs": [evidence_ref],
            }
        ],
        "possible_findings": [
            {
                "title": SUPPORTED_FINDING_TEXT,
                "severity": "high",
                "confidence": "high",
                "knowledge_refs": [knowledge_ref],
                "evidence_refs": [evidence_ref],
            }
        ],
        "limitations": [],
        "unsupported_notes": [],
        "knowledge_refs": [knowledge_ref],
        "evidence_refs": [evidence_ref],
    }


def _limited_memo_body() -> dict[str, Any]:
    return {
        "summary": "Limited task memo summary.",
        "actions_performed": [{"text": LIMITED_FORBIDDEN_TEXT}],
        "reportable_observations": [{"text": LIMITED_FORBIDDEN_TEXT}],
        "possible_findings": [{"title": LIMITED_FORBIDDEN_TEXT}],
        "limitations": [{"text": LIMITED_ALLOWED_TEXT}],
        "unsupported_notes": [{"text": f"Unsupported: {LIMITED_ALLOWED_TEXT}"}],
        "knowledge_refs": ["knowledge_finding:limited-should-not-ground"],
        "evidence_refs": ["evidence_archive:limited-should-not-ground"],
    }


def _add_previous_current_report(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    memo_id: str,
) -> EngagementReport:
    row = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type=REPORT_TYPE_PENTEST,
        version=1,
        status=REPORT_STATUS_READY,
        is_current=True,
        title="Previous Pentest Report",
        sections=[],
        markdown_snapshot="# Previous Pentest Report\n",
        source_task_memo_ids=[memo_id],
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata={
            GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "previous-hash"
        },
        generated_at=datetime(2026, 6, 9, 13, 30, tzinfo=UTC),
    )
    db.add(row)
    db.flush()
    return row


class _FakeProviderNeutralSectionGenerator:
    def __init__(
        self,
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        previous_report_id: uuid.UUID,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._engagement_id = engagement_id
        self._previous_report_id = previous_report_id
        self.calls: list[str] = []
        self.context_payloads: list[dict[str, Any]] = []
        self.previous_current_checks: list[bool] = []

    async def generate(
        self,
        *,
        rendered_prompt: RenderedReportSectionPrompt,
        **_kwargs: Any,
    ) -> ReportSectionGenerationResult:
        context = json.loads(rendered_prompt.report_context_json)
        section_plan = json.loads(rendered_prompt.section_plan_json)
        section_id = str(section_plan["section_id"])
        section_type = str(section_plan["section_type"])
        self.calls.append(section_id)
        self.context_payloads.append(context)
        self._assert_context_boundaries(context)
        self._assert_previous_current_is_still_visible()

        supported_memo_id = context["memo_partitions"]["supported_memo_ids"][0]
        limited_memo_id = context["memo_partitions"]["limited_memo_ids"][0]
        knowledge_ref = context["allowed_knowledge_refs"][0]
        evidence_ref = context["allowed_evidence_refs"][0]

        if section_type == REPORT_SECTION_TYPE_LIMITATIONS:
            source_refs = _source_refs(
                task_memo_ids=[limited_memo_id],
                knowledge_refs=[],
                evidence_refs=[],
            )
            content = f"{LIMITED_ALLOWED_TEXT} should be treated as a limitation."
            unsupported_notes = [f"Unsupported context: {LIMITED_ALLOWED_TEXT}"]
        elif section_type == REPORT_SECTION_TYPE_APPENDIX:
            source_refs = _source_refs(
                task_memo_ids=[supported_memo_id, limited_memo_id],
                knowledge_refs=[knowledge_ref],
                evidence_refs=[evidence_ref],
            )
            content = "Source index for selected memos and compatible references."
            unsupported_notes = []
        else:
            source_refs = _source_refs(
                task_memo_ids=[supported_memo_id],
                knowledge_refs=[knowledge_ref],
                evidence_refs=[evidence_ref],
            )
            content = f"{SUPPORTED_FINDING_TEXT} is grounded by compatible sources."
            unsupported_notes = []

        blocks = []
        if section_type == REPORT_SECTION_TYPE_FINDINGS:
            blocks.append(
                {
                    "block_id": f"{section_id}-finding-1",
                    "block_type": "finding",
                    "title": SUPPORTED_FINDING_TEXT,
                    "severity": "high",
                    "confidence": "high",
                    "affected_assets": ["task-scope:443"],
                    "content_markdown": f"{SUPPORTED_FINDING_TEXT} details.",
                    "impact_markdown": "Remote access risk.",
                    "remediation_markdown": "Patch or disable the affected service.",
                    "source_refs": source_refs,
                }
            )

        return ReportSectionGenerationResult(
            payload={
                "schema_version": REPORT_SECTION_SCHEMA_VERSION,
                "section_id": section_id,
                "section_type": section_type,
                "title": str(section_plan["title"]),
                "status": REPORT_SECTION_STATUS_READY,
                "content_markdown": content,
                "blocks": blocks,
                "source_refs": source_refs,
                "unsupported_notes": unsupported_notes,
                "generation_notes": ["fake provider-neutral section generator"],
            },
            metadata={
                "section_id": section_id,
                "provider": "fake-provider-neutral",
                "model": "no-network",
            },
        )

    def _assert_context_boundaries(self, context: dict[str, Any]) -> None:
        context_json = json.dumps(context, sort_keys=True)
        assert context["include_candidate_findings"] is False
        assert CANDIDATE_ONLY_TEXT not in context_json
        assert LIMITED_FORBIDDEN_TEXT not in context_json

        limited_ids = set(context["memo_partitions"]["limited_memo_ids"])
        limited_memos = [
            memo for memo in context["selected_memos"] if memo["memo_id"] in limited_ids
        ]
        assert len(limited_memos) == 1
        limited_body = limited_memos[0]["body"]
        assert limited_body["actions_performed"] == []
        assert limited_body["reportable_observations"] == []
        assert limited_body["possible_findings"] == []
        assert limited_body["knowledge_refs"] == []
        assert limited_body["evidence_refs"] == []
        assert LIMITED_ALLOWED_TEXT in json.dumps(
            limited_body["limitations"], sort_keys=True
        )
        assert LIMITED_ALLOWED_TEXT in json.dumps(
            limited_body["unsupported_notes"], sort_keys=True
        )

    def _assert_previous_current_is_still_visible(self) -> None:
        current_ready_reports = (
            self._db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == self._tenant_id,
                EngagementReport.user_id == self._user_id,
                EngagementReport.engagement_id == self._engagement_id,
                EngagementReport.report_type == REPORT_TYPE_PENTEST,
                EngagementReport.status == REPORT_STATUS_READY,
                EngagementReport.is_current.is_(True),
            )
            .all()
        )
        self.previous_current_checks.append(
            [report.id for report in current_ready_reports]
            == [self._previous_report_id]
        )
        assert self.previous_current_checks[-1]


class _FakeReportingSelectionService:
    def build_runtime_selection(self, *, user_id: int) -> LLMRuntimeSelection:
        return LLMRuntimeSelection(
            provider="anthropic",
            model="claude-haiku-report",
            credential_ref=LLMCredentialRef(user_id=user_id, provider="anthropic"),
            reasoning_effort=None,
        )


def _source_refs(
    *,
    task_memo_ids: list[str],
    knowledge_refs: list[str],
    evidence_refs: list[str],
) -> dict[str, list[str]]:
    return {
        "task_memo_ids": task_memo_ids,
        "knowledge_refs": knowledge_refs,
        "evidence_refs": evidence_refs,
    }


@pytest.mark.asyncio
async def test_generation_service_and_worker_create_ready_current_report() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db)
    supported_task = _add_stopped_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name="Supported task",
    )
    limited_task = _add_stopped_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name="Limited task",
    )

    confirmed_evidence = _add_evidence(
        db,
        task=supported_task,
        user_id=user.id,
        engagement_id=engagement.id,
        excerpt=SUPPORTED_EVIDENCE_TEXT,
    )
    confirmed_finding = _add_finding_with_provenance(
        db,
        task=supported_task,
        user_id=user.id,
        engagement_id=engagement.id,
        evidence=confirmed_evidence,
        title=SUPPORTED_FINDING_TEXT,
        candidate=False,
    )
    candidate_evidence = _add_evidence(
        db,
        task=supported_task,
        user_id=user.id,
        engagement_id=engagement.id,
        excerpt=CANDIDATE_ONLY_TEXT,
    )
    _add_finding_with_provenance(
        db,
        task=supported_task,
        user_id=user.id,
        engagement_id=engagement.id,
        evidence=candidate_evidence,
        title=CANDIDATE_ONLY_TEXT,
        candidate=True,
    )

    source_watermarks = SourceWatermarkService(db)
    supported_memo = _add_current_ready_memo(
        db,
        task=supported_task,
        user_id=user.id,
        engagement_id=engagement.id,
        memo_mode=MEMO_MODE_SUPPORTED,
        memo=_supported_memo_body(
            knowledge_ref=f"knowledge_finding:{confirmed_finding.id}",
            evidence_ref=f"evidence_archive:{confirmed_evidence.id}",
        ),
        source_watermark=source_watermarks.compute_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=supported_task.id,
        ),
    )
    limited_memo = _add_current_ready_memo(
        db,
        task=limited_task,
        user_id=user.id,
        engagement_id=engagement.id,
        memo_mode=MEMO_MODE_LIMITED,
        memo=_limited_memo_body(),
        source_watermark=source_watermarks.compute_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=limited_task.id,
        ),
    )
    previous_report = _add_previous_current_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        memo_id=str(supported_memo.id),
    )
    db.commit()

    request_result = ReportGenerationService(
        db,
        reporting_selection_service=_FakeReportingSelectionService(),  # type: ignore[arg-type]
    ).request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type=REPORT_TYPE_PENTEST,
        selected_task_memo_ids=[str(supported_memo.id), str(limited_memo.id)],
        engagement_is_owned=True,
        include_candidate_findings=False,
    )
    assert request_result.status == REPORT_JOB_STATUS_QUEUED
    assert request_result.job_id is not None
    db.commit()

    generator = _FakeProviderNeutralSectionGenerator(
        db=db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        previous_report_id=previous_report.id,
    )
    worker_result = await ReportWorker(
        db,
        section_generator=generator,  # type: ignore[arg-type]
    ).run_once(worker_id="report-flow-worker")

    assert worker_result.status == REPORT_JOB_STATUS_READY
    assert worker_result.report_id is not None
    job = (
        db.query(EngagementReportJob)
        .filter(EngagementReportJob.id == request_result.job_id)
        .one()
    )
    report = (
        db.query(EngagementReport)
        .filter(EngagementReport.id == worker_result.report_id)
        .one()
    )
    db.refresh(previous_report)

    expected_section_ids = [
        section.section_id for section in get_report_section_plan(REPORT_TYPE_PENTEST).sections
    ]
    assert generator.calls == expected_section_ids
    assert all(generator.previous_current_checks)
    assert job.status == REPORT_JOB_STATUS_READY
    assert job.report_id == report.id
    assert job.completed_sections == expected_section_ids
    assert job.finished_at is not None

    assert previous_report.status == REPORT_STATUS_READY
    assert previous_report.is_current is False
    assert report.status == REPORT_STATUS_READY
    assert report.is_current is True
    assert report.version == 2
    assert report.source_task_memo_ids == sorted(
        [str(supported_memo.id), str(limited_memo.id)]
    )
    confirmed_knowledge_ref = f"knowledge_finding:{confirmed_finding.id}"
    confirmed_evidence_ref = f"evidence_archive:{confirmed_evidence.id}"
    assert report.markdown_snapshot is not None
    assert report.markdown_snapshot.startswith("# Pentest Report")
    assert SUPPORTED_FINDING_TEXT in report.markdown_snapshot
    assert LIMITED_ALLOWED_TEXT in report.markdown_snapshot
    assert "### Evidence Index" in report.markdown_snapshot
    assert "Evidence type:" not in report.markdown_snapshot
    assert "Result:" not in report.markdown_snapshot
    assert "Output:" not in report.markdown_snapshot
    assert "Nmap" in report.markdown_snapshot
    assert str(confirmed_evidence.id) in report.markdown_snapshot
    assert "SUPPORTED-EVIDENCE-OPENSSH-7" not in report.markdown_snapshot
    assert "Source refs" not in report.markdown_snapshot
    assert confirmed_knowledge_ref not in report.markdown_snapshot
    assert confirmed_evidence_ref not in report.markdown_snapshot

    sections_by_id = {section["section_id"]: section for section in report.sections}
    assert list(sections_by_id) == expected_section_ids
    assert _section_text(sections_by_id["limitations"]).count(LIMITED_ALLOWED_TEXT) >= 2
    for section_id, section in sections_by_id.items():
        if section_id == "limitations":
            continue
        assert LIMITED_ALLOWED_TEXT not in _section_text(section)
    assert LIMITED_FORBIDDEN_TEXT not in _report_text(report)
    assert CANDIDATE_ONLY_TEXT not in _report_text(report)

    expected_knowledge_refs = [
        {
            "ref": confirmed_knowledge_ref,
            "task_id": supported_task.id,
            "record_type": "finding",
            "authoritative": True,
        }
    ]
    expected_evidence_refs = [
        {
            "ref": confirmed_evidence_ref,
            "task_id": supported_task.id,
            "evidence_type": "service",
            "source_tool": "nmap",
        }
    ]
    assert report.source_knowledge_refs == expected_knowledge_refs
    assert report.source_evidence_refs == expected_evidence_refs
    assert str(candidate_evidence.id) not in _report_text(report)

    read_service = ReportReadService(db)
    direct_report = read_service.get_report(
        tenant_id=tenant.id,
        user_id=user.id,
        report_id=report.id,
    )
    current_report = read_service.get_current_report(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type=REPORT_TYPE_PENTEST,
    ).report
    history = read_service.list_report_history(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type=REPORT_TYPE_PENTEST,
    )

    assert direct_report is not None
    assert current_report is not None
    assert [
        ref.model_dump() for ref in direct_report.source_knowledge_refs
    ] == expected_knowledge_refs
    assert [
        ref.model_dump() for ref in direct_report.source_evidence_refs
    ] == expected_evidence_refs
    assert [
        ref.model_dump() for ref in current_report.source_knowledge_refs
    ] == expected_knowledge_refs
    assert [
        ref.model_dump() for ref in current_report.source_evidence_refs
    ] == expected_evidence_refs
    assert history.reports[0].report_id == report.id
    assert [
        ref.model_dump() for ref in history.reports[0].source_knowledge_refs
    ] == expected_knowledge_refs
    assert [
        ref.model_dump() for ref in history.reports[0].source_evidence_refs
    ] == expected_evidence_refs

    metadata = report.generation_metadata
    assert (
        metadata[GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY]
        == job.source_watermark["hash"]
    )
    assert metadata[GENERATION_METADATA_SECTION_PLAN_VERSION_KEY]
    assert metadata[GENERATION_METADATA_RENDERER_VERSION_KEY]
    assert metadata["sections"][0]["generation"]["provider"] == "fake-provider-neutral"
    assert metadata["sections"][0]["generation"]["model"] == "no-network"
    assert "system_prompt" not in json.dumps(metadata, sort_keys=True)
    assert "user_prompt" not in json.dumps(metadata, sort_keys=True)


def _section_text(section: dict[str, Any]) -> str:
    return json.dumps(section, sort_keys=True)


def _report_text(report: EngagementReport) -> str:
    return json.dumps(
        {
            "sections": report.sections,
            "markdown_snapshot": report.markdown_snapshot,
            "source_knowledge_refs": report.source_knowledge_refs,
            "source_evidence_refs": report.source_evidence_refs,
            "generation_metadata": report.generation_metadata,
        },
        sort_keys=True,
    )
