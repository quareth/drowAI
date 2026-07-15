"""Tests for core candidate extraction service orchestration behavior."""

from __future__ import annotations



import asyncio
import logging

from datetime import datetime, timezone

import uuid as uuid_lib



import pytest



from backend.models.knowledge import KnowledgeEvidenceArchive

from backend.services.knowledge.candidate_extraction import (

    CandidateExtractionRequest,

    KnowledgeCandidateExtractionService,

    ReplayEvidenceSource,

)

from backend.services.usage_tracking.models import UsageData

from backend.tests.services._knowledge_candidate_extraction_test_support import (

    _FakeLLMClient,

    _RaisingLLMClient,

    _build_session,

    _seed_user_engagement_task,

)



@pytest.fixture(autouse=True)
def _enable_candidate_feature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")



def test_service_skips_when_candidate_feature_flag_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "false")
    engine, db = _build_session()
    try:
        fake_client = _FakeLLMClient(structured_output={"candidate_observations": [], "analyst_notes": [], "no_signal": True})
        service = KnowledgeCandidateExtractionService(db, llm_client=fake_client)
        result = asyncio.run(
            service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=1,
                    source_execution_id="exec-flag-off",
                    ingestion_run_id="run-flag-off",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family=None,
                )
            )
        )
        assert result.status == "skipped"
        assert result.policy_decision is not None
        assert result.policy_decision.reason == "candidate_feature_disabled"
        assert len(fake_client.calls) == 0
    finally:
        db.close()
        engine.dispose()

def test_service_extracts_candidates_from_durable_archive_rows() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="Authorization: Bearer SECRET_TOKEN_123456789",
            archived_file_ref=None,
            content_sha256="a" * 64,
            byte_size=64,
            mime_type="text/plain",
            lineage_snapshot={"artifact_kind": "stdout"},
            archive_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        db.add(evidence)
        db.flush()

        fake_client = _FakeLLMClient(
            usage=UsageData(
                prompt_tokens=180,
                completion_tokens=120,
                total_tokens=300,
                model="gpt-5-mini",
            ),
            structured_output={
                "candidate_observations": [
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key_hint": "cve-2021-44228:http://10.0.0.7/",
                        "assertion_level": "candidate",
                        "confidence": 0.86,
                        "attributes": {"title": "Possible vulnerability"},
                        "rationale": "Response marker aligns with known indicator",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": str(evidence.id),
                                "excerpt": "Bearer marker appeared in output",
                            }
                        ],
                    }
                ],
                "analyst_notes": [],
                "no_signal": False,
            },
        )
        service = KnowledgeCandidateExtractionService(db, llm_client=fake_client)
        result = asyncio.run(
            service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-candidate-replay-1",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                    compact_output_hint={"summary": "Authorization: Bearer SECRET_TOKEN_123456789"},
                )
            )
        )

        assert result.status == "succeeded"
        assert len(result.observations) == 1
        assert result.observations[0].assertion_level == "candidate"
        assert result.usage_summary is not None
        assert result.usage_summary.total_tokens == 300
        assert result.usage_summary.estimated_cost_usd > 0
        assert result.durable_masking_applied is True
        assert fake_client.calls
        assert "SECRET_TOKEN_123456789" not in fake_client.calls[0]["user_prompt"]
        assert "<EVIDENCE_DATA_START>" in fake_client.calls[0]["user_prompt"]
        assert "<EVIDENCE_DATA_END>" in fake_client.calls[0]["user_prompt"]
        assert "\"bundle_format\": \"candidate_evidence_v1\"" in fake_client.calls[0]["user_prompt"]
    finally:
        db.close()
        engine.dispose()


def test_service_supports_replay_sources_with_bounded_normalization() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        fake_client = _FakeLLMClient(
            structured_output={
                "candidate_observations": [
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key_hint": "marker:http://10.0.0.8/",
                        "assertion_level": "candidate",
                        "confidence": 0.85,
                        "attributes": {"title": "Replay marker"},
                        "rationale": "Replay evidence contained known marker",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": "replay-src-1",
                                "excerpt": "marker present in replay payload",
                            }
                        ],
                    }
                ],
                "analyst_notes": [{"note": "Replay extraction path exercised", "evidence_refs": []}],
                "no_signal": False,
            },
        )
        service = KnowledgeCandidateExtractionService(db, llm_client=fake_client)
        result = asyncio.run(
            service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-candidate-replay-replay",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="replay_backfill",
                    tool_name="replay.execution",
                    capability_family="web_scan",
                    replay_sources=(
                        ReplayEvidenceSource(
                            evidence_archive_id="replay-src-1",
                            artifact_kind="stdout",
                            content="Replay payload with marker " + ("x" * 4000),
                            mode="head",
                        ),
                    ),
                    max_evidence_chars_per_item=200,
                )
            )
        )

        assert result.status == "succeeded"
        assert result.evidence_archive_ids_used == ("replay-src-1",)
        assert len(result.observations) == 1
        assert result.observations[0].observation_metadata["extraction_mode"] == "replay_backfill"
    finally:
        db.close()
        engine.dispose()


def test_service_logs_timeout_and_returns_failed_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _TimeoutLLMClient:
        @property
        def model(self) -> str:
            return "gpt-5-mini"

        async def chat_with_usage(self, system_prompt: str, user_prompt: str, **_kwargs):
            _ = system_prompt, user_prompt
            await asyncio.sleep(0.05)
            return None

    engine, db = _build_session()
    try:
        monkeypatch.setattr(
            "backend.services.knowledge.candidate_extraction.service.LLM_TIMEOUT_KNOWLEDGE_CANDIDATE_EXTRACTION_SEC",
            0.01,
        )
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="candidate timeout marker",
            archived_file_ref=None,
            content_sha256="b" * 64,
            byte_size=32,
            mime_type="text/plain",
            lineage_snapshot={"artifact_kind": "stdout"},
            archive_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        db.add(evidence)
        db.flush()

        service = KnowledgeCandidateExtractionService(db, llm_client=_TimeoutLLMClient())

        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                service.extract_candidates(
                    request=CandidateExtractionRequest(
                        engagement_id=engagement.id,
                        task_id=task.id,
                        source_execution_id=execution_id,
                        ingestion_run_id="run-timeout",
                        extractor_family="llm.candidate_extraction",
                        extractor_version="1.0",
                        extraction_mode="candidate_fallback",
                        tool_name="shell.exec",
                        capability_family="web_scan",
                    )
                )
            )

        assert result.status == "failed"
        assert "TimeoutError" in (result.failure_reason or "")
        assert (
            f"TIMEOUT | Task {task.id} | KNOWLEDGE_CANDIDATE_EXTRACTION | "
            "candidate_extractor_llm_call"
        ) in caplog.text
    finally:
        db.close()
        engine.dispose()

def test_service_enforces_evidence_refs_before_emitting_candidate_observations() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="Potential signal text",
            archived_file_ref=None,
            content_sha256="b" * 64,
            byte_size=32,
            mime_type="text/plain",
            lineage_snapshot={"artifact_kind": "stdout"},
            archive_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        db.add(evidence)
        db.flush()

        fake_client = _FakeLLMClient(
            structured_output={
                "candidate_observations": [
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key_hint": "invalid:http://10.0.0.9/",
                        "assertion_level": "candidate",
                        "confidence": 0.6,
                        "attributes": {"title": "Should be dropped"},
                        "rationale": "No usable evidence references",
                        "evidence_refs": [],
                    }
                ],
                "analyst_notes": [],
                "no_signal": False,
            },
        )
        service = KnowledgeCandidateExtractionService(db, llm_client=fake_client)
        result = asyncio.run(
            service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-candidate-replay-2",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                )
            )
        )

        assert result.status == "no_signal"
        assert not result.observations
    finally:
        db.close()
        engine.dispose()

def test_service_failure_reason_does_not_leak_secret_payloads() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="Authorization: Bearer SECRET_TOKEN_123456789",
            archived_file_ref=None,
            content_sha256="c" * 64,
            byte_size=64,
            mime_type="text/plain",
            lineage_snapshot={"artifact_kind": "stdout"},
            archive_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        db.add(evidence)
        db.flush()

        service = KnowledgeCandidateExtractionService(db, llm_client=_RaisingLLMClient())
        result = asyncio.run(
            service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-candidate-replay-fail",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                )
            )
        )

        assert result.status == "failed"
        assert result.failure_reason == "candidate_extractor_llm_call_failed:RuntimeError"
        assert "SECRET_TOKEN_123456789" not in str(result.failure_reason or "")
    finally:
        db.close()
        engine.dispose()

def test_service_marks_durable_masking_applied_when_only_compact_hint_contains_secret() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="plain non-secret signal text",
            archived_file_ref=None,
            content_sha256="d" * 64,
            byte_size=64,
            mime_type="text/plain",
            lineage_snapshot={"artifact_kind": "stdout"},
            archive_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        db.add(evidence)
        db.flush()

        fake_client = _FakeLLMClient(
            structured_output={
                "candidate_observations": [],
                "analyst_notes": [],
                "no_signal": True,
            },
        )
        service = KnowledgeCandidateExtractionService(db, llm_client=fake_client)
        result = asyncio.run(
            service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-candidate-replay-compact-hint-redaction",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                    compact_output_hint={"token": "SECRET_TOKEN_123456789"},
                )
            )
        )

        assert result.durable_masking_applied is True
        assert fake_client.calls
        assert "SECRET_TOKEN_123456789" not in fake_client.calls[0]["user_prompt"]
    finally:
        db.close()
        engine.dispose()

def test_service_emits_metrics_for_success_failure_and_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    inc_calls: list[tuple[str, int]] = []
    gauge_calls: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "backend.services.knowledge.candidate_extraction.service.safe_inc",
        lambda name, value=1: inc_calls.append((str(name), int(value))),
    )
    monkeypatch.setattr(
        "backend.services.knowledge.candidate_extraction.service.safe_gauge",
        lambda name, value: gauge_calls.append((str(name), float(value))),
    )

    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = str(uuid_lib.uuid4())
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=execution_id,
                source_artifact_id=uuid_lib.uuid4(),
                storage_mode="inline_excerpt",
                inline_excerpt="possible marker",
                archived_file_ref=None,
                content_sha256="e" * 64,
                byte_size=32,
                mime_type="text/plain",
                lineage_snapshot={"artifact_kind": "stdout"},
                archive_metadata={},
                created_at=datetime.now(timezone.utc),
            )
        )
        db.flush()

        success_service = KnowledgeCandidateExtractionService(
            db,
            llm_client=_FakeLLMClient(
                structured_output={
                    "candidate_observations": [
                        {
                            "observation_type": "finding.vulnerability_detected",
                            "subject_type": "finding.instance",
                            "subject_key_hint": "metric:test",
                            "assertion_level": "candidate",
                            "confidence": 0.84,
                            "attributes": {"title": "Metric candidate"},
                            "rationale": "signal",
                            "evidence_refs": [
                                {
                                    "evidence_archive_id": str(
                                        db.query(KnowledgeEvidenceArchive.id).first()[0]
                                    ),
                                    "excerpt": "marker",
                                }
                            ],
                        }
                    ],
                    "analyst_notes": [],
                    "no_signal": False,
                }
            ),
        )
        success_result = asyncio.run(
            success_service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-metric-success",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                )
            )
        )
        assert success_result.status == "succeeded"

        no_signal_result = asyncio.run(
            success_service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=str(uuid_lib.uuid4()),
                    ingestion_run_id="run-metric-no-signal",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                )
            )
        )
        assert no_signal_result.status == "no_signal"

        failure_service = KnowledgeCandidateExtractionService(db, llm_client=_RaisingLLMClient())
        failed_result = asyncio.run(
            failure_service.extract_candidates(
                request=CandidateExtractionRequest(
                    engagement_id=engagement.id,
                    task_id=task.id,
                    source_execution_id=execution_id,
                    ingestion_run_id="run-metric-failed",
                    extractor_family="llm.candidate_extraction",
                    extractor_version="1.0",
                    extraction_mode="candidate_fallback",
                    tool_name="shell.exec",
                    capability_family="web_scan",
                )
            )
        )
        assert failed_result.status == "failed"
    finally:
        db.close()
        engine.dispose()

    counter_totals: dict[str, int] = {}
    for name, value in inc_calls:
        counter_totals[name] = counter_totals.get(name, 0) + value
    assert counter_totals.get("knowledge_candidate_extraction_total", 0) >= 3
    assert counter_totals.get("knowledge_candidate_extraction_failed_total", 0) >= 1
    assert counter_totals.get("knowledge_candidate_no_signal_total", 0) >= 1
    assert any(name == "knowledge_extraction_duration_seconds" for name, _ in gauge_calls)
