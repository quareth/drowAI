"""Orchestrate GPT-OSS proving connection lifecycle workflows.

This application service owns proving create, test, enablement, observation
rebinding, and their transaction boundaries. It delegates persistence,
credentials, evidence, status composition, and guarded egress to existing
provider authorities and excludes HTTP adaptation and public schemas.
"""

from __future__ import annotations

import hmac

from sqlalchemy.orm import Session

from .application_contracts import ConnectionStatusOutcome, VerificationOutcome
from .connection_service import LLMConnectionService
from .connection_status_service import LLMConnectionStatusService
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .health_service import LLMProviderHealthService
from .inventory_service import LLMProviderInventoryService
from .operation_registry import (
    GPT_OSS_20B_PROVING_PRESET_ID,
    ConnectionOperationRegistry,
)
from .types import (
    LLMAuthMode,
    LLMConnectionCredentialRef,
    LLMConnectionState,
    LLMDeploymentValidationError,
    ProviderConfigurationError,
)


class LLMProvingConnectionLifecycleService:
    """Run GPT-OSS proving workflows and own their request transactions."""

    def __init__(
        self,
        db: Session,
        *,
        operation_registry: ConnectionOperationRegistry | None = None,
        connection_service: LLMConnectionService | None = None,
        credential_service: LLMCredentialService | None = None,
        deployment_service: LLMDeploymentService | None = None,
        health_service: LLMProviderHealthService | None = None,
        inventory_service: LLMProviderInventoryService | None = None,
        status_service: LLMConnectionStatusService | None = None,
    ) -> None:
        self._db = db
        self._registry = operation_registry or ConnectionOperationRegistry()
        self._connections = connection_service or LLMConnectionService(db)
        self._credentials = credential_service or LLMCredentialService(db)
        self._deployments = deployment_service or LLMDeploymentService(db)
        self._health = health_service or LLMProviderHealthService(db)
        self._inventory = inventory_service or LLMProviderInventoryService(db)
        self._status = status_service or LLMConnectionStatusService(db)

    def create_connection(
        self,
        *,
        user_id: int,
        preset_id: str,
        api_key: str | None,
        display_label: str | None = None,
    ) -> ConnectionStatusOutcome:
        """Create one proving draft, credential, deployment, and status."""

        try:
            self._proving_preset(preset_id)
            secret = self._required_secret(api_key)
            connection = self._connections.create_gpt_oss_20b_proving_draft(
                user_id=user_id,
                display_label=display_label,
            )
            self._credentials.upsert_connection_api_key(
                user_id=user_id,
                connection_ref=LLMConnectionCredentialRef(
                    connection_id=str(connection.id),
                    expected_revision=int(connection.revision),
                ),
                provider=GPT_OSS_20B_PROVING_PRESET_ID,
                api_key=secret,
            )
            self._db.refresh(connection)
            deployment, _ = (
                self._deployments.create_gpt_oss_20b_proving_deployment(
                    user_id=user_id,
                    connection_id=connection.id,
                    expected_connection_revision=int(connection.revision),
                )
            )
            outcome = self._status.proving_status(
                user_id=user_id,
                connection=connection,
                deployment=deployment,
                verification=self._status.not_tested_verification(),
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
        api_key: str | None,
        connection_id: str,
        expected_connection_revision: int,
        deployment_id: str,
        expected_deployment_revision: int,
    ) -> VerificationOutcome:
        """Verify stored secret equality and record bounded proving evidence."""

        try:
            self._proving_preset(preset_id)
            secret = self._required_secret(api_key)
            deployment = self._owned_deployment_at_revision(
                user_id=user_id,
                deployment_id=deployment_id,
                expected_revision=expected_deployment_revision,
            )
            route = self._status.first_route_for_deployment(
                user_id=user_id,
                deployment_id=deployment.id,
            )
            connection_ref = LLMConnectionCredentialRef(
                connection_id=connection_id,
                expected_revision=expected_connection_revision,
            )
            stored_auth = self._credentials.resolve_connection_auth(
                connection_ref,
                runtime_user_id=user_id,
                purpose="gpt-oss-proving-test",
                auth_mode=LLMAuthMode.BEARER,
            )
            stored_secret = (
                stored_auth.secret.value if stored_auth.secret is not None else ""
            )
            if not hmac.compare_digest(stored_secret, secret):
                raise ProviderConfigurationError(
                    "Stored proving credential must pass verification"
                )
            credential_fingerprint = (
                self._credentials.connection_credential_fingerprint(
                    user_id=user_id,
                    connection_ref=connection_ref,
                    provider=GPT_OSS_20B_PROVING_PRESET_ID,
                )
            )
            result = self._health.verify_gpt_oss_20b_proving_connection(
                user_id=user_id,
                connection_id=connection_id,
                expected_connection_revision=expected_connection_revision,
                deployment_id=deployment.id,
                route_id=route.id,
                api_key=secret,
                credential_fingerprint=credential_fingerprint,
            )
            outcome = self._status.verification(result)
            self._db.commit()
            return outcome
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
        deployment_id: str,
        expected_deployment_revision: int,
    ) -> ConnectionStatusOutcome:
        """Enable a proving connection only after bound evidence is runnable."""

        try:
            self._proving_preset(preset_id)
            connection = self._connections.get_owned_at_revision(
                user_id=user_id,
                connection_id=connection_id,
                expected_revision=expected_connection_revision,
            )
            deployment = self._owned_deployment_at_revision(
                user_id=user_id,
                deployment_id=deployment_id,
                expected_revision=expected_deployment_revision,
            )
            if str(deployment.connection_id) != str(connection.id):
                raise LLMDeploymentValidationError(
                    "Deployment route is unavailable"
                )
            route = self._status.first_route_for_deployment(
                user_id=user_id,
                deployment_id=deployment.id,
            )
            runnability = self._status.proving_runnability(
                connection=connection,
                deployment=deployment,
                route=route,
            )
            if not runnability.runnable:
                raise ProviderConfigurationError(
                    "Successful proving verification is required before enablement"
                )
            verified_connection_revision = int(connection.revision)
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
            elif connection.state != LLMConnectionState.ENABLED.value:
                raise ProviderConfigurationError(
                    "Proving connection is not enableable"
                )
            self._inventory.rebind_proving_observation_revision(
                deployment=deployment,
                route=route,
                connection=connection,
                previous_connection_revision=verified_connection_revision,
            )
            outcome = self._status.proving_status(
                user_id=user_id,
                connection=connection,
                deployment=deployment,
                verification=VerificationOutcome(
                    status="passed",
                    code="verified",
                    message="GPT-OSS proving endpoint verified",
                    retryable=False,
                    model_present=True,
                ),
            )
            self._db.commit()
            return outcome
        except Exception:
            self._db.rollback()
            raise

    def _proving_preset(self, preset_id: str) -> None:
        """Validate the sole code-owned proving preset at application entry."""

        if preset_id != GPT_OSS_20B_PROVING_PRESET_ID:
            raise ProviderConfigurationError("Unknown proving preset")
        self._registry.get_proving_preset(preset_id)

    @staticmethod
    def _required_secret(api_key: str | None) -> str:
        """Return a non-empty supplied secret without retaining it in state."""

        secret = api_key.strip() if isinstance(api_key, str) else ""
        if not secret:
            raise ProviderConfigurationError("Proving API key is required")
        return secret

    def _owned_deployment_at_revision(
        self,
        *,
        user_id: int,
        deployment_id: str,
        expected_revision: int,
    ):
        """Resolve one owner-scoped deployment and enforce its expected revision."""

        deployment = self._deployments.get_deployment(
            user_id=user_id,
            deployment_id=deployment_id,
        )
        if int(deployment.revision) != int(expected_revision):
            raise ProviderConfigurationError("Deployment revision is stale")
        return deployment


__all__ = ["LLMProvingConnectionLifecycleService"]
