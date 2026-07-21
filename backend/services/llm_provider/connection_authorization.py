"""Live authorization boundary for connection-derived LLM operations.

Every decision reloads connection ownership, lifecycle state, revision, endpoint
policy, registered operation permission, and optional task/tenant scope.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import LLMInferenceConnection
from backend.services.task.access_service import get_tenant_task

from .operation_registry import (
    FIXED_PROVIDER_ENDPOINT_POLICY_ID,
    ConnectionOperationRegistry,
    OperationRegistryError,
)
from .runtime_services import LLMServiceOperationContext
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
_SERVICE_OPERATIONS = frozenset(
    {
        LLMConnectionOperation.HEALTH,
        LLMConnectionOperation.INVENTORY,
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

        return self._authorize_loaded_connection(
            connection=connection,
            expected_revision=expected_revision,
            operation=operation,
            resource_id=resource_id,
            require_enabled=False,
            allowed_operations=None,
            audit_actor_type="authenticated_user",
            audit_actor_id=str(access_context.authenticated_user_id),
            audit_correlation_id=None,
        )

    def authorize_service_operation(
        self,
        *,
        service_context: LLMServiceOperationContext,
        connection_id: UUID | str,
        expected_revision: int,
        operation: LLMConnectionOperation | str,
        resource_id: str | None = None,
    ) -> AuthorizedLLMConnectionOperation:
        """Authorize one bounded trusted service operation from live state."""

        if not isinstance(service_context, LLMServiceOperationContext):
            raise TypeError("service_context must be LLMServiceOperationContext")
        identifier = _connection_uuid(connection_id)
        connection = self._db.execute(
            select(LLMInferenceConnection)
            .where(LLMInferenceConnection.id == identifier)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if connection is None:
            raise _authorization_error(
                "connection_unavailable",
                "Connection is unavailable",
            )
        return self._authorize_loaded_connection(
            connection=connection,
            expected_revision=expected_revision,
            operation=operation,
            resource_id=resource_id,
            require_enabled=True,
            allowed_operations=_SERVICE_OPERATIONS,
            audit_actor_type="service",
            audit_actor_id=service_context.service_actor,
            audit_correlation_id=service_context.correlation_id
            or service_context.job_id,
        )

    def _authorize_loaded_connection(
        self,
        *,
        connection: LLMInferenceConnection,
        expected_revision: int,
        operation: LLMConnectionOperation | str,
        resource_id: str | None,
        require_enabled: bool,
        allowed_operations: frozenset[LLMConnectionOperation] | None,
        audit_actor_type: str,
        audit_actor_id: str | None,
        audit_correlation_id: str | None,
    ) -> AuthorizedLLMConnectionOperation:
        revision = _expected_revision(expected_revision)
        if int(connection.revision) != revision:
            raise _authorization_error(
                "stale_connection_revision",
                "Connection revision is stale",
            )
        normalized_operation = _normalize_operation(operation)
        if allowed_operations is not None and normalized_operation not in allowed_operations:
            raise _authorization_error(
                "operation_not_permitted",
                "Connection operation is not permitted",
            )
        try:
            state = LLMConnectionState(connection.state)
        except ValueError as exc:
            raise _authorization_error(
                "connection_not_enabled",
                "Connection is not enabled for this operation",
            ) from exc
        if require_enabled:
            state_denied = state is not LLMConnectionState.ENABLED
        else:
            state_denied = state is LLMConnectionState.DISABLED or (
                state is LLMConnectionState.DRAFT
                and normalized_operation not in _DRAFT_OPERATIONS
            )
        if state_denied:
            raise _authorization_error(
                "connection_not_enabled",
                "Connection is not enabled for this operation",
            )

        expected_endpoint_policy = FIXED_PROVIDER_ENDPOINT_POLICY_ID
        try:
            preset = self._operations.get_connection_preset(
                connection.connection_preset_id
            )
            expected_endpoint_policy = preset.endpoint_policy_id
        except OperationRegistryError:
            preset = None

        if (
            connection.transport_origin != "backend"
            or connection.endpoint_policy_id != expected_endpoint_policy
        ):
            raise _authorization_error(
                "endpoint_policy_denied",
                "Connection endpoint policy is not permitted",
            )

        base_url = (
            connection.endpoint_url
            if preset is not None and preset.endpoint_config_field is not None
            else None
        )
        try:
            target = self._operations.resolve(
                normalized_operation,
                provider=connection.connection_preset_id,
                base_url=base_url,
                resource_id=resource_id,
            )
        except OperationRegistryError as exc:
            raise _authorization_error(
                "operation_not_permitted",
                "Connection operation is not permitted",
            ) from exc

        if connection.endpoint_url is not None:
            endpoint_base = connection.endpoint_url.rstrip("/")
            if not target.url.startswith(f"{endpoint_base}/"):
                raise _authorization_error(
                    "endpoint_policy_denied",
                    "Connection endpoint policy is not permitted",
                )

        return AuthorizedLLMConnectionOperation(
            connection_id=str(connection.id),
            connection_revision=int(connection.revision),
            operation_target=target,
            audit_actor_type=audit_actor_type,
            audit_actor_id=audit_actor_id,
            audit_correlation_id=audit_correlation_id,
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


def _normalize_operation(
    value: LLMConnectionOperation | str,
) -> LLMConnectionOperation:
    try:
        return (
            value
            if isinstance(value, LLMConnectionOperation)
            else LLMConnectionOperation(str(value).strip().lower())
        )
    except ValueError as exc:
        raise _authorization_error(
            "operation_not_permitted",
            "Connection operation is not permitted",
        ) from exc


def _authorization_error(
    code: str,
    message: str,
) -> LLMConnectionAuthorizationError:
    return LLMConnectionAuthorizationError(code=code, message=message)


__all__ = ["LLMConnectionAuthorizer"]
