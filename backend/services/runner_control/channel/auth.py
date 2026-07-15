"""Runner websocket-channel authentication helpers.

Purpose: authenticate runner channel transport headers into a tenant-bound
channel identity and revalidate active session credentials. Scope boundary:
this module owns auth checks only; inbound message routing remains with channel
orchestration modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner
from backend.services.runner_control.channel.errors import _build_error_envelope
from backend.services.runner_control.channel.types import RunnerChannelSession
from backend.services.runner_control.credentials import (
    RunnerCredentialAuthError,
    RunnerCredentialService,
)
from runtime_shared.runner_protocol import RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE, RunnerEnvelope


@dataclass(frozen=True, slots=True)
class RunnerChannelAuthContext:
    """Authenticated runner-channel identity returned to websocket handlers."""

    tenant_id: int
    runner_id: UUID
    credential_id: UUID
    allowed_protocol_versions: tuple[str, ...]


class RunnerChannelAuthError(ValueError):
    """Raised when runner channel authentication fails closed."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class RunnerChannelAuthService:
    """Authenticate runner channel headers without relying on user bearer tokens."""

    def __init__(
        self,
        db: Session,
        *,
        credential_service: RunnerCredentialService | None = None,
        allowed_protocol_versions: tuple[str, ...] = RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE,
    ) -> None:
        self._db = db
        self._credential_service = credential_service or RunnerCredentialService(db)
        self._allowed_protocol_versions = tuple(
            version for version in (str(value).strip() for value in allowed_protocol_versions) if version
        ) or RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE

    def authenticate(
        self,
        *,
        tenant_id_header: str | None,
        runner_id_header: str | None,
        runner_secret_header: str | None,
    ) -> RunnerChannelAuthContext:
        """Authenticate one runner channel identity from required headers."""

        tenant_id = _parse_tenant_id(tenant_id_header)
        runner_id = _parse_uuid_value(runner_id_header, field_name="runner_id")
        runner_secret = _normalize_required_secret(runner_secret_header)

        runner = self._db.execute(
            select(Runner).where(Runner.tenant_id == tenant_id, Runner.id == runner_id)
        ).scalar_one_or_none()
        if runner is None:
            raise RunnerChannelAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner channel authentication failed.",
            )

        try:
            credential = self._credential_service.authenticate_runner_credential(
                tenant_id=tenant_id,
                runner_id=runner_id,
                plaintext_secret=runner_secret,
            )
        except RunnerCredentialAuthError as exc:
            raise RunnerChannelAuthError(
                error_code=exc.error_code,
                message="Runner channel authentication failed.",
            ) from exc

        return RunnerChannelAuthContext(
            tenant_id=tenant_id,
            runner_id=runner_id,
            credential_id=credential.id,
            allowed_protocol_versions=self._allowed_protocol_versions,
        )


def _parse_tenant_id(raw_value: str | None) -> int:
    normalized = str(raw_value or "").strip()
    if not normalized:
        raise RunnerChannelAuthError(
            error_code="RUNNER_AUTH_INVALID",
            message="Runner channel authentication failed.",
        )
    try:
        tenant_id = int(normalized)
    except ValueError as exc:
        raise RunnerChannelAuthError(
            error_code="RUNNER_AUTH_INVALID",
            message="Runner channel authentication failed.",
        ) from exc
    if tenant_id <= 0:
        raise RunnerChannelAuthError(
            error_code="RUNNER_AUTH_INVALID",
            message="Runner channel authentication failed.",
        )
    return tenant_id


def _parse_uuid_value(raw_value: str | None, *, field_name: str) -> UUID:
    normalized = str(raw_value or "").strip()
    if not normalized:
        raise RunnerChannelAuthError(
            error_code="RUNNER_AUTH_INVALID",
            message=f"Runner channel `{field_name}` is required.",
        )
    try:
        return UUID(normalized)
    except ValueError as exc:
        raise RunnerChannelAuthError(
            error_code="RUNNER_AUTH_INVALID",
            message=f"Runner channel `{field_name}` is malformed.",
        ) from exc


def _normalize_required_secret(raw_secret: str | None) -> str:
    normalized = str(raw_secret or "").strip()
    if not normalized:
        raise RunnerChannelAuthError(
            error_code="RUNNER_AUTH_INVALID",
            message="Runner channel authentication failed.",
        )
    return normalized


def _validate_session_authorization(
    *,
    db: Session,
    credential_service: RunnerCredentialService,
    session: RunnerChannelSession,
    correlation_id: str | None,
) -> RunnerEnvelope | None:
    runner = db.execute(
        select(Runner).where(
            Runner.tenant_id == session.tenant_id,
            Runner.id == session.runner_id,
        )
    ).scalar_one_or_none()
    if runner is None:
        return _build_error_envelope(
            session=session,
            error_code="RUNNER_AUTH_INVALID",
            message="Runner channel authentication failed.",
            correlation_id=correlation_id,
        )
    if str(runner.status or "").strip().lower() == "revoked":
        return _build_error_envelope(
            session=session,
            error_code="RUNNER_AUTH_REVOKED",
            message="Runner credential has been revoked.",
            correlation_id=correlation_id,
        )
    try:
        credential_service.assert_runner_credential_active(
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            credential_id=session.credential_id,
        )
    except RunnerCredentialAuthError as exc:
        return _build_error_envelope(
            session=session,
            error_code=exc.error_code,
            message="Runner credential validation failed.",
            correlation_id=correlation_id,
        )
    return None
