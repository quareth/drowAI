"""Resolve owner-scoped deployments for durable text LLM selections.

This module centralizes save/read validation shared by conversation, reporting,
and memory selection services. It does not resolve credentials or construct
runtime clients.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.profiles.registry import ModelProfile
from backend.models import LLMInferenceConnection, LLMModelDeployment

from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService
from .types import (
    LLMAuthMode,
    LLMDeploymentNotFoundError,
    LLMSelectionStatus,
    ProviderConfigurationError,
)


@dataclass(frozen=True, slots=True)
class SelectionDeploymentTarget:
    """Validated deployment facts safe to copy into a selection snapshot."""

    connection: LLMInferenceConnection
    deployment: LLMModelDeployment
    profile: ModelProfile

    @property
    def provider(self) -> str:
        """Return the compatibility provider snapshot."""

        return self.profile.ref.provider

    @property
    def model(self) -> str:
        """Return the canonical compatibility model snapshot."""

        return self.profile.ref.model


class LLMSelectionDeploymentResolver:
    """Validate a deployment for one selection role and owner."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._deployments = LLMDeploymentService(db)
        self._profiles = EffectiveProfileService()

    def resolve(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
        expected_revision: int | None = None,
        role: str,
        require_structured_output: bool = False,
    ) -> SelectionDeploymentTarget:
        """Return current compatible deployment facts or fail closed."""

        deployment = self._deployments.get_deployment(
            user_id=user_id,
            deployment_id=deployment_id,
        )
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or int(deployment.revision) != expected_revision
        ):
            raise LLMDeploymentNotFoundError(
                "Deployment revision is unavailable"
            )
        if not deployment.enabled or deployment.lifecycle_state != "active":
            raise LLMDeploymentNotFoundError("Deployment is unavailable")
        connection = self._db.get(LLMInferenceConnection, deployment.connection_id)
        if connection is None or int(connection.user_id) != int(user_id):
            raise LLMDeploymentNotFoundError("Deployment connection was not found")
        profile = self._profiles.resolve(
            connection=connection,
            deployment=deployment,
            route=None,
        )
        if not profile.supports(LLMCapability.CHAT):
            raise ProviderConfigurationError(
                f"Deployment is incompatible with {role}: chat is unsupported"
            )
        if require_structured_output and not profile.structured_output_strategies:
            raise ProviderConfigurationError(
                f"Deployment is incompatible with {role}: structured output is unsupported"
            )
        return SelectionDeploymentTarget(
            connection=connection,
            deployment=deployment,
            profile=profile,
        )

    @staticmethod
    def classify_runnability(
        *,
        user_id: int,
        target: SelectionDeploymentTarget,
        credential_available: Callable[[int, str], bool],
        missing_credential_reason: str,
    ) -> LLMSelectionStatus | None:
        """Return a non-runnable status for current connection/auth facts."""

        connection = target.connection
        config = connection.non_secret_config
        configured_mode = config.get("auth_mode") if isinstance(config, dict) else None
        try:
            auth_mode = (
                LLMAuthMode(str(configured_mode).strip().lower())
                if configured_mode is not None
                else (
                    LLMAuthMode.API_KEY
                    if connection.legacy_default_provider is not None
                    else LLMAuthMode.NONE
                )
            )
        except ValueError as exc:
            return LLMSelectionStatus(
                status="invalid_selection",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )
        if auth_mode in {LLMAuthMode.API_KEY, LLMAuthMode.BEARER} and not (
            connection.legacy_default_provider
            and credential_available(user_id, connection.legacy_default_provider)
        ):
            return LLMSelectionStatus(
                status="credential_missing",
                selectable=True,
                runnable=False,
                reason=missing_credential_reason,
            )
        if connection.state != "enabled":
            return LLMSelectionStatus(
                status="connection_unavailable",
                selectable=True,
                runnable=False,
                reason="Deployment connection is not enabled",
            )
        return None


__all__ = ["LLMSelectionDeploymentResolver", "SelectionDeploymentTarget"]
