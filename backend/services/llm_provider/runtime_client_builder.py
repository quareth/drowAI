"""Build turn-local LLM clients from already resolved runtime targets.

Purpose: own provider/model selection, reasoning validation, guarded transport
adaptation, factory invocation, short-lived secret validation, and budget
wrapping for a resolved target. Boundary: this module must not resolve
deployments, authorize users, touch persistence models, import the runtime
facade, or serialize/cache provider secrets.
"""

from __future__ import annotations

from typing import Any

from agent.providers.llm.adapters.openai.compatible_dialects import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
)
from agent.providers.llm.core.base import LLMClient
from agent.providers.llm.core.budget_enforcing_client import BudgetEnforcingLLMClient
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile
from core.llm.role_policy import RoleCallSettings

from .guarded_transport import GuardedAsyncInferenceTransport
from .types import (
    LLMCallTarget,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    RegisteredLLMOperationTarget,
    ResolvedLLMTarget,
)


class LLMRuntimeClientBuilder:
    """Construct provider clients after selection resolution has completed."""

    def resolve_supported_reasoning_effort(
        self,
        call_ref: ProviderModelRef,
        reasoning_effort: Any,
        *,
        model_profile: ModelProfile | None = None,
    ) -> str | None:
        """Return a reasoning effort only when the target model supports that option."""

        if reasoning_effort is None:
            return None

        profile = model_profile or require_model_profile(call_ref)
        if profile.supports(LLMCapability.REASONING_EFFORT):
            normalized_effort = str(reasoning_effort).strip().lower()
            if profile.reasoning_efforts and normalized_effort not in profile.reasoning_efforts:
                allowed = "|".join(sorted(profile.reasoning_efforts))
                raise LLMCapabilityNotSupportedError(
                    (
                        f"Model '{call_ref}' does not support reasoning_effort "
                        f"'{reasoning_effort}'. Allowed values: {allowed}."
                    ),
                    provider=call_ref.provider,
                    capability=LLMCapability.REASONING_EFFORT.value,
                )
            return normalized_effort

        raise LLMCapabilityNotSupportedError(
            f"Model '{call_ref}' does not support reasoning_effort",
            provider=call_ref.provider,
            capability=LLMCapability.REASONING_EFFORT.value,
        )

    def build(
        self,
        *,
        selection: LLMRuntimeSelection | LLMRuntimeSelectionV2,
        resolved_target: ResolvedLLMTarget,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None,
        legacy_call_ref: ProviderModelRef | None,
        legacy_reasoning_effort: str | None,
        reasoning_effort: Any,
        reasoning_effort_was_explicit: bool,
        client_kwargs: dict[str, Any],
    ) -> LLMClient:
        """Create a provider adapter and apply the existing budget wrapper policy."""

        factory_provider = (
            resolved_target.effective_profile.ref.provider
            if resolved_target.effective_profile is not None
            else resolved_target.connection.connection_preset_id
        )
        call_ref = ProviderModelRef(
            factory_provider,
            resolved_target.exact_wire_model_id,
        )
        supported_reasoning_effort = legacy_reasoning_effort
        if legacy_call_ref is None:
            if resolved_target.effective_profile is None:
                raise LLMConfigurationError(
                    "Deployment effective profile is unavailable"
                )
            supported_reasoning_effort = self.resolve_supported_reasoning_effort(
                call_ref,
                reasoning_effort,
                model_profile=resolved_target.effective_profile,
            )
        if supported_reasoning_effort is not None:
            client_kwargs["reasoning_effort"] = supported_reasoning_effort
        elif reasoning_effort_was_explicit:
            client_kwargs.pop("reasoning_effort", None)
        secret = resolved_target.connection.resolved_auth.secret
        if secret is None:
            raise LLMConfigurationError(
                "Selected adapter does not support unauthenticated construction",
                provider=call_ref.provider,
            )
        factory_kwargs = dict(client_kwargs)
        if (
            resolved_target.effective_profile is not None
            and (
                isinstance(selection, LLMRuntimeSelectionV2)
                or call_ref.normalized()
                != resolved_target.effective_profile.ref.normalized()
            )
        ):
            factory_kwargs["model_profile"] = resolved_target.effective_profile
        factory_kwargs["base_url"] = (
            resolved_target.connection.operation_target.client_base_url
        )
        factory_kwargs["wire_model_id"] = resolved_target.exact_wire_model_id
        factory_kwargs["dialect_policy_id"] = resolved_target.dialect_policy_id
        factory_kwargs["request_policy_id"] = resolved_target.request_policy_id
        if resolved_target.adapter_id == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID:
            factory_kwargs["adapter_id"] = resolved_target.adapter_id
        factory_kwargs["inference_transport"] = guarded_inference_transport(
            operation_target=resolved_target.connection.operation_target,
            secret=secret,
        )
        client = LLMClientFactory.get_client(
            provider_model=call_ref,
            api_key=secret.value,
            **factory_kwargs,
        )
        if resolved_target.effective_profile is None:
            return client
        return BudgetEnforcingLLMClient(
            client,
            provider_model=call_ref,
            role=resolve_budget_role(target=target, client_kwargs=client_kwargs),
            model_profile=resolved_target.effective_profile,
        )


def resolve_budget_role(
    *,
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None,
    client_kwargs: dict[str, Any],
) -> str:
    if isinstance(target, LLMCallTarget) and target.role:
        return target.role
    role = client_kwargs.get("resolution_role")
    if role is not None:
        return str(role)
    return "unspecified"


def guarded_inference_transport(
    *,
    operation_target: RegisteredLLMOperationTarget,
    secret: ProviderSecret,
) -> GuardedAsyncInferenceTransport:
    """Bind one authorized target and short-lived secret to async inference."""

    return GuardedAsyncInferenceTransport(
        operation_target=operation_target,
        secret=secret,
    )


__all__ = [
    "LLMRuntimeClientBuilder",
    "guarded_inference_transport",
    "resolve_budget_role",
]
