"""Resolve legacy LLM selections beside the runtime facade.

Purpose: own legacy mapped, live-unmapped, and detached compatibility target
resolution while preserving the facade-owned selection parsing and call-target
resolution boundary.
Scope boundary: this module may compose live target resolution for mapped
selections and existing credential, authorization, profile, and operation
authorities; it must not import the facade, parse arbitrary runtime selections,
build provider clients, or expose provider secrets outside the returned target.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMProfileNotFoundError,
)
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from core.llm.role_policy import RoleCallSettings

from backend.models import LLMInferenceConnection, LLMModelDeployment
from backend.services.metrics.utils import safe_inc_labeled

from .connection_authorization import LLMConnectionAuthorizer
from .credential_service import LLMCredentialService
from .effective_profile_service import EffectiveProfileService, NativeRouteContract
from .live_target_resolver import LiveLLMTargetResolver, _connection_auth_mode
from .operation_registry import ConnectionOperationRegistry
from .types import (
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionAccessContext,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)

_LEGACY_NORMALIZATION_NAMESPACE = UUID("24359013-a580-474c-933e-ddd1a2e78c92")


class LegacyLLMTargetResolver:
    """Resolve legacy runtime selections into compatibility targets."""

    def __init__(
        self,
        credential_service: LLMCredentialService,
        *,
        live_resolver: LiveLLMTargetResolver,
        db: Session | None = None,
        connection_authorizer: LLMConnectionAuthorizer | None = None,
        effective_profile_service: EffectiveProfileService | None = None,
    ) -> None:
        self._credential_service = credential_service
        self._live_resolver = live_resolver
        self._db = db or getattr(credential_service, "_db", None)
        self._authorizer = connection_authorizer or (
            LLMConnectionAuthorizer(self._db) if self._db is not None else None
        )
        self._profiles = effective_profile_service or EffectiveProfileService()

    def resolve(
        self,
        selection: LLMRuntimeSelection,
        *,
        call_ref: ProviderModelRef,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
    ) -> ResolvedLLMTarget:
        """Resolve one legacy selection through mapped, live, or detached policy."""

        if selection.credential_ref.user_id != access_context.runtime_user_id:
            raise LLMConfigurationError("Legacy selection user is not authorized")
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
            auth_mode=_connection_auth_mode(connection),
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
        secret = self._resolve_secret(
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
            LLMConnectionOperation.INFERENCE,
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

    def _resolve_secret(
        self,
        selection: LLMRuntimeSelection,
        *,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
    ) -> ProviderSecret:
        return self._credential_service.resolve_secret(
            selection.credential_ref,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose=purpose,
        )


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


def _unresolved_legacy_route(provider: str) -> NativeRouteContract:
    return NativeRouteContract(
        adapter_id=f"{provider}_unresolved",
        adapter_version="legacy",
        api_surface="unknown",
        dialect_policy_id=f"{provider}_unresolved.legacy",
    )


__all__ = ["LegacyLLMTargetResolver"]
