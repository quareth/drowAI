"""Runner registration workflow service for runner control-plane onboarding.

This module exchanges one-time install tokens for runner credentials, normalizes
runner self-reported metadata with bounded limits, and records registration
state transactionally with tenant-bound audit events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner, RunnerInstallToken
from backend.services.runner_control.audit import RunnerControlAuditEmitter, RunnerControlAuditService
from backend.services.runner_control.credentials import (
    IssuedRunnerCredential,
    RunnerCredentialService,
    RunnerInstallTokenValidationError,
)
from runtime_shared.runner_protocol import RUNNER_PROTOCOL_DATA_PLANE_VERSION

MAX_RUNNER_NAME_LENGTH = 255
MAX_RUNNER_VERSION_LENGTH = 64
MAX_LABEL_COUNT = 32
MAX_LABEL_KEY_LENGTH = 64
MAX_LABEL_VALUE_LENGTH = 128
MAX_CAPABILITY_COUNT = 64
MAX_CAPABILITY_LENGTH = 64


class RunnerRegistrationError(RuntimeError):
    """Raised when runner registration cannot be completed."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class RunnerRegistrationRequest:
    """Input envelope for tenant-bound runner registration."""

    install_token: str
    runner_name: str
    tenant_id: int | None = None
    runner_version: str | None = None
    labels: Mapping[str, object] | None = None
    capabilities: Sequence[object] | Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RunnerRegistrationResult:
    """Result payload returned after successful runner registration."""

    runner_id: UUID
    tenant_id: int
    credential_id: UUID
    credential_fingerprint: str
    credential_secret: str
    endpoint_metadata: dict[str, object]


class RunnerRegistrationService:
    """Register runners by exchanging one-time install tokens for credentials."""

    def __init__(
        self,
        db: Session,
        *,
        credential_service: RunnerCredentialService | None = None,
        audit_emitter: RunnerControlAuditEmitter | None = None,
        channel_endpoint: str | None = None,
        protocol_version: str = RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        heartbeat_interval_seconds: int = 30,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._credential_service = credential_service or RunnerCredentialService(db)
        self._audit = RunnerControlAuditService(emitter=audit_emitter)
        self._channel_endpoint = (channel_endpoint or "").strip()
        self._protocol_version = str(protocol_version).strip() or "runner_control.v1"
        self._heartbeat_interval_seconds = max(1, int(heartbeat_interval_seconds))
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def register_runner(self, request: RunnerRegistrationRequest) -> RunnerRegistrationResult:
        """Register a runner transactionally and return one-time credential material."""

        try:
            normalized = _normalize_registration_request(request)
            install_token_value = str(request.install_token or "").strip()
            if not install_token_value:
                raise RunnerRegistrationError(
                    error_code="RUNNER_INSTALL_TOKEN_INVALID",
                    message="Runner registration failed.",
                )

            with self._transaction_context():
                install_token = self._verify_install_token(request=request, plaintext_token=install_token_value)
                tenant_id = int(install_token.tenant_id)

                runner = self._find_or_create_runner(
                    tenant_id=tenant_id,
                    execution_site_id=install_token.execution_site_id,
                    normalized=normalized,
                )

                issued_credential = self._credential_service.issue_runner_credential(
                    tenant_id=tenant_id,
                    runner_id=runner.id,
                )

                if not self._consume_install_token(install_token_id=install_token.id):
                    raise RunnerRegistrationError(
                        error_code="RUNNER_INSTALL_TOKEN_INVALID",
                        message="Runner registration failed.",
                    )

                self._emit_registered_audit(
                    tenant_id=tenant_id,
                    runner_id=runner.id,
                    install_token=install_token_value,
                    issued_credential=issued_credential,
                    normalized=normalized,
                )

                return RunnerRegistrationResult(
                    runner_id=runner.id,
                    tenant_id=tenant_id,
                    credential_id=issued_credential.credential_id,
                    credential_fingerprint=issued_credential.credential_fingerprint,
                    credential_secret=issued_credential.plaintext_secret,
                    endpoint_metadata={
                        "channel_endpoint": self._channel_endpoint,
                        "protocol_version": self._protocol_version,
                        "heartbeat_interval_seconds": self._heartbeat_interval_seconds,
                    },
                )
        except RunnerRegistrationError:
            raise
        except RunnerInstallTokenValidationError as exc:
            raise RunnerRegistrationError(
                error_code="RUNNER_INSTALL_TOKEN_INVALID",
                message="Runner registration failed.",
            ) from exc
        except ValueError as exc:
            raise RunnerRegistrationError(
                error_code="RUNNER_METADATA_INVALID",
                message=str(exc),
            ) from exc
        except Exception as exc:
            raise RunnerRegistrationError(
                error_code="RUNNER_REGISTRATION_FAILED",
                message="Runner registration failed.",
            ) from exc

    def _verify_install_token(
        self,
        *,
        request: RunnerRegistrationRequest,
        plaintext_token: str,
    ) -> RunnerInstallToken:
        if request.tenant_id is not None:
            return self._credential_service.verify_install_token(
                tenant_id=int(request.tenant_id),
                plaintext_token=plaintext_token,
            )
        return self._credential_service.verify_install_token_for_enrollment(
            plaintext_token=plaintext_token,
        )

    def _find_or_create_runner(
        self,
        *,
        tenant_id: int,
        execution_site_id: UUID,
        normalized: _NormalizedRunnerMetadata,
    ) -> Runner:
        existing = self._db.execute(
            select(Runner).where(
                Runner.tenant_id == tenant_id,
                Runner.execution_site_id == execution_site_id,
                Runner.name == normalized.runner_name,
            )
        ).scalar_one_or_none()

        if existing is None:
            runner = Runner(
                tenant_id=tenant_id,
                execution_site_id=execution_site_id,
                name=normalized.runner_name,
                status="registered",
                version=normalized.runner_version,
                labels_json=normalized.labels_json,
                capabilities_json=normalized.capabilities_json,
            )
            self._db.add(runner)
            self._db.flush()
            return runner

        existing.status = "registered"
        existing.version = normalized.runner_version
        existing.labels_json = normalized.labels_json
        existing.capabilities_json = normalized.capabilities_json
        self._db.flush()
        return existing

    def _consume_install_token(self, *, install_token_id: UUID) -> bool:
        now = self._now()
        result = self._db.execute(
            update(RunnerInstallToken)
            .where(
                RunnerInstallToken.id == install_token_id,
                RunnerInstallToken.used_at.is_(None),
                RunnerInstallToken.status == "issued",
            )
            .values(status="used", used_at=now)
            .execution_options(synchronize_session=False)
        )
        self._db.flush()
        return bool(result.rowcount and result.rowcount > 0)

    def _emit_registered_audit(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        install_token: str,
        issued_credential: IssuedRunnerCredential,
        normalized: _NormalizedRunnerMetadata,
    ) -> None:
        secret_fields = RunnerCredentialService.build_masked_log_fields(
            install_token=install_token,
            runner_secret=issued_credential.plaintext_secret,
            credential_fingerprint=issued_credential.credential_fingerprint,
        )

        self._audit.emit(
            event_type="runner.registered",
            tenant_id=tenant_id,
            runner_id=runner_id,
            metadata={
                "runner_name": normalized.runner_name,
                "runner_version": normalized.runner_version,
                "label_count": len(normalized.labels_json),
                "capability_count": (
                    len(normalized.capabilities_json)
                    if isinstance(normalized.capabilities_json, list)
                    else len(normalized.capabilities_json)
                ),
                **secret_fields,
            },
        )

    def _transaction_context(self) -> AbstractContextManager[object]:
        if self._db.in_transaction():
            return self._db.begin_nested()
        return self._db.begin()

    def _now(self) -> datetime:
        value = self._now_provider()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _NormalizedRunnerMetadata:
    runner_name: str
    runner_version: str | None
    labels_json: dict[str, str]
    capabilities_json: list[str] | dict[str, str]


def _normalize_registration_request(request: RunnerRegistrationRequest) -> _NormalizedRunnerMetadata:
    runner_name = _normalize_required_text(
        request.runner_name,
        field_name="runner_name",
        max_length=MAX_RUNNER_NAME_LENGTH,
    )
    runner_version = _normalize_optional_text(
        request.runner_version,
        field_name="runner_version",
        max_length=MAX_RUNNER_VERSION_LENGTH,
    )
    labels_json = _normalize_label_map(request.labels)
    capabilities_json = _normalize_capabilities(request.capabilities)

    return _NormalizedRunnerMetadata(
        runner_name=runner_name,
        runner_version=runner_version,
        labels_json=labels_json,
        capabilities_json=capabilities_json,
    )


def _normalize_label_map(raw: Mapping[str, object] | None) -> dict[str, str]:
    if raw is None:
        return {}
    if len(raw) > MAX_LABEL_COUNT:
        raise ValueError("Runner metadata contains too many labels.")

    normalized: dict[str, str] = {}
    for key, value in raw.items():
        norm_key = _normalize_required_text(
            key,
            field_name="labels.key",
            max_length=MAX_LABEL_KEY_LENGTH,
        ).lower()
        norm_value = _normalize_required_text(
            value,
            field_name="labels.value",
            max_length=MAX_LABEL_VALUE_LENGTH,
        )
        normalized[norm_key] = norm_value

    return normalized


def _normalize_capabilities(
    raw: Sequence[object] | Mapping[str, object] | None,
) -> list[str] | dict[str, str]:
    if raw is None:
        return []

    if isinstance(raw, Mapping):
        if len(raw) > MAX_CAPABILITY_COUNT:
            raise ValueError("Runner metadata contains too many capabilities.")
        normalized_map: dict[str, str] = {}
        for key, value in raw.items():
            norm_key = _normalize_required_text(
                key,
                field_name="capabilities.key",
                max_length=MAX_CAPABILITY_LENGTH,
            ).lower()
            norm_value = _normalize_required_text(
                value,
                field_name="capabilities.value",
                max_length=MAX_CAPABILITY_LENGTH,
            )
            normalized_map[norm_key] = norm_value
        return normalized_map

    if isinstance(raw, (str, bytes)):
        raise ValueError("Runner capabilities must be a list or object.")

    if len(raw) > MAX_CAPABILITY_COUNT:
        raise ValueError("Runner metadata contains too many capabilities.")

    normalized_items: list[str] = []
    for item in raw:
        normalized_item = _normalize_required_text(
            item,
            field_name="capabilities.item",
            max_length=MAX_CAPABILITY_LENGTH,
        ).lower()
        if normalized_item not in normalized_items:
            normalized_items.append(normalized_item)

    return normalized_items


def _normalize_required_text(value: object, *, field_name: str, max_length: int) -> str:
    normalized = str(value if value is not None else "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required.")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds max length {max_length}.")
    return normalized


def _normalize_optional_text(value: object | None, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds max length {max_length}.")
    return normalized
