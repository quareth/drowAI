"""Orchestrate reviewed managed LLM connection lifecycle workflows.

This application service owns create, test, inventory refresh, enablement, and
their transaction boundaries. It delegates persistence, authorization,
credential, inventory, status, and guarded-egress policy to existing focused
authorities and excludes HTTP adaptation, public schemas, and proving flows.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .application_contracts import ConnectionStatusOutcome, VerificationOutcome
from .connection_authorization import LLMConnectionAuthorizer
from .connection_service import LLMConnectionService
from .connection_status_service import LLMConnectionStatusService
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .guarded_transport import GuardedTransport, GuardedTransportError
from .health_service import map_guarded_provider_error
from .inventory_service import LLMInventoryService
from .operation_registry import (
    GPT_OSS_20B_PROVING_PRESET_ID,
    PUBLIC_GPT_OSS_20B_PRESET_IDS,
    ConnectionOperationRegistry,
)
from .types import (
    LLMAuthMode,
    LLMConnectionAccessContext,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
    ProviderConfigurationError,
    ProviderSecret,
)


class LLMManagedConnectionLifecycleService:
    """Run reviewed connection workflows and own their request transactions."""

    def __init__(
        self,
        db: Session,
        *,
        operation_registry: ConnectionOperationRegistry | None = None,
        connection_service: LLMConnectionService | None = None,
        credential_service: LLMCredentialService | None = None,
        deployment_service: LLMDeploymentService | None = None,
        inventory_service: LLMInventoryService | None = None,
        connection_authorizer: LLMConnectionAuthorizer | None = None,
        guarded_transport: GuardedTransport | None = None,
        status_service: LLMConnectionStatusService | None = None,
    ) -> None:
        self._db = db
        self._registry = operation_registry or ConnectionOperationRegistry()
        self._connections = connection_service or LLMConnectionService(db)
        self._credentials = credential_service or LLMCredentialService(db)
        self._deployments = deployment_service or LLMDeploymentService(db)
        self._authorizer = connection_authorizer or LLMConnectionAuthorizer(db)
        self._inventory = inventory_service or LLMInventoryService(
            db,
            connection_authorizer=self._authorizer,
            operation_registry=self._registry,
        )
        self._transport = guarded_transport or GuardedTransport(
            registry=self._registry,
        )
        self._status = status_service or LLMConnectionStatusService(db)

    def create_connection(
        self,
        *,
        user_id: int,
        preset_id: str,
        api_key: str | None,
        display_label: str | None = None,
        base_url: str | None = None,
        wire_model_id: str | None = None,
        model_label: str | None = None,
        canonical_model_id: str | None = None,
    ) -> ConnectionStatusOutcome:
        """Create a reviewed draft, optional deployment, and committed status."""

        try:
            preset = self._managed_preset(preset_id)
            secret = api_key.strip() if isinstance(api_key, str) else ""
            if not secret:
                raise ProviderConfigurationError(
                    "Connection API key is required"
                )
            non_secret_config = {"auth_mode": "bearer"}
            if preset.endpoint_config_field is not None:
                non_secret_config[preset.endpoint_config_field] = base_url
            connection = self._connections.create_draft(
                user_id=user_id,
                display_name=display_label or preset.display_name,
                connection_preset_id=preset.id,
                runtime_family_id=preset.runtime_family_id,
                serving_operator_id=preset.serving_operator_id,
                non_secret_config=non_secret_config,
            )
            self._credentials.upsert_connection_api_key(
                user_id=user_id,
                connection_ref=LLMConnectionCredentialRef(
                    connection_id=str(connection.id),
                    expected_revision=int(connection.revision),
                ),
                provider=preset.id,
                api_key=secret,
            )
            self._db.refresh(connection)
            deployment = None
            selected_wire_model = (
                wire_model_id
                or preset.exact_wire_model_id
                or preset.canonical_model_id
            )
            if selected_wire_model:
                deployment, _ = self._inventory.register_custom_model(
                    user_id=user_id,
                    connection_id=connection.id,
                    expected_connection_revision=int(connection.revision),
                    wire_model_id=selected_wire_model,
                    display_name=model_label or selected_wire_model,
                    canonical_model_id=(
                        canonical_model_id or preset.canonical_model_id or None
                    ),
                    requested_capabilities=(),
                )
            outcome = self._status.managed_status(
                user_id=user_id,
                connection=connection,
                deployment=deployment,
            )
            self._db.commit()
            return outcome
        except Exception:
            self._db.rollback()
            raise

    def test_connection(
        self,
        *,
        user_id: int,
        preset_id: str,
        connection_id: str,
        expected_connection_revision: int,
        api_key: str | None = None,
    ) -> VerificationOutcome:
        """Run one authorized health or product capability probe."""

        try:
            preset = self._managed_preset(preset_id)
            connection = self._owned_preset_connection(
                user_id=user_id,
                preset_id=preset.id,
                connection_id=connection_id,
                expected_connection_revision=expected_connection_revision,
            )
            secret = self._connection_secret(
                user_id=user_id,
                connection=connection,
                api_key=api_key,
                purpose="connection-preset-health-check",
            )
            deployment = self._product_connection_deployment(
                user_id=user_id,
                connection=connection,
                preset=preset,
            )
            operation = (
                LLMConnectionOperation.CAPABILITY_PROBE
                if deployment is not None
                else LLMConnectionOperation.HEALTH
            )
            transport_kwargs = {}
            if deployment is not None:
                transport_kwargs["json_body"] = {
                    "model": deployment.wire_model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                    "max_tokens": 1,
                }
            authorized = self._authorizer.authorize(
                access_context=LLMConnectionAccessContext(
                    authenticated_user_id=user_id,
                ),
                connection_id=connection.id,
                expected_revision=int(connection.revision),
                operation=operation,
            )
            self._transport.execute(
                operation,
                provider=preset.id,
                secret=ProviderSecret(provider=preset.id, value=secret),
                operation_target=authorized.operation_target,
                **transport_kwargs,
            )
            outcome = VerificationOutcome(
                status="passed",
                code="verified",
                message=(
                    "GPT-OSS 20B is ready"
                    if deployment is not None
                    else "Connection endpoint verified"
                ),
                retryable=False,
            )
            self._db.commit()
            return outcome
        except GuardedTransportError as exc:
            self._db.rollback()
            if exc.status_code in {401, 403, 429}:
                raise map_guarded_provider_error(preset.display_name, exc) from None
            raise
        except Exception:
            self._db.rollback()
            raise

    def refresh_inventory(
        self,
        *,
        user_id: int,
        preset_id: str,
        connection_id: str,
        expected_connection_revision: int,
        api_key: str | None = None,
    ) -> ConnectionStatusOutcome:
        """Refresh reviewed inventory and commit one exact deployment status."""

        try:
            preset = self._managed_preset(preset_id)
            connection = self._owned_preset_connection(
                user_id=user_id,
                preset_id=preset.id,
                connection_id=connection_id,
                expected_connection_revision=expected_connection_revision,
            )
            secret = self._connection_secret(
                user_id=user_id,
                connection=connection,
                api_key=api_key,
                purpose="connection-preset-inventory-refresh",
            )
            authorized = self._authorizer.authorize(
                access_context=LLMConnectionAccessContext(
                    authenticated_user_id=user_id,
                ),
                connection_id=connection.id,
                expected_revision=int(connection.revision),
                operation=LLMConnectionOperation.INVENTORY,
            )
            response = self._transport.execute(
                LLMConnectionOperation.INVENTORY,
                provider=preset.id,
                secret=ProviderSecret(provider=preset.id, value=secret),
                operation_target=authorized.operation_target,
            )
            model_ids = self._inventory.parse_inventory_model_ids(response.body)
            deployments = self._inventory.refresh_inventory(
                user_id=user_id,
                connection_id=connection.id,
                expected_connection_revision=int(connection.revision),
                discovered_model_ids=model_ids,
            )
            outcome = self._status.managed_status(
                user_id=user_id,
                connection=connection,
                deployment=deployments[0] if len(deployments) == 1 else None,
            )
            self._db.commit()
            return outcome
        except GuardedTransportError as exc:
            self._db.rollback()
            if exc.status_code in {401, 403, 429}:
                raise map_guarded_provider_error(preset.display_name, exc) from None
            raise
        except Exception:
            self._db.rollback()
            raise

    def enable_connection(
        self,
        *,
        user_id: int,
        preset_id: str,
        connection_id: str,
        expected_connection_revision: int,
        deployment_id: str | None = None,
        expected_deployment_revision: int | None = None,
    ) -> ConnectionStatusOutcome:
        """Enable one reviewed connection after validating its optional deployment."""

        try:
            preset = self._managed_preset(preset_id)
            connection = self._owned_preset_connection(
                user_id=user_id,
                preset_id=preset.id,
                connection_id=connection_id,
                expected_connection_revision=expected_connection_revision,
            )
            deployment = None
            if (deployment_id is None) != (expected_deployment_revision is None):
                raise ProviderConfigurationError(
                    "Deployment reference is incomplete"
                )
            if deployment_id is not None:
                deployment = self._deployments.get_deployment(
                    user_id=user_id,
                    deployment_id=deployment_id,
                )
                if int(deployment.revision) != expected_deployment_revision:
                    raise ProviderConfigurationError(
                        "Deployment revision is stale"
                    )
                if str(deployment.connection_id) != str(connection.id):
                    raise ProviderConfigurationError(
                        "Deployment connection mismatch"
                    )
            if connection.state == LLMConnectionState.DRAFT.value:
                connection = self._connections.transition_state(
                    user_id=user_id,
                    connection_id=connection.id,
                    expected_revision=int(connection.revision),
                    target_state=LLMConnectionState.DISABLED,
                )
            if connection.state == LLMConnectionState.DISABLED.value:
                connection = self._connections.transition_state(
                    user_id=user_id,
                    connection_id=connection.id,
                    expected_revision=int(connection.revision),
                    target_state=LLMConnectionState.ENABLED,
                )
            outcome = self._status.managed_status(
                user_id=user_id,
                connection=connection,
                deployment=deployment,
            )
            self._db.commit()
            return outcome
        except Exception:
            self._db.rollback()
            raise

    def _managed_preset(self, preset_id: str):
        preset = self._registry.get_connection_preset(preset_id)
        if preset.id == GPT_OSS_20B_PROVING_PRESET_ID:
            raise ProviderConfigurationError(
                "Use proving preset routes for GPT-OSS proving"
            )
        return preset

    def _owned_preset_connection(
        self,
        *,
        user_id: int,
        preset_id: str,
        connection_id: str,
        expected_connection_revision: int,
    ):
        connection = self._connections.get_owned_at_revision(
            user_id=user_id,
            connection_id=connection_id,
            expected_revision=expected_connection_revision,
        )
        if connection.connection_preset_id != preset_id:
            raise ProviderConfigurationError("Connection preset mismatch")
        return connection

    def _connection_secret(
        self,
        *,
        user_id: int,
        connection,
        api_key: str | None,
        purpose: str,
    ) -> str:
        supplied = api_key.strip() if isinstance(api_key, str) else ""
        if supplied:
            return supplied
        resolved = self._credentials.resolve_connection_auth(
            LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            runtime_user_id=user_id,
            purpose=purpose,
            auth_mode=LLMAuthMode.BEARER,
        )
        return resolved.secret.value if resolved.secret is not None else ""

    def _product_connection_deployment(self, *, user_id: int, connection, preset):
        if preset.id not in PUBLIC_GPT_OSS_20B_PRESET_IDS:
            return None
        deployments = self._deployments.list_deployments(
            user_id=user_id,
            connection_id=connection.id,
        )
        for deployment in deployments:
            if preset.exact_wire_model_id:
                if deployment.wire_model_id == preset.exact_wire_model_id:
                    return deployment
                continue
            model_id = deployment.canonical_model_id or deployment.wire_model_id
            if model_id == preset.canonical_model_id:
                return deployment
        return None


__all__ = ["LLMManagedConnectionLifecycleService"]
