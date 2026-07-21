"""Turn-local LLMClient resolver for provider-neutral runtime selection.

This service is the stable facade and security boundary. It parses runtime
selection, normalizes trusted access context, resolves authorized targets, and
delegates provider-client construction to the runtime client builder.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.base import (
    LLMClient,
)
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
)
from agent.providers.llm.core.identity import ProviderModelRef
from core.llm.role_policy import RoleCallSettings

from backend.models import Task

from .connection_authorization import LLMConnectionAuthorizer
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService
from .legacy_target_resolver import LegacyLLMTargetResolver
from .live_target_resolver import LiveLLMTargetResolver
from .runtime_client_builder import LLMRuntimeClientBuilder
from .types import (
    LLMCallTarget,
    LLMCredentialRef,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedLLMTarget,
    parse_llm_runtime_selection,
)

_UNSET = object()


class LLMRuntimeClientResolver:
    """Resolve runtime selections into concrete provider clients."""

    def __init__(
        self,
        credential_service: LLMCredentialService,
        *,
        db: Session | None = None,
        deployment_service: LLMDeploymentService | None = None,
        connection_authorizer: LLMConnectionAuthorizer | None = None,
        effective_profile_service: EffectiveProfileService | None = None,
    ) -> None:
        self._credential_service = credential_service
        self._db = db or getattr(credential_service, "_db", None)
        self._deployments = deployment_service or (
            LLMDeploymentService(self._db) if self._db is not None else None
        )
        self._authorizer = connection_authorizer or (
            LLMConnectionAuthorizer(self._db) if self._db is not None else None
        )
        self._profiles = effective_profile_service or EffectiveProfileService()
        self._live_resolver = LiveLLMTargetResolver(
            credential_service,
            db=self._db,
            deployment_service=self._deployments,
            connection_authorizer=self._authorizer,
            effective_profile_service=self._profiles,
        )
        self._legacy_resolver = LegacyLLMTargetResolver(
            credential_service,
            live_resolver=self._live_resolver,
            db=self._db,
            connection_authorizer=self._authorizer,
            effective_profile_service=self._profiles,
        )
        self._client_builder = LLMRuntimeClientBuilder()

    def get_client(
        self,
        selection: LLMRuntimeSelection | LLMRuntimeSelectionV2 | dict[str, Any],
        *,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
        access_context: LLMRuntimeAccessContext | None = None,
        runtime_user_id: int | None = None,
        task_id: int | None = None,
        tenant_id: int | None = None,
        purpose: str,
        **client_kwargs: Any,
    ) -> LLMClient:
        """Create an LLMClient for the selected credential context."""

        parsed_selection = parse_llm_runtime_selection(selection)
        legacy_call_ref = (
            resolve_call_target(parsed_selection, target)
            if isinstance(parsed_selection, LLMRuntimeSelection)
            else None
        )
        reasoning_effort_kwarg = client_kwargs.get("reasoning_effort", _UNSET)
        reasoning_effort = (
            reasoning_effort_kwarg
            if reasoning_effort_kwarg is not _UNSET
            else _selection_reasoning_effort(parsed_selection, target)
        )
        legacy_reasoning_effort = None
        if legacy_call_ref is not None:
            legacy_reasoning_effort = (
                self._client_builder.resolve_supported_reasoning_effort(
                    legacy_call_ref,
                    reasoning_effort,
                )
            )
        trusted_context = self._trusted_access_context(
            parsed_selection,
            access_context=access_context,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            tenant_id=tenant_id,
        )
        resolved_target = self.resolve_target(
            parsed_selection,
            access_context=trusted_context,
            target=target,
            purpose=purpose,
        )
        return self._client_builder.build(
            selection=parsed_selection,
            resolved_target=resolved_target,
            target=target,
            legacy_call_ref=legacy_call_ref,
            legacy_reasoning_effort=legacy_reasoning_effort,
            reasoning_effort=reasoning_effort,
            reasoning_effort_was_explicit=reasoning_effort_kwarg is not _UNSET,
            client_kwargs=client_kwargs,
        )

    def resolve_target(
        self,
        selection: LLMRuntimeSelection | LLMRuntimeSelectionV2 | dict[str, Any],
        *,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
    ) -> ResolvedLLMTarget:
        """Normalize legacy or V2 selection into one authorized live target."""

        if not isinstance(access_context, LLMRuntimeAccessContext):
            raise TypeError("access_context must be LLMRuntimeAccessContext")
        parsed = parse_llm_runtime_selection(selection)
        if isinstance(parsed, LLMRuntimeSelectionV2):
            return self._live_resolver.resolve_target(
                parsed,
                access_context=access_context,
                purpose=purpose,
                target=target,
            )
        call_ref = resolve_call_target(parsed, target)
        return self._legacy_resolver.resolve(
            parsed,
            call_ref=call_ref,
            access_context=access_context,
            purpose=purpose,
            target=target,
        )

    def _trusted_access_context(
        self,
        selection: LLMRuntimeSelection | LLMRuntimeSelectionV2,
        *,
        access_context: LLMRuntimeAccessContext | None,
        runtime_user_id: int | None,
        task_id: int | None,
        tenant_id: int | None,
    ) -> LLMRuntimeAccessContext:
        if access_context is not None:
            if not isinstance(access_context, LLMRuntimeAccessContext):
                raise TypeError("access_context must be LLMRuntimeAccessContext")
            if runtime_user_id is not None and (
                runtime_user_id != access_context.runtime_user_id
            ):
                raise LLMConfigurationError("Conflicting runtime user identity")
            return access_context
        if runtime_user_id is None:
            raise TypeError("runtime_user_id is required for runtime selection")
        resolved_tenant_id = tenant_id
        if task_id is not None and resolved_tenant_id is None and self._db is not None:
            resolved_tenant_id = self._db.execute(
                select(Task.tenant_id).where(
                    Task.id == task_id,
                    Task.user_id == runtime_user_id,
                )
            ).scalar_one_or_none()
            if resolved_tenant_id is None:
                raise LLMConfigurationError("Runtime task identity is unavailable")
        if task_id is not None and resolved_tenant_id is None:
            return LLMRuntimeAccessContext(runtime_user_id=runtime_user_id)
        return LLMRuntimeAccessContext(
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            tenant_id=resolved_tenant_id,
        )

    def resolve_secret(
        self,
        selection: LLMRuntimeSelection | dict[str, Any],
        *,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
    ) -> ProviderSecret:
        """Resolve the selected credential context to a short-lived secret."""

        runtime_selection = LLMRuntimeSelection.from_mapping(selection)
        return self._credential_service.resolve_secret(
            runtime_selection.credential_ref,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose=purpose,
        )

    def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        """Return an enabled credential ref for explicit non-chat dependencies."""

        return self._credential_service.get_credential_ref(user_id, provider)


def _selection_reasoning_effort(
    selection: LLMRuntimeSelection | LLMRuntimeSelectionV2,
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None,
) -> str | None:
    if isinstance(target, (RoleCallSettings, LLMCallTarget)):
        return target.reasoning_effort
    return selection.reasoning_effort


def resolve_call_target(
    selection: LLMRuntimeSelection | dict[str, Any],
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
) -> ProviderModelRef:
    """Resolve the provider/model for a concrete LLM call."""

    runtime_selection = LLMRuntimeSelection.from_mapping(selection)
    if target is None:
        return ProviderModelRef(runtime_selection.provider, runtime_selection.model)
    if isinstance(target, ProviderModelRef):
        return target.normalized()
    if isinstance(target, RoleCallSettings):
        return ProviderModelRef(target.provider, target.model).normalized()
    if isinstance(target, LLMCallTarget):
        return ProviderModelRef(target.provider, target.model).normalized()
    raise TypeError(f"Unsupported LLM call target type: {type(target)!r}")


def resolve_call_reasoning_effort(
    selection: LLMRuntimeSelection | dict[str, Any],
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
) -> str | None:
    """Resolve the reasoning effort for a concrete LLM call."""

    runtime_selection = LLMRuntimeSelection.from_mapping(selection)
    if isinstance(target, (RoleCallSettings, LLMCallTarget)):
        return target.reasoning_effort
    return runtime_selection.reasoning_effort


__all__ = [
    "LLMRuntimeClientResolver",
    "resolve_call_reasoning_effort",
    "resolve_call_target",
]
