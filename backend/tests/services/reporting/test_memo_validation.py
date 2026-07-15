"""Tests for task closure memo output validation."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from backend.services.reporting.contracts import (
    MEMO_MODE_LIMITED,
    MEMO_MODE_SUPPORTED,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
)
from backend.services.reporting.evidence_packet_builder import (
    EvidencePacket,
    EvidencePacketItem,
)
from backend.services.reporting.knowledge_packet_builder import (
    KnowledgePacket,
    KnowledgePacketItem,
)
from backend.services.reporting.runtime_readiness_service import RuntimeReadiness
from backend.services.reporting.task_memo_context_builder import (
    TaskMemoContext,
    TaskMemoTaskMetadata,
)
from backend.services.reporting.transcript_context_builder import TranscriptContext
from backend.services.reporting.validation import (
    TaskClosureMemoValidationError,
    validate_task_closure_memo,
)


def _valid_payload() -> dict[str, Any]:
    return {
        "task_name": "Inspect service exposure",
        "summary": "Task closure memo summary.",
        "include_in_report_recommendation": {
            "include": True,
            "reason": "Evidence-backed service exposure was observed.",
        },
        "actions_performed": [
            {"text": "Reviewed service scan output.", "source": "evidence"}
        ],
        "reportable_observations": [
            {
                "text": "HTTPS was exposed.",
                "confidence": "high",
                "evidence_refs": ["evidence_archive:1"],
                "knowledge_refs": [],
            }
        ],
        "possible_findings": [],
        "limitations": [],
        "unsupported_notes": [],
        "evidence_refs": ["evidence_archive:1"],
        "knowledge_refs": [],
    }


def _limited_payload() -> dict[str, Any]:
    return {
        **_valid_payload(),
        "actions_performed": [{"text": "Checked the target.", "source": "transcript"}],
        "reportable_observations": [],
        "possible_findings": [],
        "limitations": [{"text": "No durable evidence was available."}],
        "unsupported_notes": [{"text": "Transcript mentioned a scan result."}],
        "evidence_refs": [],
        "knowledge_refs": [],
    }


def _context(
    *,
    memo_mode: str = MEMO_MODE_SUPPORTED,
    task_id: int = 42,
    evidence_task_id: int | None = None,
    knowledge_task_id: int | None = None,
    evidence_items: tuple[EvidencePacketItem, ...] | None = None,
    knowledge_items: tuple[KnowledgePacketItem, ...] | None = None,
    allowed_evidence_refs: frozenset[str] | None = None,
    allowed_knowledge_refs: frozenset[str] | None = None,
) -> TaskMemoContext:
    evidence_items = evidence_items if evidence_items is not None else (_evidence_item(),)
    knowledge_items = knowledge_items if knowledge_items is not None else (
        _knowledge_item(),
    )
    return TaskMemoContext(
        task=TaskMemoTaskMetadata(
            task_id=task_id,
            tenant_id=7,
            user_id=11,
            engagement_id=13,
            name="Inspect service exposure",
            description=None,
            scope=None,
            status="stopped",
            created_at=None,
            stopped_at=None,
        ),
        source_watermark=MappingProxyType({}),
        transcript=TranscriptContext(
            task_id=task_id,
            conversation_id=None,
            items=(),
            message_count=0,
            detail_event_count=0,
            total_characters=0,
            truncated=False,
            max_messages=80,
            max_characters=12000,
        ),
        knowledge=KnowledgePacket(
            task_id=knowledge_task_id if knowledge_task_id is not None else task_id,
            items=knowledge_items,
            canonical_item_count=len(
                [item for item in knowledge_items if item.record_type != "observation"]
            ),
            observation_item_count=len(
                [item for item in knowledge_items if item.record_type == "observation"]
            ),
            candidate_item_count=len(
                [item for item in knowledge_items if not item.authoritative]
            ),
            truncated=False,
            max_items=120,
        ),
        evidence=EvidencePacket(
            task_id=evidence_task_id if evidence_task_id is not None else task_id,
            items=evidence_items,
            item_count=len(evidence_items),
            artifact_fallback_count=0,
            total_excerpt_characters=sum(len(item.excerpt) for item in evidence_items),
            truncated=False,
            max_items=80,
            max_excerpt_characters=1500,
            max_total_characters=12000,
        ),
        previous_memo=None,
        runtime_readiness=RuntimeReadiness(
            runtime_retired=True,
            useful_runtime_execution=True,
            not_preparable_reason=None,
        ),
        memo_mode=memo_mode,  # type: ignore[arg-type]
        not_preparable_reason=None,
        allowed_evidence_refs=allowed_evidence_refs
        if allowed_evidence_refs is not None
        else frozenset(item.ref for item in evidence_items),
        allowed_knowledge_refs=allowed_knowledge_refs
        if allowed_knowledge_refs is not None
        else frozenset(item.ref for item in knowledge_items),
    )


def _evidence_item(ref: str = "evidence_archive:1") -> EvidencePacketItem:
    return EvidencePacketItem(
        ref=ref,
        evidence_id=ref.rsplit(":", 1)[-1],
        tenant_id=7,
        user_id=11,
        engagement_id=13,
        task_id=42,
        source_execution_id="exec-1",
        source_artifact_id=None,
        observed_at=None,
        created_at=None,
        source_tool="nmap",
        evidence_type="scan",
        target="example.test",
        summary="nmap scan output",
        excerpt="443/tcp open https",
        excerpt_source="inline_excerpt",
        excerpt_truncated=False,
        linked_asset_refs=(),
        linked_service_refs=(),
        linked_finding_refs=(),
        byte_size=None,
        mime_type=None,
    )


def _knowledge_item(
    ref: str = "knowledge_service:1",
    *,
    authoritative: bool = True,
    record_type: str = "service",
    tenant_id: int = 7,
    user_id: int = 11,
    engagement_id: int = 13,
    task_id: int = 42,
) -> KnowledgePacketItem:
    return KnowledgePacketItem(
        ref=ref,
        record_id=ref.rsplit(":", 1)[-1],
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        record_type=record_type,  # type: ignore[arg-type]
        summary="HTTPS service on example.test",
        confidence="high" if authoritative else "low",
        assertion_level=None if authoritative else "candidate",
        first_observed_at=None,
        last_observed_at=None,
        source_execution_ids=("exec-1",),
        ingestion_run_ids=(),
        evidence_archive_refs=("1",),
        provenance_refs=(),
        authoritative=authoritative,
        authority="task_local_canonical"
        if authoritative
        else "candidate_low_authority",
    )


def _issue_codes(error: TaskClosureMemoValidationError) -> set[str]:
    return {issue.code for issue in error.issues}


def test_validator_accepts_supported_source_backed_payload() -> None:
    result = validate_task_closure_memo(
        payload=_valid_payload(),
        context=_context(),
    )

    assert result.payload["task_name"] == "Inspect service exposure"
    assert result.metadata["validation_status"] == "passed"


def test_validator_rejects_schema_invalid_payload_without_dumping_content() -> None:
    payload = _valid_payload()
    payload.pop("summary")

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(payload=payload, context=_context())

    error = exc_info.value
    assert error.reason == TASK_MEMO_ERROR_VALIDATION_FAILED
    assert "schema_invalid" in _issue_codes(error)
    assert "HTTPS was exposed" not in error.safe_message
    assert "HTTPS was exposed" not in str(error.metadata)


def test_validator_rejects_unknown_evidence_and_knowledge_refs() -> None:
    payload = _valid_payload()
    payload["evidence_refs"] = ["evidence_archive:missing"]
    payload["knowledge_refs"] = ["knowledge_service:missing"]

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(payload=payload, context=_context())

    assert _issue_codes(exc_info.value) == {
        "unknown_evidence_ref",
        "unknown_knowledge_ref",
    }


def test_validator_rejects_evidence_ref_absent_from_packet_even_if_allowed() -> None:
    payload = _valid_payload()

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=payload,
            context=_context(
                evidence_items=(),
                allowed_evidence_refs=frozenset({"evidence_archive:1"}),
            ),
        )

    assert "unknown_evidence_ref" in _issue_codes(exc_info.value)


def test_validator_rejects_evidence_ref_present_in_packet_but_not_allowed() -> None:
    payload = _valid_payload()

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=payload,
            context=_context(allowed_evidence_refs=frozenset()),
        )

    assert "unknown_evidence_ref" in _issue_codes(exc_info.value)


def test_validator_rejects_knowledge_ref_absent_from_packet_even_if_allowed() -> None:
    payload = _valid_payload()
    payload["reportable_observations"][0]["evidence_refs"] = []
    payload["reportable_observations"][0]["knowledge_refs"] = ["knowledge_service:1"]
    payload["evidence_refs"] = []
    payload["knowledge_refs"] = ["knowledge_service:1"]

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=payload,
            context=_context(
                evidence_items=(),
                knowledge_items=(),
                allowed_knowledge_refs=frozenset({"knowledge_service:1"}),
            ),
        )

    assert "unknown_knowledge_ref" in _issue_codes(exc_info.value)


def test_validator_rejects_knowledge_ref_present_in_packet_but_not_allowed() -> None:
    payload = _valid_payload()
    payload["reportable_observations"][0]["evidence_refs"] = []
    payload["reportable_observations"][0]["knowledge_refs"] = ["knowledge_service:1"]
    payload["evidence_refs"] = []
    payload["knowledge_refs"] = ["knowledge_service:1"]

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=payload,
            context=_context(
                evidence_items=(),
                allowed_knowledge_refs=frozenset(),
            ),
        )

    assert "unknown_knowledge_ref" in _issue_codes(exc_info.value)


def test_validator_rejects_other_task_packet_scope() -> None:
    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=_valid_payload(),
            context=_context(evidence_task_id=99),
        )

    assert "packet_scope_mismatch" in _issue_codes(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", 99),
        ("user_id", 99),
        ("engagement_id", 99),
        ("task_id", 99),
    ],
)
def test_validator_rejects_referenced_evidence_ref_outside_task_scope(
    field: str,
    value: int,
) -> None:
    item = _evidence_item()
    scoped_item = EvidencePacketItem(
        **{**item.__dict__, field: value},
    )

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=_valid_payload(),
            context=_context(evidence_items=(scoped_item,)),
        )

    assert "evidence_ref_scope_mismatch" in _issue_codes(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", 99),
        ("user_id", 99),
        ("engagement_id", 99),
        ("task_id", 99),
    ],
)
def test_validator_rejects_referenced_knowledge_ref_outside_task_scope(
    field: str,
    value: int,
) -> None:
    payload = _valid_payload()
    payload["reportable_observations"][0]["evidence_refs"] = []
    payload["reportable_observations"][0]["knowledge_refs"] = ["knowledge_service:1"]
    payload["evidence_refs"] = []
    payload["knowledge_refs"] = ["knowledge_service:1"]

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=payload,
            context=_context(
                evidence_items=(),
                knowledge_items=(
                    _knowledge_item("knowledge_service:1", **{field: value}),
                ),
            ),
        )

    assert "knowledge_ref_scope_mismatch" in _issue_codes(exc_info.value)


def test_validator_rejects_limited_mode_reportable_content() -> None:
    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=_valid_payload(),
            context=_context(memo_mode=MEMO_MODE_LIMITED),
        )

    assert "limited_mode_reportable_content" in _issue_codes(exc_info.value)


def test_validator_allows_transcript_only_limited_notes_and_actions() -> None:
    result = validate_task_closure_memo(
        payload=_limited_payload(),
        context=_context(
            memo_mode=MEMO_MODE_LIMITED,
            evidence_items=(),
            knowledge_items=(),
        ),
    )

    assert result.payload["actions_performed"][0]["source"] == "transcript"
    assert result.payload["limitations"]
    assert result.payload["unsupported_notes"]


def test_validator_rejects_transcript_only_reportable_items() -> None:
    payload = _valid_payload()
    payload["reportable_observations"][0]["evidence_refs"] = []
    payload["reportable_observations"][0]["knowledge_refs"] = []
    payload["evidence_refs"] = []

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(payload=payload, context=_context())

    assert "schema_invalid" in _issue_codes(exc_info.value)


def test_validator_keeps_candidate_only_findings_low_confidence() -> None:
    candidate_ref = "knowledge_finding:candidate"
    payload = _valid_payload()
    payload["reportable_observations"] = []
    payload["possible_findings"] = [
        {
            "title": "Possible exposed admin panel",
            "severity_hint": "medium",
            "confidence": "medium",
            "description": "Candidate-only signal.",
            "evidence_refs": [],
            "knowledge_refs": [candidate_ref],
        }
    ]
    payload["evidence_refs"] = []
    payload["knowledge_refs"] = [candidate_ref]

    with pytest.raises(TaskClosureMemoValidationError) as exc_info:
        validate_task_closure_memo(
            payload=payload,
            context=_context(
                evidence_items=(),
                knowledge_items=(
                    _knowledge_item(
                        candidate_ref,
                        authoritative=False,
                        record_type="finding",
                    ),
                ),
            ),
        )

    assert "candidate_only_confidence" in _issue_codes(exc_info.value)


def test_validator_allows_low_confidence_candidate_possible_finding() -> None:
    candidate_ref = "knowledge_finding:candidate"
    payload = _valid_payload()
    payload["reportable_observations"] = []
    payload["possible_findings"] = [
        {
            "title": "Possible exposed admin panel",
            "severity_hint": "medium",
            "confidence": "low",
            "description": "Candidate-only signal.",
            "evidence_refs": [],
            "knowledge_refs": [candidate_ref],
        }
    ]
    payload["evidence_refs"] = []
    payload["knowledge_refs"] = [candidate_ref]

    result = validate_task_closure_memo(
        payload=payload,
        context=_context(
            evidence_items=(),
            knowledge_items=(
                _knowledge_item(
                    candidate_ref,
                    authoritative=False,
                    record_type="finding",
                ),
            ),
        ),
    )

    assert result.payload["possible_findings"][0]["confidence"] == "low"
