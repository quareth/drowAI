"""Compose non-secret LLM connection refs, verification, and status outcomes.

This service owns owner-scoped route lookup and read-only status composition
over existing LLM provider authorities. It must not own HTTP adaptation,
transactions, lifecycle mutation, credential resolution, or guarded egress.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
)

from .application_contracts import (
    ConnectionRefOutcome,
    ConnectionStatusOutcome,
    DeploymentRefOutcome,
    RunnabilityOutcome,
    VerificationOutcome,
    VerificationUsageOutcome,
)
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService
from .inventory_service import GptOssProvingVerificationResult
from .operation_registry import GPT_OSS_20B_PROVING_PRESET_ID
from .selection_deployment_resolver import (
    LLMSelectionDeploymentResolver,
    SelectionDeploymentTarget,
)
from .types import (
    LLMConnectionCredentialRef,
    LLMDeploymentValidationError,
    LLMProviderServiceError,
)


class LLMConnectionStatusService:
    """Build typed connection status without workflow side effects."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._credentials = LLMCredentialService(db)
        self._deployments = LLMDeploymentService(db)
        self._profiles = EffectiveProfileService()
        self._evidence = EffectiveProfileService(db)
        self._selection_resolver = LLMSelectionDeploymentResolver(db)

    @staticmethod
    def connection_ref(connection: LLMInferenceConnection) -> ConnectionRefOutcome:
        """Return the current opaque connection identity and revision."""

        return ConnectionRefOutcome(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        )

    @staticmethod
    def deployment_ref(deployment: LLMModelDeployment) -> DeploymentRefOutcome:
        """Return the current opaque deployment identity and revision."""

        return DeploymentRefOutcome(
            deployment_id=str(deployment.id),
            expected_revision=int(deployment.revision),
        )

    def first_route_for_deployment(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
    ) -> LLMDeploymentRoute:
        """Return the first owner-scoped route or a stable provider error."""

        routes = self._deployments.list_routes(
            user_id=user_id,
            deployment_id=deployment_id,
        )
        if not routes:
            raise LLMDeploymentValidationError("Deployment route is unavailable")
        return routes[0]

    @staticmethod
    def verification(
        result: GptOssProvingVerificationResult,
    ) -> VerificationOutcome:
        """Convert sanitized proving evidence to the application contract."""

        usage = result.usage
        return VerificationOutcome(
            status=result.status,
            code=result.code,
            message=result.message,
            retryable=result.retryable,
            observed_at=result.observed_at,
            expires_at=result.expires_at,
            model_present=result.model_present,
            usage=(
                VerificationUsageOutcome(
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                )
                if usage is not None
                else None
            ),
        )

    @staticmethod
    def not_tested_verification() -> VerificationOutcome:
        """Return the stable status used when no verification has run."""

        return VerificationOutcome(
            status="failed",
            code="not_tested",
            message="Verification has not run.",
            retryable=False,
        )

    def connection_runnability(
        self,
        *,
        user_id: int,
        connection: LLMInferenceConnection | None,
        deployment: LLMModelDeployment | None,
        route: LLMDeploymentRoute | None,
    ) -> RunnabilityOutcome:
        """Return current runnability for a reviewed connection deployment."""

        if connection is None:
            return RunnabilityOutcome(
                status="not_created",
                selectable=True,
                runnable=False,
                reason="Connection configuration is required.",
            )
        if deployment is None:
            return RunnabilityOutcome(
                status="deployment_missing",
                selectable=True,
                runnable=False,
                reason="Deployment model registration is required.",
            )
        if route is None or not route.enabled:
            return RunnabilityOutcome(
                status="capability_unknown",
                selectable=True,
                runnable=False,
                reason="Capability evidence is required.",
            )
        try:
            profile = self._profiles.resolve(
                connection=connection,
                deployment=deployment,
                route=route,
            )
            status = self._selection_resolver.classify_runnability(
                user_id=user_id,
                target=SelectionDeploymentTarget(
                    connection=connection,
                    deployment=deployment,
                    route=route,
                    profile=profile,
                ),
                credential_available=self._credentials.has_enabled_credential,
                credential_fingerprint=(
                    self._credentials.connection_credential_fingerprint
                ),
                missing_credential_reason="Stored connection credential is required.",
                required_capabilities=(LLMCapability.CHAT,),
                capability_missing_reason="Capability evidence is required.",
            )
        except LLMProviderServiceError as exc:
            return RunnabilityOutcome(
                status="invalid_selection",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )
        if status is not None:
            return RunnabilityOutcome(
                status=status.status,
                selectable=status.selectable,
                runnable=status.runnable,
                reason=status.reason,
            )
        return RunnabilityOutcome(
            status="runnable",
            selectable=True,
            runnable=True,
            reason=None,
        )

    def proving_runnability(
        self,
        *,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute,
    ) -> RunnabilityOutcome:
        """Return chat and usage-evidence status for a proving deployment."""

        try:
            credential_fingerprint = (
                self._credentials.connection_credential_fingerprint(
                    user_id=int(connection.user_id),
                    connection_ref=LLMConnectionCredentialRef(
                        connection_id=str(connection.id),
                        expected_revision=int(connection.revision),
                    ),
                    provider=GPT_OSS_20B_PROVING_PRESET_ID,
                )
            )
        except LLMProviderServiceError:
            return RunnabilityOutcome(
                status="credential_missing",
                selectable=True,
                runnable=False,
                reason="Stored proving credential is required.",
            )
        decision = self._evidence.classify_runnability(
            deployment=deployment,
            route=route,
            required_capabilities=(
                LLMCapability.CHAT,
                LLMCapability.USAGE_REPORTING,
            ),
            connection_id=str(connection.id),
            connection_revision=int(connection.revision),
            credential_fingerprint=credential_fingerprint,
        )
        if decision.runnable:
            return RunnabilityOutcome(
                status="runnable",
                selectable=True,
                runnable=True,
                reason=None,
            )
        return RunnabilityOutcome(
            status=decision.status,
            selectable=True,
            runnable=False,
            reason="Usage evidence is required.",
        )

    def managed_status(
        self,
        *,
        user_id: int,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment | None,
    ) -> ConnectionStatusOutcome:
        """Return one reviewed connection lifecycle status outcome."""

        route = None
        if deployment is not None:
            try:
                route = self.first_route_for_deployment(
                    user_id=user_id,
                    deployment_id=deployment.id,
                )
            except LLMDeploymentValidationError:
                route = None
        return ConnectionStatusOutcome(
            lifecycle_state=connection.state,
            connection_ref=self.connection_ref(connection),
            deployment_ref=(
                self.deployment_ref(deployment) if deployment is not None else None
            ),
            verification=self.not_tested_verification(),
            runnability=self.connection_runnability(
                user_id=user_id,
                connection=connection,
                deployment=deployment,
                route=route,
            ),
        )

    def proving_status(
        self,
        *,
        user_id: int,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        verification: VerificationOutcome | None = None,
    ) -> ConnectionStatusOutcome:
        """Return one proving connection lifecycle status outcome."""

        route = self.first_route_for_deployment(
            user_id=user_id,
            deployment_id=deployment.id,
        )
        return ConnectionStatusOutcome(
            lifecycle_state=connection.state,
            connection_ref=self.connection_ref(connection),
            deployment_ref=self.deployment_ref(deployment),
            verification=verification,
            runnability=self.proving_runnability(
                connection=connection,
                deployment=deployment,
                route=route,
            ),
        )


__all__ = ["LLMConnectionStatusService"]
