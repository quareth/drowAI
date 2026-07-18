"""User-owned inference connection persistence and lifecycle service.

The service persists disabled drafts before any credential binding or outbound
work and owns optimistic-revision checks for connection mutations.
"""

from __future__ import annotations

from math import isfinite
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import LLMInferenceConnection

from .operation_registry import ConnectionOperationRegistry, OperationRegistryError
from .types import (
    LLMConnectionCredentialRef,
    LLMConnectionNotFoundError,
    LLMConnectionOperation,
    LLMConnectionRevisionConflictError,
    LLMConnectionState,
    LLMConnectionStateTransitionError,
    LLMConnectionValidationError,
)

FIXED_PROVIDER_ENDPOINT_POLICY_ID = "fixed_provider_v1"

_ALLOWED_STATE_TRANSITIONS = {
    LLMConnectionState.DRAFT: frozenset({LLMConnectionState.DISABLED}),
    LLMConnectionState.DISABLED: frozenset({LLMConnectionState.ENABLED}),
    LLMConnectionState.ENABLED: frozenset({LLMConnectionState.DISABLED}),
}
_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
    }
)


class LLMConnectionService:
    """Create and mutate inference connections within their owning user scope."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def create_draft(
        self,
        *,
        user_id: int,
        display_name: str,
        connection_preset_id: str,
        runtime_family_id: str,
        serving_operator_id: str | None = None,
        non_secret_config: dict[str, Any] | None = None,
    ) -> LLMInferenceConnection:
        """Persist a non-enabled revision-one draft before dependent work."""

        owner_id = _positive_int(user_id, "user_id")
        preset_id = _registered_fixed_preset(connection_preset_id)
        connection = LLMInferenceConnection(
            id=uuid4(),
            user_id=owner_id,
            display_name=_required_text(display_name, "display_name", 255),
            connection_preset_id=preset_id,
            runtime_family_id=_required_text(
                runtime_family_id,
                "runtime_family_id",
                100,
            ),
            serving_operator_id=_optional_text(
                serving_operator_id,
                "serving_operator_id",
                100,
            ),
            transport_origin="backend",
            endpoint_url=None,
            endpoint_policy_id=FIXED_PROVIDER_ENDPOINT_POLICY_ID,
            config_schema_version=1,
            non_secret_config=_optional_config(non_secret_config),
            state=LLMConnectionState.DRAFT.value,
            revision=1,
        )
        self._db.add(connection)
        self._db.flush()
        return connection

    def get_owned(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
    ) -> LLMInferenceConnection:
        """Return one user-owned connection without exposing foreign existence."""

        return self._require_owned(
            user_id=user_id,
            connection_id=connection_id,
            for_update=False,
        )

    def list_for_user(self, *, user_id: int) -> tuple[LLMInferenceConnection, ...]:
        """Return all connections owned by a user in deterministic order."""

        owner_id = _positive_int(user_id, "user_id")
        rows = self._db.execute(
            select(LLMInferenceConnection)
            .where(LLMInferenceConnection.user_id == owner_id)
            .order_by(
                LLMInferenceConnection.created_at.asc(),
                LLMInferenceConnection.id.asc(),
            )
        ).scalars()
        return tuple(rows)

    def get_owned_at_revision(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
    ) -> LLMInferenceConnection:
        """Return a locked owner-scoped connection at an expected revision."""

        return self._require_owned_revision(
            user_id=user_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
        )

    def update_draft(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
        display_name: str,
        non_secret_config: dict[str, Any] | None = None,
    ) -> LLMInferenceConnection:
        """Update editable draft configuration with optimistic revision checking."""

        connection = self._require_owned_revision(
            user_id=user_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
        )
        if connection.state != LLMConnectionState.DRAFT.value:
            raise LLMConnectionStateTransitionError(
                "Only draft connections accept configuration updates"
            )
        connection.display_name = _required_text(display_name, "display_name", 255)
        connection.non_secret_config = _optional_config(non_secret_config)
        connection.revision += 1
        self._db.flush()
        return connection

    def transition_state(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
        target_state: LLMConnectionState | str,
    ) -> LLMInferenceConnection:
        """Apply one explicit connection lifecycle transition and bump revision."""

        connection = self._require_owned_revision(
            user_id=user_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
        )
        current = LLMConnectionState(connection.state)
        try:
            target = (
                target_state
                if isinstance(target_state, LLMConnectionState)
                else LLMConnectionState(str(target_state))
            )
        except ValueError as exc:
            raise LLMConnectionStateTransitionError(
                "Unknown connection target state"
            ) from exc
        if target not in _ALLOWED_STATE_TRANSITIONS[current]:
            raise LLMConnectionStateTransitionError(
                f"Connection cannot transition from {current.value} to {target.value}"
            )
        connection.state = target.value
        connection.revision += 1
        self._db.flush()
        return connection

    def authorize_credential_binding(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
    ) -> LLMConnectionCredentialRef:
        """Return an opaque binding only for a persisted user-owned connection."""

        connection = self._require_owned_revision(
            user_id=user_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
        )
        return LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        )

    def delete(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
    ) -> None:
        """Revoke a connection by deleting its owner-scoped persistence row."""

        connection = self._require_owned_revision(
            user_id=user_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
        )
        self._db.delete(connection)
        self._db.flush()

    def _require_owned_revision(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
    ) -> LLMInferenceConnection:
        connection = self._require_owned(
            user_id=user_id,
            connection_id=connection_id,
            for_update=True,
        )
        revision = _positive_int(expected_revision, "expected_revision")
        if int(connection.revision) != revision:
            raise LLMConnectionRevisionConflictError(
                "Connection revision does not match the expected revision"
            )
        return connection

    def _require_owned(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        for_update: bool,
    ) -> LLMInferenceConnection:
        owner_id = _positive_int(user_id, "user_id")
        identifier = _connection_uuid(connection_id)
        statement = select(LLMInferenceConnection).where(
            LLMInferenceConnection.id == identifier,
            LLMInferenceConnection.user_id == owner_id,
        )
        if for_update:
            statement = statement.with_for_update()
        statement = statement.execution_options(populate_existing=True)
        connection = self._db.execute(statement).scalar_one_or_none()
        if connection is None:
            raise LLMConnectionNotFoundError("Connection was not found")
        return connection


def _registered_fixed_preset(value: str) -> str:
    """Require a preset admitted by the current code-owned registry."""

    preset = _required_text(value, "connection_preset_id", 100).lower()
    try:
        ConnectionOperationRegistry().resolve(
            LLMConnectionOperation.HEALTH,
            provider=preset,
        )
    except OperationRegistryError as exc:
        raise LLMConnectionValidationError(
            "Connection preset is not registered"
        ) from exc
    return preset


def _connection_uuid(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LLMConnectionNotFoundError("Connection was not found") from exc


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LLMConnectionValidationError(
            f"{field_name} must be a positive integer"
        )
    return value


def _required_text(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise LLMConnectionValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise LLMConnectionValidationError(f"{field_name} is invalid")
    return normalized


def _optional_text(
    value: str | None,
    field_name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, max_length)


def _optional_config(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LLMConnectionValidationError(
            "non_secret_config must be an object"
        )
    validated = dict(value)
    _validate_config_object(validated, depth=0)
    return validated


def _validate_config_object(value: dict[str, Any], *, depth: int) -> None:
    if depth > 8:
        raise LLMConnectionValidationError("non_secret_config is too deeply nested")
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise LLMConnectionValidationError(
                "non_secret_config keys must be non-empty strings"
            )
        key_with_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
        normalized_key = re.sub(
            r"[^a-z0-9]+",
            "_",
            key_with_boundaries.lower(),
        ).strip("_")
        if normalized_key in _SENSITIVE_CONFIG_KEYS:
            raise LLMConnectionValidationError(
                "Credential material cannot be stored in non_secret_config"
            )
        _validate_config_value(item, depth=depth + 1)


def _validate_config_value(value: Any, *, depth: int) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if isfinite(value):
            return
        raise LLMConnectionValidationError(
            "non_secret_config numbers must be finite"
        )
    if isinstance(value, list):
        if depth > 8:
            raise LLMConnectionValidationError(
                "non_secret_config is too deeply nested"
            )
        for item in value:
            _validate_config_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        _validate_config_object(value, depth=depth)
        return
    raise LLMConnectionValidationError(
        "non_secret_config must contain only JSON values"
    )


__all__ = [
    "FIXED_PROVIDER_ENDPOINT_POLICY_ID",
    "LLMConnectionService",
]
