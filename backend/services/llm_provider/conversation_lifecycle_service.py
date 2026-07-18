"""Provider-managed lifecycle through registered guarded operations.

This service owns guarded remote conversation create/delete orchestration.
Local conversation row persistence remains with route/runtime owners until the
route layer is moved in the next phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, normalize_provider_id
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    UserLLMSelection,
)

from .catalog_service import LLMProviderCatalogService
from .connection_authorization import LLMConnectionAuthorizer
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService
from .guarded_transport import GuardedTransport, GuardedTransportError
from .migration_service import LLMProviderMigrationService
from .types import (
    CredentialNotFoundError,
    LLMAuthMode,
    LLMConnectionAccessContext,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMDeploymentNotFoundError,
    ProviderConfigurationError,
    ProviderSecret,
)


@dataclass(frozen=True, slots=True)
class RemoteConversationOrigin:
    """Immutable identity required for later remote lifecycle operations."""

    connection_id: str
    deployment_id: str
    route_id: str
    origin_revision: int
    deployment_revision: int
    provider: str
    model: str
    remote_resource_id: str


@dataclass(frozen=True, slots=True)
class _ResolvedLifecycleOrigin:
    """Live rows revalidated from a persisted remote conversation snapshot."""

    connection: LLMInferenceConnection
    deployment: LLMModelDeployment
    route: LLMDeploymentRoute


class LLMConversationLifecycleService:
    """Run provider-managed remote conversation lifecycle calls."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
        guarded_transport: GuardedTransport | None = None,
        connection_authorizer: LLMConnectionAuthorizer | None = None,
        deployment_service: LLMDeploymentService | None = None,
        profile_service: EffectiveProfileService | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._credential_service = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
        )
        self._guarded_transport = guarded_transport or GuardedTransport()
        self._authorizer = connection_authorizer or LLMConnectionAuthorizer(db)
        self._deployments = deployment_service or LLMDeploymentService(db)
        self._profiles = profile_service or EffectiveProfileService()

    def create_remote_conversation(
        self,
        *,
        runtime_user_id: int,
        task_id: int,
        tenant_id: int,
    ) -> RemoteConversationOrigin:
        """Create remotely and return the exact authorized creation origin."""

        connection, deployment, route = self._resolve_selected_origin(
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            tenant_id=tenant_id,
        )
        self._authorizer.authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=runtime_user_id,
                task_id=task_id,
                tenant_id=tenant_id,
            ),
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            operation=LLMConnectionOperation.LIFECYCLE_CREATE,
        )
        secret = self._resolve_connection_secret(
            connection=connection,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose="remote_conversation_create",
        )
        provider = normalize_provider_id(connection.connection_preset_id)
        if provider == OPENAI_PROVIDER_ID:
            remote_resource_id = self._create_openai_conversation(secret.value)
            return RemoteConversationOrigin(
                connection_id=str(connection.id),
                deployment_id=str(deployment.id),
                route_id=str(route.id),
                origin_revision=int(connection.revision),
                deployment_revision=int(deployment.revision),
                provider=provider,
                model=deployment.canonical_model_id or deployment.wire_model_id,
                remote_resource_id=remote_resource_id,
            )
        raise ProviderConfigurationError(
            "Remote conversation create is not implemented for provider "
            f"{provider}"
        )

    def delete_remote_conversation(
        self,
        *,
        origin: RemoteConversationOrigin,
        runtime_user_id: int,
        task_id: int,
        tenant_id: int,
    ) -> None:
        """Delete remotely through the revalidated creation origin only."""

        resolved = self.validate_remote_conversation_origin(
            origin=origin,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            tenant_id=tenant_id,
        )
        secret = self._resolve_connection_secret(
            connection=resolved.connection,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose="remote_conversation_delete",
        )
        provider = normalize_provider_id(resolved.connection.connection_preset_id)
        if provider == OPENAI_PROVIDER_ID:
            self._delete_openai_conversation(
                secret.value,
                origin.remote_resource_id,
            )
            return
        raise ProviderConfigurationError(
            "Remote conversation delete is not implemented for provider "
            f"{provider}"
        )

    def validate_remote_conversation_origin(
        self,
        *,
        origin: RemoteConversationOrigin,
        runtime_user_id: int,
        task_id: int,
        tenant_id: int,
    ) -> _ResolvedLifecycleOrigin:
        """Reload and authorize a persisted lifecycle origin without side effects."""

        if not isinstance(origin, RemoteConversationOrigin):
            raise ProviderConfigurationError("Remote conversation origin is unmapped")
        deployment = self._deployments.get_deployment(
            user_id=runtime_user_id,
            deployment_id=origin.deployment_id,
        )
        if (
            str(deployment.connection_id) != origin.connection_id
            or int(deployment.revision) != origin.deployment_revision
            or not deployment.enabled
            or deployment.lifecycle_state != "active"
        ):
            raise LLMDeploymentNotFoundError("Remote conversation origin is stale")
        route = self._deployments.get_route(
            user_id=runtime_user_id,
            route_id=origin.route_id,
        )
        if str(route.deployment_id) != origin.deployment_id or not route.enabled:
            raise LLMDeploymentNotFoundError("Remote conversation route is unavailable")
        connection = self._db.get(LLMInferenceConnection, deployment.connection_id)
        if connection is None or int(connection.user_id) != runtime_user_id:
            raise LLMDeploymentNotFoundError("Remote conversation connection is unavailable")
        profile = self._require_remote_lifecycle_route(
            connection=connection,
            deployment=deployment,
            route=route,
        )
        if (
            profile.ref.provider != origin.provider
            or (deployment.canonical_model_id or deployment.wire_model_id) != origin.model
            or not origin.remote_resource_id
        ):
            raise LLMDeploymentNotFoundError("Remote conversation origin is unmapped")
        self._authorizer.authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=runtime_user_id,
                task_id=task_id,
                tenant_id=tenant_id,
            ),
            connection_id=origin.connection_id,
            expected_revision=origin.origin_revision,
            operation=LLMConnectionOperation.LIFECYCLE_DELETE,
            resource_id=origin.remote_resource_id,
        )
        return _ResolvedLifecycleOrigin(
            connection=connection,
            deployment=deployment,
            route=route,
        )

    def require_remote_conversation_lifecycle(self, provider: str) -> str:
        """Validate remote lifecycle support without performing SDK side effects."""

        return self._require_remote_lifecycle_provider(provider)

    def _require_remote_lifecycle_provider(self, provider: str) -> str:
        normalized_provider = normalize_provider_id(provider)
        provider_profile = self._catalog.require_provider(normalized_provider)
        try:
            provider_profile.require_capability(
                LLMCapability.REMOTE_CONVERSATION_LIFECYCLE
            )
        except Exception as exc:
            raise ProviderConfigurationError(
                f"Provider {normalized_provider} does not support remote "
                "conversation lifecycle"
            ) from exc
        return normalized_provider

    def _resolve_selected_origin(
        self,
        *,
        runtime_user_id: int,
        task_id: int,
        tenant_id: int,
    ) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
        LLMProviderMigrationService(self._db).backfill_deployment_identity_for_user(
            runtime_user_id
        )
        selection = self._db.execute(
            select(UserLLMSelection).where(UserLLMSelection.user_id == runtime_user_id)
        ).scalar_one_or_none()
        if selection is None or selection.deployment_id is None:
            raise LLMDeploymentNotFoundError(
                "Conversation selection has no deployment origin"
            )
        deployment = self._deployments.get_deployment(
            user_id=runtime_user_id,
            deployment_id=selection.deployment_id,
        )
        if not deployment.enabled or deployment.lifecycle_state != "active":
            raise LLMDeploymentNotFoundError("Conversation deployment is unavailable")
        connection = self._db.get(LLMInferenceConnection, deployment.connection_id)
        if connection is None or int(connection.user_id) != runtime_user_id:
            raise LLMDeploymentNotFoundError("Conversation connection is unavailable")
        route = self._select_or_create_native_route(
            runtime_user_id=runtime_user_id,
            connection=connection,
            deployment=deployment,
        )
        self._require_remote_lifecycle_route(
            connection=connection,
            deployment=deployment,
            route=route,
        )
        return connection, deployment, route

    def _select_or_create_native_route(
        self,
        *,
        runtime_user_id: int,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
    ) -> LLMDeploymentRoute:
        routes = tuple(
            route
            for route in self._deployments.list_routes(
                user_id=runtime_user_id,
                deployment_id=deployment.id,
            )
            if route.enabled
        )
        if routes:
            for route in routes:
                try:
                    self._profiles.resolve(
                        connection=connection,
                        deployment=deployment,
                        route=route,
                    )
                except Exception:
                    continue
                return route
            raise LLMDeploymentNotFoundError(
                "Conversation deployment has no verified route"
            )
        profile = self._profiles.resolve(
            connection=connection,
            deployment=deployment,
            route=None,
        )
        contract = self._profiles.native_route_contract(profile)
        route = LLMDeploymentRoute(
            id=uuid4(),
            deployment_id=deployment.id,
            adapter_id=contract.adapter_id,
            adapter_version=contract.adapter_version,
            api_surface=contract.api_surface,
            dialect_policy_id=contract.dialect_policy_id,
            enabled=True,
        )
        self._db.add(route)
        self._db.flush()
        return route

    def _require_remote_lifecycle_route(
        self,
        *,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute,
    ):
        provider = self._require_remote_lifecycle_provider(
            connection.connection_preset_id
        )
        profile = self._profiles.resolve(
            connection=connection,
            deployment=deployment,
            route=route,
        )
        if provider != OPENAI_PROVIDER_ID or profile.api_surface != "responses":
            raise ProviderConfigurationError(
                f"Deployment route {profile.api_surface} does not support remote conversation lifecycle"
            )
        return profile

    def _resolve_connection_secret(
        self,
        *,
        connection: LLMInferenceConnection,
        runtime_user_id: int,
        task_id: int,
        purpose: str,
    ) -> ProviderSecret:
        resolved_auth = self._credential_service.resolve_connection_auth(
            LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose=purpose,
            auth_mode=_connection_auth_mode(connection),
        )
        if resolved_auth.secret is None:
            raise CredentialNotFoundError(
                "Remote conversation lifecycle requires connection-bound credentials"
            )
        return resolved_auth.secret

    def _create_openai_conversation(self, api_key: str) -> str:
        try:
            response = self._guarded_transport.execute(
                LLMConnectionOperation.LIFECYCLE_CREATE,
                provider=OPENAI_PROVIDER_ID,
                secret=ProviderSecret(
                    provider=OPENAI_PROVIDER_ID,
                    value=api_key,
                ),
                json_body={},
            )
        except GuardedTransportError as exc:
            raise ProviderConfigurationError(
                f"OpenAI conversation create failed: {exc}"
            ) from None
        try:
            payload = json.loads(response.body)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise ProviderConfigurationError(
                "OpenAI conversation create failed: invalid provider response"
            ) from None
        conversation_id = payload.get("id") if isinstance(payload, dict) else None
        if not conversation_id:
            raise CredentialNotFoundError(
                "OpenAI did not return a conversation id"
            )
        return str(conversation_id)

    def _delete_openai_conversation(
        self,
        api_key: str,
        conversation_id: str,
    ) -> None:
        try:
            self._guarded_transport.execute(
                LLMConnectionOperation.LIFECYCLE_DELETE,
                provider=OPENAI_PROVIDER_ID,
                secret=ProviderSecret(
                    provider=OPENAI_PROVIDER_ID,
                    value=api_key,
                ),
                resource_id=conversation_id,
            )
        except GuardedTransportError as exc:
            raise ProviderConfigurationError(
                f"OpenAI conversation delete failed: {exc}"
            ) from None


def _connection_auth_mode(connection: LLMInferenceConnection) -> LLMAuthMode:
    config = connection.non_secret_config
    configured = config.get("auth_mode") if isinstance(config, dict) else None
    if configured is not None:
        try:
            return LLMAuthMode(str(configured).strip().lower())
        except ValueError as exc:
            raise ProviderConfigurationError(
                "Connection auth mode is not supported"
            ) from exc
    return (
        LLMAuthMode.API_KEY
        if connection.legacy_default_provider is not None
        else LLMAuthMode.NONE
    )


__all__ = ["LLMConversationLifecycleService", "RemoteConversationOrigin"]
