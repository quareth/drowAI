"""Transport-neutral outcomes for LLM catalog and connection applications.

This module owns immutable, non-secret values exchanged across the LLM
application boundary. It must not own HTTP schemas, persistence, provider
transport, credential material, logging, or application behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class ConnectionRefOutcome:
    """Opaque connection identity with its expected optimistic revision."""

    connection_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class DeploymentRefOutcome:
    """Opaque deployment identity with its expected optimistic revision."""

    deployment_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class VerificationUsageOutcome:
    """Provider-reported token usage from a sanitized verification probe."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Sanitized verification result, including optional evidence metadata."""

    status: str
    code: str
    message: str
    retryable: bool
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    model_present: bool | None = None
    usage: VerificationUsageOutcome | None = None


@dataclass(frozen=True, slots=True)
class RunnabilityOutcome:
    """Current selectability and runtime-readiness decision."""

    status: str
    selectable: bool
    runnable: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionStatusOutcome:
    """Managed or proving connection lifecycle status after one use case."""

    lifecycle_state: str
    connection_ref: ConnectionRefOutcome
    deployment_ref: DeploymentRefOutcome | None
    verification: VerificationOutcome | None
    runnability: RunnabilityOutcome | None


@dataclass(frozen=True, slots=True)
class MaskedCredentialStatusOutcome:
    """Catalog-safe credential availability without credential material."""

    user_id: int
    provider: str
    enabled: bool
    has_api_key: bool
    masked_api_key: Literal["***"] | None
    connection_ref: ConnectionRefOutcome | None
    auth_mode: str | None


@dataclass(frozen=True, slots=True)
class ConnectionConfigFieldOutcome:
    """Public metadata describing one connection configuration field."""

    name: str
    label: str
    field_type: str
    required: bool
    secret: bool


@dataclass(frozen=True, slots=True)
class ConnectionCatalogMetadataOutcome:
    """Reviewed connection-preset metadata attached to a catalog model."""

    preset_id: str
    display_name: str
    enabled: bool
    auth_mode: str
    user_config_fields: tuple[str, ...]
    lifecycle_state: str
    config_fields: tuple[ConnectionConfigFieldOutcome, ...]
    connection_ref: ConnectionRefOutcome | None
    deployment_ref: DeploymentRefOutcome | None
    verification: VerificationOutcome | None
    runnability: RunnabilityOutcome | None


@dataclass(frozen=True, slots=True)
class ProvingCatalogMetadataOutcome:
    """Proving-preset metadata attached to a compatible catalog model."""

    preset_id: str
    display_name: str
    enabled: bool
    auth_mode: str
    user_config_fields: tuple[str, ...]
    lifecycle_state: str
    connection_ref: ConnectionRefOutcome | None
    deployment_ref: DeploymentRefOutcome | None
    verification: VerificationOutcome | None
    runnability: RunnabilityOutcome | None


@dataclass(frozen=True, slots=True)
class CatalogModelOutcome:
    """Transport-neutral public facts for one catalog model."""

    id: str
    canonical_model_id: str
    exact_wire_model_id: str | None
    label: str
    api_surface: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    max_output_tokens: int
    reasoning_efforts: tuple[str, ...]
    visible_reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    default_visible_reasoning_effort: str | None
    tool_choice_modes: tuple[str, ...]
    structured_output_strategies: tuple[str, ...]
    pricing_status: str
    deployment_ref: DeploymentRefOutcome | None
    runnable: bool
    connection: ConnectionCatalogMetadataOutcome | None
    proving: ProvingCatalogMetadataOutcome | None


@dataclass(frozen=True, slots=True)
class CatalogProviderOutcome:
    """Transport-neutral public facts for one ordered catalog provider."""

    id: str
    label: str
    capabilities: tuple[str, ...]
    available: bool
    selectable: bool
    credential: MaskedCredentialStatusOutcome
    models: tuple[CatalogModelOutcome, ...]
    default_model: str


@dataclass(frozen=True, slots=True)
class CatalogOutcome:
    """Complete ordered model-catalog result for one application request."""

    providers: tuple[CatalogProviderOutcome, ...]


__all__ = [
    "CatalogModelOutcome",
    "CatalogOutcome",
    "CatalogProviderOutcome",
    "ConnectionCatalogMetadataOutcome",
    "ConnectionConfigFieldOutcome",
    "ConnectionRefOutcome",
    "ConnectionStatusOutcome",
    "DeploymentRefOutcome",
    "MaskedCredentialStatusOutcome",
    "ProvingCatalogMetadataOutcome",
    "RunnabilityOutcome",
    "VerificationOutcome",
    "VerificationUsageOutcome",
]
