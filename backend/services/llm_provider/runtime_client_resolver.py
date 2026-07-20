"""Turn-local LLMClient resolver for provider-neutral runtime selection.

This service is the narrow adapter-construction boundary. It resolves a
credential ref to a short-lived secret, then delegates provider/model adapter
construction to the tenant baseline `LLMClientFactory`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.base import (
    LLMClient,
)
from agent.providers.llm.core.budget_enforcing_client import (
    BudgetEnforcingLLMClient as _BudgetEnforcingLLMClient,
)
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMProfileNotFoundError,
)
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile
from core.llm.role_policy import RoleCallSettings

from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    Task,
)
from backend.services.metrics.utils import safe_inc_labeled

from .connection_authorization import LLMConnectionAuthorizer
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService, NativeRouteContract
from .guarded_transport import GuardedTransport
from .live_target_resolver import (
    LiveLLMTargetResolver,
    _connection_auth_mode as _live_connection_auth_mode,
)
from .operation_registry import ConnectionOperationRegistry
from .types import (
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionAccessContext,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMCredentialRef,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)

_UNSET = object()
_LEGACY_NORMALIZATION_NAMESPACE = UUID("24359013-a580-474c-933e-ddd1a2e78c92")

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

        parsed_selection = _parse_runtime_selection(selection)
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
            legacy_reasoning_effort = _resolve_supported_reasoning_effort(
                legacy_call_ref,
                reasoning_effort,
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
            supported_reasoning_effort = _resolve_supported_reasoning_effort(
                call_ref,
                reasoning_effort,
                model_profile=resolved_target.effective_profile,
            )
        if supported_reasoning_effort is not None:
            client_kwargs["reasoning_effort"] = supported_reasoning_effort
        elif reasoning_effort_kwarg is not _UNSET:
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
                isinstance(parsed_selection, LLMRuntimeSelectionV2)
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
        factory_kwargs["guarded_executor"] = _guarded_inference_executor(
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
        return _BudgetEnforcingLLMClient(
            client,
            provider_model=call_ref,
            role=_resolve_budget_role(target=target, client_kwargs=client_kwargs),
            model_profile=resolved_target.effective_profile,
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
        parsed = _parse_runtime_selection(selection)
        if isinstance(parsed, LLMRuntimeSelectionV2):
            return self._live_resolver.resolve_target(
                parsed,
                access_context=access_context,
                purpose=purpose,
                target=target,
            )
        return self._resolve_legacy_target(
            parsed,
            access_context=access_context,
            purpose=purpose,
            target=target,
        )

    def _resolve_legacy_target(
        self,
        selection: LLMRuntimeSelection,
        *,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None,
    ) -> ResolvedLLMTarget:
        if selection.credential_ref.user_id != access_context.runtime_user_id:
            raise LLMConfigurationError("Legacy selection user is not authorized")
        call_ref = resolve_call_target(selection, target)
        if self._db is not None:
            connection = self._db.execute(
                select(LLMInferenceConnection).where(
                    LLMInferenceConnection.user_id == access_context.runtime_user_id,
                    LLMInferenceConnection.legacy_default_provider == call_ref.provider,
                )
            ).scalar_one_or_none()
            if connection is not None:
                deployment = self._db.execute(
                    select(LLMModelDeployment).where(
                        LLMModelDeployment.connection_id == connection.id,
                        LLMModelDeployment.wire_model_id == call_ref.model,
                    )
                ).scalar_one_or_none()
                if deployment is not None:
                    _emit_legacy_identity_metric(
                        "mapped",
                        deployment_id=str(deployment.id),
                    )
                    return self._live_resolver.resolve_target(
                        LLMRuntimeSelectionV2(
                            deployment_ref=DeploymentRef(
                                str(deployment.id),
                                int(deployment.revision),
                            ),
                            reasoning_effort=selection.reasoning_effort,
                            legacy_provider=selection.provider,
                            legacy_model=selection.model,
                        ),
                        access_context=access_context,
                        purpose=purpose,
                        target=target,
                    )
                _emit_legacy_identity_metric(
                    "live_unmapped",
                    connection_id=str(connection.id),
                )
                return self._resolve_live_legacy_target(
                    selection,
                    call_ref=call_ref,
                    connection=connection,
                    access_context=access_context,
                    purpose=purpose,
                )
        _emit_legacy_identity_metric("detached")
        return self._resolve_detached_legacy_target(
            selection,
            call_ref=call_ref,
            access_context=access_context,
            purpose=purpose,
        )

    def _resolve_live_legacy_target(
        self,
        selection: LLMRuntimeSelection,
        *,
        call_ref: ProviderModelRef,
        connection: LLMInferenceConnection,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
    ) -> ResolvedLLMTarget:
        if self._authorizer is None:
            raise LLMConfigurationError("Connection authorizer is unavailable")
        try:
            profile = require_model_profile(call_ref)
        except LLMProfileNotFoundError:
            profile = None
        contract = (
            self._profiles.native_route_contract(profile)
            if profile is not None
            else _unresolved_legacy_route(call_ref.provider)
        )
        authorized = self._authorizer.authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=access_context.runtime_user_id,
                task_id=access_context.task_id,
                tenant_id=access_context.tenant_id,
            ),
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            operation="inference",
        )
        resolved_auth = self._credential_service.resolve_connection_auth(
            LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            runtime_user_id=access_context.runtime_user_id,
            task_id=access_context.task_id,
            purpose=purpose,
            auth_mode=_live_connection_auth_mode(connection),
        )
        deployment_id = uuid5(
            _LEGACY_NORMALIZATION_NAMESPACE,
            f"live:{connection.id}:{call_ref.model}",
        )
        return ResolvedLLMTarget(
            connection=ResolvedConnectionTarget(
                connection_id=authorized.connection_id,
                connection_revision=authorized.connection_revision,
                connection_preset_id=connection.connection_preset_id,
                runtime_family_id=connection.runtime_family_id,
                serving_operator_id=connection.serving_operator_id,
                transport_origin=connection.transport_origin,
                endpoint_policy_id=str(connection.endpoint_policy_id),
                endpoint=authorized.operation_target.url,
                operation_target=authorized.operation_target,
                resolved_auth=resolved_auth,
            ),
            deployment_id=str(deployment_id),
            deployment_revision=1,
            route_id=None,
            adapter_id=contract.adapter_id,
            adapter_version=contract.adapter_version,
            api_surface=contract.api_surface,
            dialect_policy_id=contract.dialect_policy_id,
            canonical_model_id=profile.ref.model if profile is not None else None,
            exact_wire_model_id=call_ref.model,
            effective_profile=profile,
        )

    def _resolve_detached_legacy_target(
        self,
        selection: LLMRuntimeSelection,
        *,
        call_ref: ProviderModelRef,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
    ) -> ResolvedLLMTarget:
        credential_selection = self._selection_for_call_provider(
            selection,
            call_ref=call_ref,
            runtime_user_id=access_context.runtime_user_id,
        )
        secret = self.resolve_secret(
            credential_selection,
            runtime_user_id=access_context.runtime_user_id,
            task_id=access_context.task_id,
            purpose=purpose,
        )
        try:
            profile = require_model_profile(call_ref)
        except LLMProfileNotFoundError:
            profile = None
        contract = (
            self._profiles.native_route_contract(profile)
            if profile is not None
            else _unresolved_legacy_route(call_ref.provider)
        )
        operation_target = ConnectionOperationRegistry().resolve(
            "inference",
            provider=call_ref.provider,
        )
        connection_id = uuid5(
            _LEGACY_NORMALIZATION_NAMESPACE,
            f"detached-connection:{access_context.runtime_user_id}:{call_ref.provider}",
        )
        deployment_id = uuid5(
            _LEGACY_NORMALIZATION_NAMESPACE,
            f"detached-deployment:{connection_id}:{call_ref.model}",
        )
        return ResolvedLLMTarget(
            connection=ResolvedConnectionTarget(
                connection_id=str(connection_id),
                connection_revision=1,
                connection_preset_id=call_ref.provider,
                runtime_family_id=f"{call_ref.provider}_native",
                serving_operator_id=call_ref.provider,
                transport_origin="backend",
                endpoint_policy_id="fixed_provider_v1",
                endpoint=operation_target.url,
                operation_target=operation_target,
                resolved_auth=ResolvedAuth.with_secret(
                    mode=LLMAuthMode.API_KEY,
                    provider=secret.provider,
                    secret=secret,
                ),
            ),
            deployment_id=str(deployment_id),
            deployment_revision=1,
            route_id=None,
            adapter_id=contract.adapter_id,
            adapter_version=contract.adapter_version,
            api_surface=contract.api_surface,
            dialect_policy_id=contract.dialect_policy_id,
            canonical_model_id=profile.ref.model if profile is not None else None,
            exact_wire_model_id=call_ref.model,
            effective_profile=profile,
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

    def _selection_for_call_provider(
        self,
        runtime_selection: LLMRuntimeSelection,
        *,
        call_ref: ProviderModelRef,
        runtime_user_id: int,
    ) -> LLMRuntimeSelection:
        """Return a selection whose credential provider matches the call target."""

        selected_provider = str(runtime_selection.credential_ref.provider)
        if call_ref.provider == selected_provider:
            return runtime_selection

        credential_ref = self._credential_service.get_credential_ref(
            runtime_user_id,
            call_ref.provider,
        )
        return LLMRuntimeSelection(
            provider=call_ref.provider,
            model=call_ref.model,
            credential_ref=credential_ref,
            reasoning_effort=runtime_selection.reasoning_effort,
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


def _emit_legacy_identity_metric(
    status: str,
    *,
    deployment_id: str | None = None,
    connection_id: str | None = None,
) -> None:
    labels = {"status": status}
    if deployment_id is not None:
        labels["deployment_id"] = deployment_id
    if connection_id is not None:
        labels["connection_id"] = connection_id
    safe_inc_labeled("llm_provider.legacy_identity_read.total", labels)


def _resolve_budget_role(
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


def _parse_runtime_selection(
    value: LLMRuntimeSelection | LLMRuntimeSelectionV2 | dict[str, Any],
) -> LLMRuntimeSelection | LLMRuntimeSelectionV2:
    if isinstance(value, (LLMRuntimeSelection, LLMRuntimeSelectionV2)):
        return value
    if not isinstance(value, dict):
        raise TypeError("Runtime selection requires a mapping or selection object")
    if value.get("schema_version") == 2 or "deployment_ref" in value:
        return LLMRuntimeSelectionV2.from_mapping(value)
    return LLMRuntimeSelection.from_mapping(value)


def _selection_reasoning_effort(
    selection: LLMRuntimeSelection | LLMRuntimeSelectionV2,
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None,
) -> str | None:
    if isinstance(target, (RoleCallSettings, LLMCallTarget)):
        return target.reasoning_effort
    return selection.reasoning_effort


def _unresolved_legacy_route(provider: str) -> NativeRouteContract:
    return NativeRouteContract(
        adapter_id=f"{provider}_unresolved",
        adapter_version="legacy",
        api_surface="unknown",
        dialect_policy_id=f"{provider}_unresolved.legacy",
    )


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


def _guarded_inference_executor(*, operation_target, secret: ProviderSecret):
    """Return the guarded compatible Chat Completions executor for one client."""

    transport = GuardedTransport()

    def _execute(json_body: Mapping[str, Any]) -> bytes:
        response = transport.execute(
            LLMConnectionOperation.INFERENCE,
            provider=operation_target.provider,
            secret=secret,
            json_body=json_body,
            operation_target=operation_target,
        )
        return response.body

    return _execute


def _resolve_supported_reasoning_effort(
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


__all__ = [
    "LLMRuntimeClientResolver",
    "resolve_call_reasoning_effort",
    "resolve_call_target",
]
