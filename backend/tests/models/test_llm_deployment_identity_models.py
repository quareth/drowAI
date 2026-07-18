"""ORM contract tests for deployment-aware text LLM identity tables."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from backend.models.llm import (
    LLMCapabilityObservation,
    LLMConversation,
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    LLMUsageRecord,
    UserEmbeddingSelection,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
)


def _column_names(model: type) -> set[str]:
    """Return declared table column names for one ORM model."""

    return set(model.__table__.columns.keys())


def test_connection_is_user_owned_revisioned_and_state_constrained() -> None:
    """Connections persist owner, policy configuration, state, and revision."""

    columns = _column_names(LLMInferenceConnection)
    assert {
        "id",
        "user_id",
        "display_name",
        "connection_preset_id",
        "runtime_family_id",
        "serving_operator_id",
        "transport_origin",
        "endpoint_url",
        "endpoint_policy_id",
        "config_schema_version",
        "non_secret_config",
        "state",
        "revision",
        "legacy_default_provider",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert LLMInferenceConnection.__table__.c.user_id.foreign_keys
    assert LLMInferenceConnection.__table__.c.state.default.arg == "draft"
    assert LLMInferenceConnection.__table__.c.revision.default.arg == 1

    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in LLMInferenceConnection.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "draft" in check_sql
    assert "disabled" in check_sql
    assert "enabled" in check_sql
    assert "revision > 0" in check_sql

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in LLMInferenceConnection.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("user_id", "legacy_default_provider") in unique_columns


def test_deployment_preserves_exact_wire_model_identity() -> None:
    """ORM construction does not normalize case-sensitive wire model IDs."""

    deployment = LLMModelDeployment(
        connection_id="00000000-0000-0000-0000-000000000001",
        wire_model_id="Org/Model-Case:Exact",
        display_name="Exact Model",
        discovery_source="operator",
    )

    assert deployment.wire_model_id == "Org/Model-Case:Exact"
    assert {
        "connection_id",
        "wire_model_id",
        "canonical_model_id",
        "display_name",
        "discovery_source",
        "source_metadata",
        "lifecycle_state",
        "availability_state",
        "enabled",
        "revision",
    }.issubset(_column_names(LLMModelDeployment))


def test_routes_reference_registered_protocol_contracts() -> None:
    """Routes persist adapter, API surface, and dialect policy identifiers."""

    assert {
        "deployment_id",
        "adapter_id",
        "adapter_version",
        "api_surface",
        "dialect_policy_id",
        "billing_provider_id",
        "route_config",
        "enabled",
    }.issubset(_column_names(LLMDeploymentRoute))


def test_capability_observation_carries_evidence_lifecycle() -> None:
    """Capability evidence includes support, constraints, source, and expiry."""

    assert {
        "deployment_id",
        "route_id",
        "capability",
        "support_state",
        "constraints",
        "source",
        "observed_at",
        "expires_at",
        "revision",
        "fingerprint",
    }.issubset(_column_names(LLMCapabilityObservation))

    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in LLMCapabilityObservation.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "supported" in check_sql
    assert "unsupported" in check_sql
    assert "unknown" in check_sql


def test_text_llm_rows_gain_nullable_identity_refs_but_embeddings_do_not() -> None:
    """Deployment identity extends text workloads without changing embeddings."""

    assert "deployment_id" in _column_names(UserLLMSelection)
    assert "deployment_id" in _column_names(UserReportingLLMSelection)
    assert {
        "gate_deployment_id",
        "extraction_deployment_id",
    }.issubset(_column_names(UserMemoryLLMSelection))
    for model in (LLMConversation, LLMUsageRecord):
        assert {"connection_id", "deployment_id", "route_id"}.issubset(
            _column_names(model)
        )
    assert {
        "origin_revision",
        "origin_deployment_revision",
        "remote_resource_id",
    }.issubset(_column_names(LLMConversation))

    embedding_columns = _column_names(UserEmbeddingSelection)
    assert embedding_columns == {
        "id",
        "user_id",
        "provider",
        "model",
        "dimensions",
        "vector_family",
        "created_at",
        "updated_at",
    }
    assert not {
        "connection_id",
        "deployment_id",
        "route_id",
    }.intersection(embedding_columns)
