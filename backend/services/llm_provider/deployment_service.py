"""Owner-scoped model deployment persistence and route lookup service.

The service preserves exact endpoint wire-model identifiers and resolves
deployments and routes only through their current connection owner.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
)

from .connection_service import LLMConnectionService
from .types import (
    LLMConnectionNotFoundError,
    LLMDeploymentNotFoundError,
    LLMDeploymentValidationError,
)


class LLMDeploymentService:
    """Create deployments and resolve deployment routes in owner scope."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._connections = LLMConnectionService(db)

    def create_deployment(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_connection_revision: int,
        wire_model_id: str,
        display_name: str,
        discovery_source: str,
        canonical_model_id: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> LLMModelDeployment:
        """Persist a deployment after checking its connection owner/revision."""

        try:
            self._connections.get_owned_at_revision(
                user_id=user_id,
                connection_id=connection_id,
                expected_revision=expected_connection_revision,
            )
        except LLMConnectionNotFoundError as exc:
            raise LLMDeploymentNotFoundError(
                "Deployment connection was not found"
            ) from exc

        deployment = LLMModelDeployment(
            id=uuid4(),
            connection_id=_uuid(connection_id),
            wire_model_id=_exact_wire_model_id(wire_model_id),
            canonical_model_id=_optional_text(canonical_model_id, 255),
            display_name=_required_text(display_name, 255),
            discovery_source=_required_text(discovery_source, 50),
            source_metadata=_optional_metadata(source_metadata),
            lifecycle_state="active",
            availability_state="unknown",
            enabled=True,
            revision=1,
        )
        self._db.add(deployment)
        self._db.flush()
        return deployment

    def get_deployment(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
    ) -> LLMModelDeployment:
        """Return a deployment only through a connection owned by the user."""

        deployment = self._db.execute(
            select(LLMModelDeployment)
            .join(
                LLMInferenceConnection,
                LLMInferenceConnection.id == LLMModelDeployment.connection_id,
            )
            .where(
                LLMModelDeployment.id == _uuid(deployment_id),
                LLMInferenceConnection.user_id == _owner_id(user_id),
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if deployment is None:
            raise LLMDeploymentNotFoundError("Deployment was not found")
        return deployment

    def list_deployments(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
    ) -> tuple[LLMModelDeployment, ...]:
        """List deployments for one user-owned connection."""

        connection = self._connections.get_owned(
            user_id=user_id,
            connection_id=connection_id,
        )
        rows = self._db.execute(
            select(LLMModelDeployment)
            .where(LLMModelDeployment.connection_id == connection.id)
            .order_by(
                LLMModelDeployment.created_at.asc(),
                LLMModelDeployment.id.asc(),
            )
        ).scalars()
        return tuple(rows)

    def get_route(
        self,
        *,
        user_id: int,
        route_id: UUID | str,
    ) -> LLMDeploymentRoute:
        """Return a route after reloading its deployment connection owner."""

        route = self._db.execute(
            select(LLMDeploymentRoute)
            .join(
                LLMModelDeployment,
                LLMModelDeployment.id == LLMDeploymentRoute.deployment_id,
            )
            .join(
                LLMInferenceConnection,
                LLMInferenceConnection.id == LLMModelDeployment.connection_id,
            )
            .where(
                LLMDeploymentRoute.id == _uuid(route_id),
                LLMInferenceConnection.user_id == _owner_id(user_id),
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if route is None:
            raise LLMDeploymentNotFoundError("Deployment route was not found")
        return route

    def list_routes(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
    ) -> tuple[LLMDeploymentRoute, ...]:
        """List routes for one owner-scoped deployment."""

        deployment = self.get_deployment(
            user_id=user_id,
            deployment_id=deployment_id,
        )
        rows = self._db.execute(
            select(LLMDeploymentRoute)
            .where(LLMDeploymentRoute.deployment_id == deployment.id)
            .order_by(
                LLMDeploymentRoute.created_at.asc(),
                LLMDeploymentRoute.id.asc(),
            )
        ).scalars()
        return tuple(rows)


def _uuid(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LLMDeploymentNotFoundError("Deployment identity was not found") from exc


def _exact_wire_model_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise LLMDeploymentValidationError("Wire model identity is invalid")
    return value


def _required_text(value: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise LLMDeploymentValidationError("Deployment field is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise LLMDeploymentValidationError("Deployment field is invalid")
    return normalized


def _optional_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, max_length)


def _optional_metadata(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LLMDeploymentValidationError("source_metadata must be an object")
    return dict(value)


def _owner_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LLMDeploymentValidationError("user_id must be a positive integer")
    return value


__all__ = ["LLMDeploymentService"]
