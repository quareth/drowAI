"""Direct tests for transport-neutral LLM application outcome contracts.

These tests lock immutable, value-comparable, secret-free handoff types used by
future LLM application services. They do not exercise HTTP schemas, persistence,
provider transport, or router behavior.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields
from datetime import datetime, timezone
from typing import Any, get_args, get_type_hints

import pytest

from backend.services.llm_provider.application_contracts import (
    CatalogModelOutcome,
    CatalogOutcome,
    CatalogProviderOutcome,
    ConnectionCatalogMetadataOutcome,
    ConnectionConfigFieldOutcome,
    ConnectionRefOutcome,
    ConnectionStatusOutcome,
    DeploymentRefOutcome,
    MaskedCredentialStatusOutcome,
    ProvingCatalogMetadataOutcome,
    RunnabilityOutcome,
    VerificationOutcome,
    VerificationUsageOutcome,
)


_OUTCOME_TYPES = (
    ConnectionRefOutcome,
    DeploymentRefOutcome,
    VerificationUsageOutcome,
    VerificationOutcome,
    RunnabilityOutcome,
    ConnectionStatusOutcome,
    MaskedCredentialStatusOutcome,
    ConnectionConfigFieldOutcome,
    ConnectionCatalogMetadataOutcome,
    ProvingCatalogMetadataOutcome,
    CatalogModelOutcome,
    CatalogProviderOutcome,
    CatalogOutcome,
)


def _sample_outcomes() -> tuple[ConnectionStatusOutcome, CatalogOutcome]:
    observed_at = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    connection_ref = ConnectionRefOutcome(
        connection_id="connection-1",
        expected_revision=3,
    )
    deployment_ref = DeploymentRefOutcome(
        deployment_id="deployment-1",
        expected_revision=5,
    )
    verification = VerificationOutcome(
        status="passed",
        code="verified",
        message="Endpoint verified",
        retryable=False,
        observed_at=observed_at,
        expires_at=None,
        model_present=True,
        usage=VerificationUsageOutcome(
            prompt_tokens=4,
            completion_tokens=6,
            total_tokens=10,
        ),
    )
    runnability = RunnabilityOutcome(
        status="runnable",
        selectable=True,
        runnable=True,
        reason=None,
    )
    status = ConnectionStatusOutcome(
        lifecycle_state="enabled",
        connection_ref=connection_ref,
        deployment_ref=deployment_ref,
        verification=verification,
        runnability=runnability,
    )
    connection_metadata = ConnectionCatalogMetadataOutcome(
        preset_id="reviewed-preset",
        display_name="Reviewed preset",
        enabled=True,
        auth_mode="bearer",
        user_config_fields=("api_key", "base_url"),
        lifecycle_state="enabled",
        config_fields=(
            ConnectionConfigFieldOutcome(
                name="api_key",
                label="API key",
                field_type="password",
                required=True,
                secret=True,
            ),
        ),
        connection_ref=connection_ref,
        deployment_ref=deployment_ref,
        verification=verification,
        runnability=runnability,
    )
    proving_metadata = ProvingCatalogMetadataOutcome(
        preset_id="proving-preset",
        display_name="Proving preset",
        enabled=True,
        auth_mode="bearer_api_key",
        user_config_fields=("display_label", "api_key"),
        lifecycle_state="enabled",
        connection_ref=connection_ref,
        deployment_ref=deployment_ref,
        verification=verification,
        runnability=runnability,
    )
    model = CatalogModelOutcome(
        id="wire-model",
        canonical_model_id="owner/model",
        exact_wire_model_id="wire-model",
        label="Model",
        api_surface="chat_completions",
        capabilities=("chat", "tools"),
        context_window_tokens=128_000,
        max_output_tokens=10_000,
        reasoning_efforts=("low", "high"),
        visible_reasoning_efforts=("low", "high"),
        default_reasoning_effort="low",
        default_visible_reasoning_effort="low",
        tool_choice_modes=("auto",),
        structured_output_strategies=(),
        pricing_status="unavailable",
        deployment_ref=deployment_ref,
        runnable=True,
        connection=connection_metadata,
        proving=proving_metadata,
    )
    provider = CatalogProviderOutcome(
        id="provider",
        label="Provider",
        capabilities=("chat", "tools"),
        available=True,
        selectable=True,
        credential=MaskedCredentialStatusOutcome(
            user_id=42,
            provider="provider",
            enabled=True,
            has_api_key=True,
            masked_api_key="***",
            connection_ref=connection_ref,
            auth_mode="bearer",
        ),
        models=(model,),
        default_model="wire-model",
    )
    return status, CatalogOutcome(providers=(provider,))


def _contains_unbounded_type(annotation: object) -> bool:
    if annotation in {Any, object}:
        return True
    return any(_contains_unbounded_type(arg) for arg in get_args(annotation))


def test_outcomes_are_frozen_slotted_and_deeply_read_only() -> None:
    """Nested contract values cannot be reassigned or extended in place."""

    status, catalog = _sample_outcomes()

    with pytest.raises(FrozenInstanceError):
        status.lifecycle_state = "draft"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        status.internal_row = object()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        catalog.providers.append(catalog.providers[0])  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        catalog.providers[0].models[0].capabilities.append("usage")  # type: ignore[attr-defined]


def test_outcomes_have_stable_field_values_and_value_equality() -> None:
    """Equivalent service results compare exactly across application boundaries."""

    status, catalog = _sample_outcomes()
    same_status, same_catalog = _sample_outcomes()

    assert status == same_status
    assert catalog == same_catalog
    assert status.verification is not None
    assert status.verification.usage == VerificationUsageOutcome(4, 6, 10)
    assert catalog.providers[0].models[0].connection is not None
    assert catalog.providers[0].models[0].connection.config_fields[0].secret is True
    assert catalog.providers[0].models[0].proving is not None
    assert catalog.providers[0].models[0].proving.preset_id == "proving-preset"


def test_verification_without_evidence_has_stable_empty_fields() -> None:
    """A not-tested result carries no accidental evidence or usage container."""

    outcome = VerificationOutcome(
        status="failed",
        code="not_tested",
        message="Verification has not run.",
        retryable=False,
    )

    assert outcome == VerificationOutcome(
        status="failed",
        code="not_tested",
        message="Verification has not run.",
        retryable=False,
        observed_at=None,
        expires_at=None,
        model_present=None,
        usage=None,
    )
    assert outcome.usage is None


def test_outcome_storage_surface_excludes_unbounded_or_secret_value_slots() -> None:
    """Contracts expose only typed public values, never generic object containers."""

    all_field_names: set[str] = set()
    for outcome_type in _OUTCOME_TYPES:
        assert "__dict__" not in outcome_type.__slots__
        hints = get_type_hints(outcome_type)
        assert set(hints) == {field.name for field in fields(outcome_type)}
        assert not any(_contains_unbounded_type(annotation) for annotation in hints.values())
        all_field_names.update(hints)

    assert all_field_names.isdisjoint(
        {
            "api_key",
            "credential_secret",
            "decrypted_secret",
            "encrypted_api_key",
            "orm_row",
            "provider_secret",
            "session",
            "transport",
        }
    )


def test_repr_and_dataclass_serialization_contain_only_public_masked_values() -> None:
    """Default repr and serialization contain refs and masks but no raw secret slot."""

    status, catalog = _sample_outcomes()
    rendered = f"{status!r}\n{catalog!r}\n{asdict(status)!r}\n{asdict(catalog)!r}"

    assert "connection-1" in rendered
    assert "deployment-1" in rendered
    assert "masked_api_key" in rendered
    assert "***" in rendered
    assert "raw-contract-secret" not in rendered
    assert "encrypted_api_key" not in rendered
    assert "provider_secret" not in rendered
