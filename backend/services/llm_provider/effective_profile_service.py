"""Compose validated runtime model profiles for persisted LLM deployments.

The service keeps the curated profile registry authoritative and validates that
persisted route metadata cannot select a different executable adapter surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
)

from .types import LLMDeploymentValidationError


@dataclass(frozen=True, slots=True)
class NativeRouteContract:
    """Code-owned adapter metadata for one native provider API surface."""

    adapter_id: str
    adapter_version: str
    api_surface: str
    dialect_policy_id: str


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

    def resolve(
        self,
        *,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
    ) -> ModelProfile:
        """Return the effective profile or fail closed on route/profile drift."""

        canonical_model = deployment.canonical_model_id or deployment.wire_model_id
        profile = require_model_profile(
            ProviderModelRef(connection.connection_preset_id, canonical_model)
        )
        contract = self.native_route_contract(profile)
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

    @staticmethod
    def native_route_contract(profile: ModelProfile) -> NativeRouteContract:
        """Return code-owned native route metadata for a curated profile."""

        contract = _NATIVE_ROUTES.get((profile.ref.provider, profile.api_surface))
        if contract is None:
            raise LLMDeploymentValidationError(
                "Deployment profile has no registered native adapter route"
            )
        return contract


__all__ = ["EffectiveProfileService", "NativeRouteContract"]
