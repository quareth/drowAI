"""Tests for candidate extraction result contract outcomes and validation."""

from __future__ import annotations



import pytest



from backend.services.knowledge.candidate_extraction import (

    CandidateExtractionPolicyRequest,

    CandidateExtractionResult,

    CandidateExtractionUsageSummary,

    KnowledgeCandidateExtractionPolicy,

)

from backend.services.knowledge.contracts import ObservationCreate



def _candidate_observation() -> ObservationCreate:
    return ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-1",
        ingestion_run_id="run-1",
        observation_type="finding.vulnerability_detected",
        subject_type="finding.instance",
        subject_key="finding.instance:cve-2021-44228:http://10.0.0.5/",
        assertion_level="candidate",
        payload={
            "title": "Potential vulnerability signal",
            "evidence_refs": [{"evidence_archive_id": "archive-1", "excerpt": "marker"}],
        },
    )



def test_candidate_extraction_result_supports_distinct_status_outcomes() -> None:
    run_decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=0,
            native_observation_count=0,
            capability_family="web_scan",
            archived_evidence_count=1,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout",),
            estimated_prompt_tokens=150,
            max_prompt_tokens=1000,
            estimated_cost_usd=0.01,
            max_cost_usd=0.05,
        )
    )
    no_signal_decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=0,
            native_observation_count=0,
            capability_family="web_scan",
            archived_evidence_count=0,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout",),
            estimated_prompt_tokens=0,
            max_prompt_tokens=1000,
            estimated_cost_usd=0.0,
            max_cost_usd=0.05,
        )
    )

    succeeded = CandidateExtractionResult.succeeded(
        observations=[_candidate_observation()],
        evidence_archive_ids_used=["archive-1"],
        durable_masking_applied=True,
        usage_summary=CandidateExtractionUsageSummary(
            input_tokens=120,
            output_tokens=80,
            total_tokens=200,
            estimated_cost_usd=0.02,
        ),
        policy_decision=run_decision,
    )
    no_signal = CandidateExtractionResult.no_signal_result(
        reason="model_returned_no_signal",
        policy_decision=no_signal_decision,
        evidence_archive_ids_used=["archive-1"],
        durable_masking_applied=True,
    )
    failed = CandidateExtractionResult.failed(reason="llm_timeout", policy_decision=run_decision)

    assert succeeded.status == "succeeded"
    assert succeeded.no_signal is False
    assert len(succeeded.observations) == 1
    assert no_signal.status == "no_signal"
    assert no_signal.no_signal is True
    assert failed.status == "failed"
    assert failed.failure_reason == "llm_timeout"

def test_candidate_extraction_result_rejects_non_candidate_observations() -> None:
    observation = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-1",
        ingestion_run_id="run-1",
        observation_type="network.open_port",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.1",
        assertion_level="observed",
        payload={"port": 80},
    )
    with pytest.raises(ValueError, match="assertion_level='candidate'"):
        CandidateExtractionResult.succeeded(observations=[observation])

