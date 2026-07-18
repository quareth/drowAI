"""Live authorization boundary for connection-derived LLM operations.

Every decision reloads connection ownership, lifecycle state, revision, endpoint
policy, registered operation permission, and optional task/tenant scope.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import LLMInferenceConnection
from backend.services.task.access_service import get_tenant_task

from .connection_service import FIXED_PROVIDER_ENDPOINT_POLICY_ID
from .operation_registry import ConnectionOperationRegistry, OperationRegistryError
from .types import (
    AuthorizedLLMConnectionOperation,
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionOperation,
    LLMConnectionState,
)

_DRAFT_OPERATIONS = frozenset(
    {
        LLMConnectionOperation.HEALTH,
        LLMConnectionOperation.INVENTORY,
        LLMConnectionOperation.CAPABILITY_PROBE,
    }
)


class LLMConnectionAuthorizer:
    """Authorize one live registered operation from server-side state."""

    def __init__(
        self,
        db: Session,
        *,
        operation_registry: ConnectionOperationRegistry | None = None,
    ) -> None:
        self._db = db
        self._operations = operation_registry or ConnectionOperationRegistry()

    def authorize(
        self,
        *,
        access_context: LLMConnectionAccessContext,
        connection_id: UUID | str,
        expected_revision: int,
        operation: LLMConnectionOperation | str,
        resource_id: str | None = None,
    ) -> AuthorizedLLMConnectionOperation:
        """Reload and authorize a connection operation or fail closed."""

        if not isinstance(access_context, LLMConnectionAccessContext):
            raise TypeError("access_context must be LLMConnectionAccessContext")
        identifier = _connection_uuid(connection_id)
        connection = self._db.execute(
            select(LLMInferenceConnection)
            .where(LLMInferenceConnection.id == identifier)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            connection is None
            or int(connection.user_id) != access_context.authenticated_user_id
        ):
            raise _authorization_error(
                "connection_unavailable",
                "Connection is unavailable",
            )

        if access_context.task_id is not None:
            task = get_tenant_task(
                self._db,
                access_context.task_id,
                access_context.authenticated_user_id,
                tenant_id=int(access_context.tenant_id),
            )
            if task is None:
                raise _authorization_error(
                    "task_context_denied",
                    "Task context is not authorized",
                )

        revision = _expected_revision(expected_revision)
        if int(connection.revision) != revision:
            raise _authorization_error(
                "stale_connection_revision",
                "Connection revision is stale",
            )

        try:
            normalized_operation = (
                operation
                if isinstance(operation, LLMConnectionOperation)
                else LLMConnectionOperation(str(operation).strip().lower())
            )
        except ValueError as exc:
            raise _authorization_error(
                "operation_not_permitted",
                "Connection operation is not permitted",
            ) from exc

        try:
            state = LLMConnectionState(connection.state)
        except ValueError as exc:
            raise _authorization_error(
                "connection_not_enabled",
                "Connection is not enabled for this operation",
            ) from exc
        if state is LLMConnectionState.DISABLED or (
            state is LLMConnectionState.DRAFT
            and normalized_operation not in _DRAFT_OPERATIONS
        ):
            raise _authorization_error(
                "connection_not_enabled",
                "Connection is not enabled for this operation",
            )

        if (
            connection.transport_origin != "backend"
            or connection.endpoint_policy_id != FIXED_PROVIDER_ENDPOINT_POLICY_ID
        ):
            raise _authorization_error(
                "endpoint_policy_denied",
                "Connection endpoint policy is not permitted",
            )

        try:
            target = self._operations.resolve(
                normalized_operation,
                provider=connection.connection_preset_id,
                resource_id=resource_id,
            )
        except OperationRegistryError as exc:
            raise _authorization_error(
                "operation_not_permitted",
                "Connection operation is not permitted",
            ) from exc

        if connection.endpoint_url is not None:
            parsed = urlsplit(target.url)
            expected_origin = f"{parsed.scheme}://{parsed.netloc}"
            if connection.endpoint_url not in {
                expected_origin,
                f"{expected_origin}/",
            }:
                raise _authorization_error(
                    "endpoint_policy_denied",
                    "Connection endpoint policy is not permitted",
                )

        return AuthorizedLLMConnectionOperation(
            connection_id=str(connection.id),
            connection_revision=int(connection.revision),
            operation_target=target,
        )


def _connection_uuid(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _authorization_error(
            "connection_unavailable",
            "Connection is unavailable",
        ) from exc


def _expected_revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _authorization_error(
            "stale_connection_revision",
            "Connection revision is stale",
        )
    return value


def _authorization_error(
    code: str,
    message: str,
) -> LLMConnectionAuthorizationError:
    return LLMConnectionAuthorizationError(code=code, message=message)


__all__ = ["LLMConnectionAuthorizer"]
