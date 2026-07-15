"""Tests for retention executor/result contracts and safe serialization."""

from __future__ import annotations

from dataclasses import fields, is_dataclass

import pytest

from backend.services.retention import contracts


def test_retention_classes_match_roadmap_vocabulary() -> None:
    assert contracts.RETENTION_CLASSES == (
        "operational_ephemeral",
        "runtime_resume_state",
        "task_record",
        "task_transcript",
        "artifact_payload",
        "execution_provenance",
        "engagement_knowledge",
        "semantic_memory",
        "reporting",
        "usage_accounting",
    )


def test_dry_run_and_apply_results_share_same_safe_schema() -> None:
    dry_run = _build_result(mode=contracts.RETENTION_RUN_MODE_DRY_RUN)
    apply = _build_result(mode=contracts.RETENTION_RUN_MODE_APPLY)

    dry_run_summary = dry_run.to_safe_dict()
    apply_summary = apply.to_safe_dict()

    assert set(dry_run_summary) == set(apply_summary)
    assert set(dry_run_summary["counts"]) == set(apply_summary["counts"])
    assert set(dry_run_summary["decisions"][0]) == set(apply_summary["decisions"][0])


def test_contract_supports_tenant_and_all_tenant_request_scopes() -> None:
    tenant_request = contracts.RetentionRunRequest(
        mode=contracts.RETENTION_RUN_MODE_DRY_RUN,
        scope=contracts.RETENTION_SCOPE_TENANT,
        tenant_id=42,
        limit_per_tenant=100,
    )
    all_tenant_request = contracts.RetentionRunRequest(
        mode=contracts.RETENTION_RUN_MODE_APPLY,
        scope=contracts.RETENTION_SCOPE_ALL_TENANTS,
        tenant_id=None,
        limit_per_tenant=100,
    )

    assert tenant_request.tenant_id == 42
    assert all_tenant_request.scope == contracts.RETENTION_SCOPE_ALL_TENANTS


def test_tenant_scoped_request_requires_tenant_id() -> None:
    with pytest.raises(ValueError, match="tenant_id is required"):
        contracts.RetentionRunRequest(
            mode=contracts.RETENTION_RUN_MODE_DRY_RUN,
            scope=contracts.RETENTION_SCOPE_TENANT,
            tenant_id=None,
        )


def test_safe_serialization_rejects_unsafe_fields_recursively() -> None:
    with pytest.raises(ValueError, match="unsafe retention summary field"):
        contracts.to_safe_dict({"safe": {"raw_payload": "not allowed"}})

    with pytest.raises(ValueError, match="unsafe retention summary field"):
        contracts.to_safe_dict({"safe": [{"object-key": "not allowed"}]})

    with pytest.raises(ValueError, match="unsafe retention summary field"):
        contracts.to_safe_dict({"secret_value": "not allowed"})


def test_safe_serialization_allows_canonical_class_terms_in_reason_codes() -> None:
    summary = contracts.to_safe_dict(
        {
            "reason_counts": {
                "durable_evidence_retained_runtime_artifact_payload_deleted": 1,
                "terminal_task_transcript_retention_expired": 1,
            }
        }
    )

    assert summary["reason_counts"] == {
        "durable_evidence_retained_runtime_artifact_payload_deleted": 1,
        "terminal_task_transcript_retention_expired": 1,
    }


def test_contract_dataclass_fields_do_not_expose_unsafe_field_names() -> None:
    contract_dataclasses = (
        contracts.RetentionRunRequest,
        contracts.RetentionDecision,
        contracts.RetentionBatchCounts,
        contracts.RetentionExecutorResult,
        contracts.RetentionRunResult,
    )

    for contract in contract_dataclasses:
        assert is_dataclass(contract)
        for field_info in fields(contract):
            contracts.validate_safe_field_names({field_info.name: None})


def test_reason_codes_are_normalized_safe_identifiers() -> None:
    assert (
        contracts.normalize_reason_code("Operational_Log_Retention_Expired")
        == "operational_log_retention_expired"
    )
    with pytest.raises(ValueError, match="invalid retention reason code"):
        contracts.normalize_reason_code("contains spaces")


def _build_result(mode: str) -> contracts.RetentionExecutorResult:
    return contracts.RetentionExecutorResult(
        executor_name="knowledge.retention",
        retention_class=contracts.RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        mode=mode,
        tenant_id=1,
        counts=contracts.RetentionBatchCounts(
            scanned_count=3,
            candidate_count=2,
            protected_count=1,
            applied_count=0 if mode == contracts.RETENTION_RUN_MODE_DRY_RUN else 2,
            batch_count=2,
            batch_limit=100,
        ),
        reason_counts={"operational_log_retention_expired": 2},
        decisions=(
            contracts.RetentionDecision(
                retention_class=contracts.RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                outcome=contracts.RETENTION_DECISION_CANDIDATE,
                reason_code="operational_log_retention_expired",
                resource_id="agent_log:1",
            ),
        ),
    )
