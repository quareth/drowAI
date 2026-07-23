"""Compose validated runtime model profiles for persisted LLM deployments.

The service keeps the curated profile registry authoritative and validates that
persisted route metadata cannot select a different executable adapter surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.adapters.openai.compatible_chat import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
)
from agent.providers.llm.adapters.openai.compatible_dialects import (
    resolve_openai_compatible_dialect,
)
from agent.providers.llm.adapters.openai.compatible_request_policies import (
    DEFAULT_COMPATIBLE_REQUEST_POLICY_ID,
)
from agent.providers.llm.core.capabilities import CapabilityInput, LLMCapability, freeze_capabilities
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile
from backend.models import (
    LLMCapabilityObservation,
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
)

from .operation_registry import (
    GPT_OSS_20B_PROVING_PRESET_ID,
    ConnectionOperationRegistry,
    ProvingConnectionPreset,
)
from .types import LLMDeploymentValidationError


@dataclass(frozen=True, slots=True)
class NativeRouteContract:
    """Code-owned adapter metadata for one native provider API surface."""

    adapter_id: str
    adapter_version: str
    api_surface: str
    dialect_policy_id: str


@dataclass(frozen=True, slots=True)
class CapabilityRunnability:
    """Deployment capability runnability decision from observed evidence."""

    runnable: bool
    status: str
    missing_capabilities: tuple[str, ...] = ()


_NATIVE_ROUTES = {
    ("openai", "responses"): NativeRouteContract(
        adapter_id="openai_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
    ),
    ("openai", "chat_completions"): NativeRouteContract(
        adapter_id="openai_chat",
        adapter_version="1",
        api_surface="chat_completions",
        dialect_policy_id="openai_chat.native_v1",
    ),
    ("anthropic", "messages"): NativeRouteContract(
        adapter_id="anthropic_messages",
        adapter_version="1",
        api_surface="messages",
        dialect_policy_id="anthropic_messages.native_v1",
    ),
}


class EffectiveProfileService:
    """Resolve curated model limits and verify persisted native route identity."""

    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    def resolve(
        self,
        *,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
    ) -> ModelProfile:
        """Return the effective profile or fail closed on route/profile drift."""

        if route is not None and route.adapter_id == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID:
            return _compatible_profile(
                connection=connection,
                deployment=deployment,
                route=route,
            )
        profile = require_model_profile(_profile_ref(connection, deployment))
        contract = self._route_contract(connection, profile)
        if route is not None and (
            route.adapter_id != contract.adapter_id
            or route.adapter_version != contract.adapter_version
            or route.api_surface != contract.api_surface
            or route.dialect_policy_id != contract.dialect_policy_id
        ):
            raise LLMDeploymentValidationError(
                "Deployment route does not match its registered adapter profile"
            )
        return profile

    def classify_runnability(
        self,
        *,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
        required_capabilities: tuple[CapabilityInput, ...],
        connection_id: str | None = None,
        connection_revision: int | None = None,
        credential_fingerprint: str | None = None,
    ) -> CapabilityRunnability:
        """Return whether route-scoped observations satisfy required capabilities."""

        if self._db is None:
            raise LLMDeploymentValidationError("Capability observation store is unavailable")
        required = freeze_capabilities(required_capabilities)
        supported = _supported_observations(
            self._db,
            deployment_id=deployment.id,
            route_id=route.id if route is not None else None,
            connection_id=connection_id,
            connection_revision=connection_revision,
            credential_fingerprint=credential_fingerprint,
        )
        missing = tuple(
            capability.value
            for capability in sorted(required, key=lambda item: item.value)
            if capability.value not in supported
        )
        if missing:
            return CapabilityRunnability(
                runnable=False,
                status="capability_unknown",
                missing_capabilities=missing,
            )
        return CapabilityRunnability(runnable=True, status="runnable")

    @staticmethod
    def native_route_contract(profile: ModelProfile) -> NativeRouteContract:
        """Return code-owned native route metadata for a curated profile."""

        contract = _NATIVE_ROUTES.get((profile.ref.provider, profile.api_surface))
        if contract is None:
            raise LLMDeploymentValidationError(
                "Deployment profile has no registered native adapter route"
            )
        return contract

    @staticmethod
    def _route_contract(
        connection: LLMInferenceConnection,
        profile: ModelProfile,
    ) -> NativeRouteContract:
        try:
            preset = ConnectionOperationRegistry().get_connection_preset(
                connection.connection_preset_id
            )
        except Exception:
            preset = None
        if preset is not None and preset.adapter_id == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID:
            return NativeRouteContract(
                adapter_id=preset.adapter_id,
                adapter_version=preset.adapter_version,
                api_surface=preset.api_surface,
                dialect_policy_id=preset.dialect_policy_id,
            )
        return EffectiveProfileService.native_route_contract(profile)


def _profile_ref(
    connection: LLMInferenceConnection,
    deployment: LLMModelDeployment,
) -> ProviderModelRef:
    canonical_model = deployment.canonical_model_id or deployment.wire_model_id
    if (
        connection.connection_preset_id == GPT_OSS_20B_PROVING_PRESET_ID
        and canonical_model == "openai/gpt-oss-20b"
    ):
        return ProviderModelRef("openai", "gpt-oss-20b")
    return ProviderModelRef(connection.connection_preset_id, canonical_model)


def _compatible_profile(
    *,
    connection: LLMInferenceConnection,
    deployment: LLMModelDeployment,
    route: LLMDeploymentRoute,
) -> ModelProfile:
    """Build a runtime-only profile for reviewed compatible deployments."""

    registry = ConnectionOperationRegistry()
    preset = registry.get_connection_preset(connection.connection_preset_id)
    if (
        route.adapter_id != preset.adapter_id
        or route.adapter_version != preset.adapter_version
        or route.api_surface != preset.api_surface
        or route.dialect_policy_id != preset.dialect_policy_id
    ):
        raise LLMDeploymentValidationError(
            "Deployment route does not match its registered adapter profile"
        )
    route_config = route.route_config if isinstance(route.route_config, dict) else {}
    route_request_policy = str(
        route_config.get(
            "request_policy_id",
            DEFAULT_COMPATIBLE_REQUEST_POLICY_ID,
        )
    )
    if route_request_policy != preset.request_policy_id:
        raise LLMDeploymentValidationError(
            "Deployment route does not match its registered request policy"
        )
    return compose_reviewed_compatible_profile(
        preset,
        display_name=deployment.display_name,
        canonical_model_id=deployment.canonical_model_id or preset.canonical_model_id,
        lifecycle=deployment.lifecycle_state,
    )


def compose_reviewed_compatible_profile(
    preset: ProvingConnectionPreset,
    *,
    display_name: str,
    canonical_model_id: str,
    lifecycle: str,
) -> ModelProfile:
    """Intersect a curated model profile with one reviewed route preset."""

    canonical_profile = require_model_profile(preset.canonical_ref)
    dialect = resolve_openai_compatible_dialect(preset.dialect_policy_id)
    capabilities = (
        canonical_profile.capabilities
        & preset.capability_ceiling
        & dialect.capabilities
    )
    reasoning_efforts = (
        canonical_profile.reasoning_efforts & dialect.reasoning_efforts
    )
    return ModelProfile(
        ref=canonical_profile.ref,
        display_name=display_name,
        api_surface=preset.api_surface,
        capabilities=capabilities,
        context_window_tokens=canonical_profile.context_window_tokens,
        max_output_tokens=canonical_profile.max_output_tokens,
        listable=False,
        canonical_model_id=canonical_model_id,
        lifecycle=lifecycle,
        support_tier="deployment",
        tool_choice_modes=(
            canonical_profile.tool_choice_modes & dialect.tool_choice_modes
        ),
        structured_output_strategies=(
            canonical_profile.structured_output_strategies
            & dialect.structured_output_strategies
        ),
        reasoning_efforts=reasoning_efforts,
        default_reasoning_effort=(
            canonical_profile.default_reasoning_effort
            if canonical_profile.default_reasoning_effort in reasoning_efforts
            else None
        ),
        pricing_schedule_ref=canonical_profile.pricing_schedule_ref,
        pricing_provenance=canonical_profile.pricing_provenance,
    )


def _supported_observations(
    db: Session,
    *,
    deployment_id,
    route_id,
    connection_id: str | None = None,
    connection_revision: int | None = None,
    credential_fingerprint: str | None = None,
) -> frozenset[str]:
    rows = db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment_id,
            LLMCapabilityObservation.route_id == route_id,
        )
    ).scalars()
    supported: set[str] = set()
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            capability = LLMCapability(str(row.capability))
        except ValueError:
            continue
        if _is_expired(row.expires_at, now):
            continue
        if not _constraints_match(
            row.constraints,
            connection_id=connection_id,
            connection_revision=connection_revision,
            credential_fingerprint=credential_fingerprint,
        ):
            continue
        if row.support_state == "supported":
            supported.add(capability.value)
    return frozenset(supported)


def _is_expired(expires_at: Any, now: datetime) -> bool:
    """Return whether an observation is outside its freshness window."""

    if expires_at is None:
        return False
    if not isinstance(expires_at, datetime):
        return True
    comparable = expires_at
    if comparable.tzinfo is None:
        comparable = comparable.replace(tzinfo=timezone.utc)
    return comparable <= now


def _constraints_match(
    constraints: Any,
    *,
    connection_id: str | None,
    connection_revision: int | None,
    credential_fingerprint: str | None,
) -> bool:
    """Match optional proving provenance constraints when requested."""

    if (
        connection_id is None
        and connection_revision is None
        and credential_fingerprint is None
    ):
        return True
    if not isinstance(constraints, dict):
        return False
    if connection_id is not None and str(constraints.get("connection_id")) != str(
        connection_id
    ):
        return False
    if connection_revision is not None:
        try:
            observed_revision = int(constraints.get("connection_revision"))
        except (TypeError, ValueError):
            return False
        if observed_revision != int(connection_revision):
            return False
    if credential_fingerprint is not None and str(
        constraints.get("credential_fingerprint")
    ) != str(credential_fingerprint):
        return False
    return True


__all__ = ["CapabilityRunnability", "EffectiveProfileService", "NativeRouteContract"]
