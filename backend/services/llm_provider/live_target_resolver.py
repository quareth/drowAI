"""Resolve live deployment-aware LLM targets behind the runtime facade.

Purpose: own the V2 deployment lookup, validation, authorization, credential,
metric, and target assembly policy for one runtime call.
Scope boundary: this module composes existing deployment, authorization,
profile, credential, and typed-contract authorities; it must not parse facade
selection inputs, resolve legacy compatibility, build provider clients, or
expose provider secrets outside the returned non-checkpoint target contract.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import ProviderModelRef
from core.llm.role_policy import RoleCallSettings

from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
)
from backend.services.metrics.utils import safe_inc_labeled

from .connection_authorization import LLMConnectionAuthorizer
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService
from .operation_registry import GPT_OSS_20B_PROVING_PRESET_ID
from .types import (
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeAccessContext,
    LLMRuntimeSelectionV2,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)


class LiveLLMTargetResolver:
    """Resolve deployment-aware runtime selections into authorized live targets."""

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

    def resolve_target(
        self,
        selection: LLMRuntimeSelectionV2,
        *,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
    ) -> ResolvedLLMTarget:
        """Resolve one V2 deployment selection into an authorized live target."""

        if self._db is None or self._deployments is None or self._authorizer is None:
            raise LLMConfigurationError("Deployment resolver database is unavailable")
        deployment = self._deployments.get_deployment(
            user_id=access_context.runtime_user_id,
            deployment_id=selection.deployment_ref.deployment_id,
        )
        if int(deployment.revision) != selection.deployment_ref.expected_revision:
            _emit_deployment_resolution_metric(
                "stale_revision",
                deployment_id=str(deployment.id),
            )
            raise LLMDeploymentNotFoundError("Deployment revision is unavailable")
        if not deployment.enabled or deployment.lifecycle_state != "active":
            _emit_deployment_resolution_metric(
                "deployment_unavailable",
                deployment_id=str(deployment.id),
            )
            raise LLMDeploymentNotFoundError("Deployment is unavailable")
        connection = self._db.get(LLMInferenceConnection, deployment.connection_id)
        if connection is None:
            _emit_deployment_resolution_metric(
                "connection_unavailable",
                deployment_id=str(deployment.id),
            )
            raise LLMDeploymentNotFoundError("Deployment connection was not found")
        route = self._select_route(
            user_id=access_context.runtime_user_id,
            deployment=deployment,
            preferred_route_id=selection.preferred_route_id,
        )
        profile = self._profiles.resolve(
            connection=connection,
            deployment=deployment,
            route=route,
        )
        if target is not None:
            requested = _target_ref(target)
            permitted_models = {
                profile.ref.model,
                deployment.wire_model_id.strip().lower(),
            }
            if (
                requested.provider != profile.ref.provider
                or requested.model not in permitted_models
            ):
                raise LLMDeploymentNotFoundError(
                    "Call target does not match the selected deployment"
                )
        try:
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
        except LLMConnectionAuthorizationError as exc:
            _emit_deployment_resolution_metric(
                _authorization_metric_status(exc.code),
                deployment_id=str(deployment.id),
                connection_id=str(connection.id),
            )
            raise
        auth_mode = _connection_auth_mode(connection)
        resolved_auth = self._credential_service.resolve_connection_auth(
            LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            runtime_user_id=access_context.runtime_user_id,
            task_id=access_context.task_id,
            purpose=purpose,
            auth_mode=auth_mode,
        )
        contract = self._profiles.native_route_contract(profile)
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
            deployment_id=str(deployment.id),
            deployment_revision=int(deployment.revision),
            route_id=str(route.id) if route is not None else None,
            adapter_id=route.adapter_id if route is not None else contract.adapter_id,
            adapter_version=(
                route.adapter_version if route is not None else contract.adapter_version
            ),
            api_surface=route.api_surface if route is not None else contract.api_surface,
            dialect_policy_id=(
                route.dialect_policy_id
                if route is not None
                else contract.dialect_policy_id
            ),
            canonical_model_id=deployment.canonical_model_id,
            exact_wire_model_id=deployment.wire_model_id,
            effective_profile=profile,
        )

    def _select_route(
        self,
        *,
        user_id: int,
        deployment: LLMModelDeployment,
        preferred_route_id: str | None,
    ) -> LLMDeploymentRoute | None:
        if self._deployments is None:
            raise LLMConfigurationError("Deployment service is unavailable")
        return self._deployments.select_enabled_route(
            user_id=user_id,
            deployment_id=deployment.id,
            preferred_route_id=preferred_route_id,
        )


def _emit_deployment_resolution_metric(
    status: str,
    *,
    deployment_id: str | None = None,
    connection_id: str | None = None,
    route_id: str | None = None,
) -> None:
    labels = {"status": status}
    if deployment_id is not None:
        labels["deployment_id"] = deployment_id
    if connection_id is not None:
        labels["connection_id"] = connection_id
    if route_id is not None:
        labels["route_id"] = route_id
    safe_inc_labeled("llm_provider.deployment_resolution.total", labels)


def _authorization_metric_status(code: str) -> str:
    if code == "stale_connection_revision":
        return "connection_revision_conflict"
    return code or "authorization_denied"


def _target_ref(
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget,
) -> ProviderModelRef:
    if isinstance(target, ProviderModelRef):
        return target.normalized()
    if isinstance(target, (RoleCallSettings, LLMCallTarget)):
        return ProviderModelRef(target.provider, target.model).normalized()
    raise TypeError(f"Unsupported LLM call target type: {type(target)!r}")


def _connection_auth_mode(connection: LLMInferenceConnection) -> LLMAuthMode:
    if connection.connection_preset_id == GPT_OSS_20B_PROVING_PRESET_ID:
        return LLMAuthMode.BEARER
    config = connection.non_secret_config
    configured_mode = config.get("auth_mode") if isinstance(config, dict) else None
    if configured_mode is not None:
        try:
            return LLMAuthMode(str(configured_mode).strip().lower())
        except ValueError as exc:
            raise LLMConfigurationError("Connection auth mode is not supported") from exc
    return (
        LLMAuthMode.API_KEY
        if connection.legacy_default_provider is not None
        else LLMAuthMode.NONE
    )


__all__ = ["LiveLLMTargetResolver"]
