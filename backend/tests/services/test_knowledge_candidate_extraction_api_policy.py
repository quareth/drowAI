"""Tests for candidate extraction package exports and policy behavior."""

from __future__ import annotations



import pytest
from types import SimpleNamespace



import backend.services.knowledge.candidate_extraction as extraction_api

from backend.services.knowledge.candidate_extraction import (

    CandidateExtractionPolicyRequest,

    KnowledgeCandidateExtractionPolicy,

)
from backend.services.knowledge.candidate_extraction.service import (
    maybe_run_candidate_extraction,
    record_candidate_usage_if_task_present,
)



def test_package_re_exports_expected_symbols() -> None:
    expected = {
        "CandidateExtractionPolicyDecision",
        "CandidateExtractionPolicyRequest",
        "CandidateExtractionRequest",
        "CandidateExtractionResult",
        "CandidateExtractionStatus",
        "CandidateExtractionUsageSummary",
        "CandidatePolicyAction",
        "KnowledgeCandidateExtractionPolicy",
        "KnowledgeCandidateExtractionService",
        "ReplayEvidenceSource",
    }
    assert expected.issubset(set(dir(extraction_api)))

def test_policy_returns_run_for_eligible_sparse_candidate_case() -> None:
    decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=0,
            native_observation_count=0,
            capability_family="web_scan",
            archived_evidence_count=2,
            artifact_kinds_present=("stdout", "http_response"),
            artifact_kind_allowlist=("stdout", "stderr", "http_response"),
            capability_family_allowlist=("web_scan", "network_scan"),
            estimated_prompt_tokens=1200,
            max_prompt_tokens=2000,
            estimated_cost_usd=0.05,
            max_cost_usd=0.10,
            compact_output_hint={"summary": "possible targets"},
        )
    )

    assert decision.action == "run"
    assert decision.reason == "eligible_for_candidate_extraction"
    assert decision.policy_metadata["matched_artifact_kinds"] == ["http_response", "stdout"]

def test_policy_allows_service_only_deterministic_non_finding_observations() -> None:
    decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=2,
            native_observation_count=0,
            observation_count_finding_total=0,
            observation_count_finding_authoritative=0,
            observation_count_non_finding_total=2,
            capability_family="web_scan",
            archived_evidence_count=2,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout", "stderr"),
            estimated_prompt_tokens=300,
            max_prompt_tokens=2000,
            estimated_cost_usd=0.02,
            max_cost_usd=0.10,
        )
    )
    assert decision.action == "run"
    assert decision.reason == "eligible_for_candidate_extraction"

def test_policy_returns_skip_when_authoritative_signal_exists() -> None:
    decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=1,
            native_observation_count=0,
            observation_count_finding_total=1,
            observation_count_finding_authoritative=1,
            observation_count_non_finding_total=0,
            capability_family="web_scan",
            archived_evidence_count=3,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout",),
            estimated_prompt_tokens=200,
            max_prompt_tokens=2000,
            estimated_cost_usd=0.01,
            max_cost_usd=0.10,
        )
    )
    assert decision.action == "skip"
    assert decision.reason == "authoritative_signal_present"

def test_policy_returns_no_signal_when_allowlisted_evidence_signal_is_missing() -> None:
    decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=0,
            native_observation_count=0,
            capability_family="web_scan",
            archived_evidence_count=2,
            artifact_kinds_present=("pcap",),
            artifact_kind_allowlist=("stdout", "stderr"),
            estimated_prompt_tokens=250,
            max_prompt_tokens=2000,
            estimated_cost_usd=0.01,
            max_cost_usd=0.10,
        )
    )
    assert decision.action == "no_signal"
    assert decision.reason == "no_allowlisted_artifact_signal"

def test_policy_skips_when_cost_pricing_is_unavailable() -> None:
    decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=0,
            native_observation_count=0,
            capability_family="web_scan",
            archived_evidence_count=2,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout",),
            estimated_prompt_tokens=250,
            max_prompt_tokens=2000,
            estimated_cost_usd=0.0,
            max_cost_usd=0.10,
            pricing_status="unavailable",
        )
    )

    assert decision.action == "skip"
    assert decision.reason == "cost_pricing_unavailable"

def test_candidate_usage_recording_preserves_provider_identity() -> None:
    captured = {}

    class _UsageService:
        def record_usage(self, **kwargs):
            captured.update(kwargs)

    record_candidate_usage_if_task_present(
        task_id=10,
        usage_summary={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_surface": "messages",
            "pricing_status": "unavailable",
            "provider_usage_components": {
                "provider": "anthropic",
                "api_surface": "messages",
                "components": {
                    "input_tokens": 90,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 50,
                },
            },
        },
        source_label="knowledge_candidate_extractor",
        source_execution_id="exec-1",
        ingestion_run_id="run-1",
        resolve_task_user_id=lambda task_id: 99,
        usage_tracking_service_factory=_UsageService,
    )

    usage = captured["usage"]
    assert usage.provider == "anthropic"
    assert usage.model == "claude-sonnet-4-6"
    assert usage.api_surface == "messages"
    assert usage.provider_usage_components is not None
    assert captured["metadata"]["pricing_status"] == "unavailable"


def test_post_tool_candidate_policy_skips_unavailable_pricing(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")

    result = maybe_run_candidate_extraction(
        run=SimpleNamespace(
            run_metadata={},
            extractor_family="deterministic",
            extractor_version="1.0",
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-1",
            id="run-1",
            user_id=99,
        ),
        execution_payload={"execution": {"tool_name": "nmap"}},
        archived_rows=[
            SimpleNamespace(
                id="archive-1",
                archive_metadata={"artifact_kind": "stdout"},
                mime_type=None,
                storage_mode="inline",
            )
        ],
        deterministic_observations=[],
        extraction_stats={},
        post_tool_candidate_payload={
            "candidate_observations": [
                {
                    "observation_type": "finding.vulnerability_detected",
                    "subject_type": "finding.instance",
                    "subject_key_hint": "cve-2024-0001:host.ip:10.0.0.1",
                    "evidence_refs": [
                        {
                            "evidence_archive_id": "archive-1",
                            "excerpt": "marker",
                        }
                    ],
                }
            ],
        },
        post_tool_candidate_usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "estimated_cost_usd": 0.0,
            "pricing_status": "unavailable",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_surface": "messages",
        },
        candidate_extractor_family="llm.candidate_extraction",
        candidate_extractor_version="1.0",
        candidate_extraction_mode="candidate_fallback",
    )

    assert result.status == "skipped"
    assert result.policy_decision is not None
    assert result.policy_decision.reason == "cost_pricing_unavailable"


def test_post_tool_candidate_payload_masks_durable_candidate_fields(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    raw_secret = "post-tool-candidate-secret-123"

    result = maybe_run_candidate_extraction(
        run=SimpleNamespace(
            run_metadata={},
            extractor_family="deterministic",
            extractor_version="1.0",
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-post-tool-mask",
            id="run-post-tool-mask",
            user_id=99,
        ),
        execution_payload={"execution": {"tool_name": "nmap"}},
        archived_rows=[
            SimpleNamespace(
                id="archive-post-tool-mask",
                source_artifact_id="artifact-post-tool-mask",
                lineage_snapshot={"artifact_id": "artifact-post-tool-mask"},
                archive_metadata={"artifact_kind": "stdout"},
                mime_type=None,
                storage_mode="inline",
            )
        ],
        deterministic_observations=[],
        extraction_stats={},
        post_tool_candidate_payload={
            "candidate_observations": [
                {
                    "observation_type": "network.service_observed",
                    "subject_type": "service.socket",
                    "subject_key_hint": "10.0.0.1/tcp/443",
                    "confidence": 0.9,
                    "attributes": {
                        "service_name": "https",
                        "password": raw_secret,
                    },
                    "rationale": f"Service leaked password={raw_secret}",
                    "evidence_refs": [
                        {
                            "source_artifact_id": "artifact-post-tool-mask",
                            "excerpt": f"Authorization: Bearer {raw_secret}",
                        }
                    ],
                }
            ],
            "analyst_notes": [
                {"note": f"Follow up on token={raw_secret} for login reuse."}
            ],
        },
        post_tool_candidate_usage=None,
        candidate_extractor_family="llm.candidate_extraction",
        candidate_extractor_version="1.0",
        candidate_extraction_mode="candidate_fallback",
    )

    assert result.status == "succeeded"
    durable_text = repr(
        {
            "observations": result.observations,
            "analyst_notes": result.analyst_notes,
        }
    )
    assert raw_secret not in durable_text
    assert "<DURABLE_SECRET_MASK:" in durable_text
    assert "https" in durable_text


def test_policy_returns_skip_when_capability_family_is_not_allowlisted() -> None:
    decision = KnowledgeCandidateExtractionPolicy.evaluate(
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=0,
            native_observation_count=0,
            capability_family="filesystem",
            archived_evidence_count=2,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout", "stderr"),
            capability_family_allowlist=("web_scan", "network_scan"),
            estimated_prompt_tokens=200,
            max_prompt_tokens=2000,
            estimated_cost_usd=0.01,
            max_cost_usd=0.10,
        )
    )
    assert decision.action == "skip"
    assert decision.reason == "capability_family_not_allowlisted"

def test_policy_request_contract_carries_finding_level_counts() -> None:
    request = CandidateExtractionPolicyRequest(
        deterministic_observation_count=5,
        native_observation_count=0,
        observation_count_finding_total=2,
        observation_count_finding_authoritative=1,
        observation_count_non_finding_total=3,
        capability_family="web_scan",
        archived_evidence_count=2,
        artifact_kinds_present=("stdout",),
        artifact_kind_allowlist=("stdout",),
        estimated_prompt_tokens=250,
        max_prompt_tokens=2000,
        estimated_cost_usd=0.01,
        max_cost_usd=0.10,
    )

    assert request.observation_count_finding_total == 2
    assert request.observation_count_finding_authoritative == 1
    assert request.observation_count_non_finding_total == 3

def test_policy_request_contract_rejects_invalid_finding_level_counts() -> None:
    with pytest.raises(ValueError, match="cannot exceed observation_count_finding_total"):
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=2,
            native_observation_count=0,
            observation_count_finding_total=1,
            observation_count_finding_authoritative=2,
            observation_count_non_finding_total=0,
            capability_family="web_scan",
            archived_evidence_count=1,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout",),
        )

    with pytest.raises(ValueError, match="cannot exceed deterministic_observation_count"):
        CandidateExtractionPolicyRequest(
            deterministic_observation_count=1,
            native_observation_count=0,
            observation_count_finding_total=1,
            observation_count_finding_authoritative=1,
            observation_count_non_finding_total=1,
            capability_family="web_scan",
            archived_evidence_count=1,
            artifact_kinds_present=("stdout",),
            artifact_kind_allowlist=("stdout",),
        )
