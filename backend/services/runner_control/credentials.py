"""Runner credential/token lifecycle service for runner control plane auth.

This module issues one-time runner install tokens and runner credentials,
verifies presented token/secret material using constant-time comparisons, and
enforces expiration/revocation policy without ever persisting plaintext values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
import hashlib
import secrets
from typing import Callable
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.models.runner_control import RunnerCredential, RunnerInstallToken

DEFAULT_INSTALL_TOKEN_TTL = timedelta(minutes=30)
DEFAULT_RUNNER_CREDENTIAL_TTL = timedelta(days=90)
_TOKEN_PREFIX = "rit_"
_SECRET_PREFIX = "rsec_"
_HASH_ALGO = "sha256"
_MISSING_TOKEN_HASH = f"{_HASH_ALGO}$" + ("0" * 64)


class RunnerCredentialServiceError(RuntimeError):
    """Base error for runner credential service operations."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class RunnerInstallTokenValidationError(RunnerCredentialServiceError):
    """Raised when install-token verification fails closed."""


class RunnerCredentialAuthError(RunnerCredentialServiceError):
    """Raised when runner credential authentication fails."""


@dataclass(frozen=True, slots=True)
class IssuedInstallToken:
    """Install token issuance result containing the one-time plaintext token."""

    install_token_id: UUID
    plaintext_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedRunnerCredential:
    """Runner credential issuance result containing the one-time plaintext secret."""

    credential_id: UUID
    runner_id: UUID
    credential_fingerprint: str
    plaintext_secret: str
    expires_at: datetime


class RunnerCredentialService:
    """Issue and validate runner install tokens and runner credentials."""

    def __init__(
        self,
        db: Session,
        *,
        install_token_ttl: timedelta = DEFAULT_INSTALL_TOKEN_TTL,
        credential_ttl: timedelta = DEFAULT_RUNNER_CREDENTIAL_TTL,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._install_token_ttl = install_token_ttl
        self._credential_ttl = credential_ttl
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def issue_install_token(
        self,
        *,
        tenant_id: int,
        execution_site_id: UUID,
        created_by_user_id: int | None,
        ttl: timedelta | None = None,
    ) -> IssuedInstallToken:
        """Create a one-time install token and store only its hash."""

        now = self._now()
        expires_at = now + (ttl if ttl is not None else self._install_token_ttl)
        plaintext_token = _TOKEN_PREFIX + secrets.token_urlsafe(32)

        record = RunnerInstallToken(
            tenant_id=tenant_id,
            execution_site_id=execution_site_id,
            token_hash=_hash_secret(plaintext_token),
            status="issued",
            expires_at=expires_at,
            created_by_user_id=created_by_user_id,
        )
        self._db.add(record)
        self._db.flush()

        return IssuedInstallToken(
            install_token_id=record.id,
            plaintext_token=plaintext_token,
            expires_at=expires_at,
        )

    def verify_install_token(
        self,
        *,
        tenant_id: int,
        plaintext_token: str,
    ) -> RunnerInstallToken:
        """Verify an install token and reject expired/used/revoked/wrong-tenant tokens."""
        record = self.verify_install_token_for_enrollment(plaintext_token=plaintext_token)
        if record.tenant_id != tenant_id:
            self._raise_install_token_invalid()
        return record

    def verify_install_token_for_enrollment(self, *, plaintext_token: str) -> RunnerInstallToken:
        """Verify a one-time enrollment token and resolve its tenant-bound row."""
        normalized_token = (plaintext_token or "").strip()
        if not normalized_token:
            self._raise_install_token_invalid()

        candidate_hash = _hash_secret(normalized_token)
        record = self._db.execute(
            select(RunnerInstallToken)
            .where(RunnerInstallToken.token_hash == candidate_hash)
            .order_by(desc(RunnerInstallToken.created_at))
            .limit(1)
        ).scalar_one_or_none()

        stored_hash = str(record.token_hash) if record is not None else _MISSING_TOKEN_HASH

        # Always exercise constant-time comparison, including missing-token failures.
        if not compare_digest(stored_hash, candidate_hash):
            self._raise_install_token_invalid()

        if record is None:
            self._raise_install_token_invalid()

        if record.used_at is not None:
            self._raise_install_token_invalid()

        normalized_status = _normalize_status(record.status)
        if normalized_status in {"revoked", "used", "disabled"}:
            self._raise_install_token_invalid()

        if _as_utc(record.expires_at) <= self._now():
            self._raise_install_token_invalid()

        return record

    def mark_install_token_used(self, install_token: RunnerInstallToken) -> None:
        """Mark a verified install token as consumed."""

        install_token.status = "used"
        install_token.used_at = self._now()
        self._db.flush()

    def issue_runner_credential(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        ttl: timedelta | None = None,
    ) -> IssuedRunnerCredential:
        """Issue a new runner credential, returning plaintext only once."""

        now = self._now()
        expires_at = now + (ttl if ttl is not None else self._credential_ttl)
        plaintext_secret = _SECRET_PREFIX + secrets.token_urlsafe(48)

        credential = RunnerCredential(
            tenant_id=tenant_id,
            runner_id=runner_id,
            credential_fingerprint=_build_fingerprint(plaintext_secret),
            secret_hash=_hash_secret(plaintext_secret),
            status="active",
            expires_at=expires_at,
        )
        self._db.add(credential)
        self._db.flush()

        return IssuedRunnerCredential(
            credential_id=credential.id,
            runner_id=runner_id,
            credential_fingerprint=credential.credential_fingerprint,
            plaintext_secret=plaintext_secret,
            expires_at=expires_at,
        )

    def authenticate_runner_credential(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        plaintext_secret: str,
    ) -> RunnerCredential:
        """Authenticate runner secret and enforce stable revocation/expiry failures."""

        normalized_secret = (plaintext_secret or "").strip()
        if not normalized_secret:
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner credential is invalid.",
            )

        records = list(
            self._db.execute(
                select(RunnerCredential)
                .where(
                    RunnerCredential.tenant_id == tenant_id,
                    RunnerCredential.runner_id == runner_id,
                )
                .order_by(desc(RunnerCredential.created_at))
            ).scalars()
        )

        if not records:
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner credential is invalid.",
            )

        matched: RunnerCredential | None = None
        for candidate in records:
            if _verify_secret_hash(plaintext=normalized_secret, stored_hash=candidate.secret_hash):
                matched = candidate
                break

        if matched is None:
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner credential is invalid.",
            )

        self._ensure_runner_credential_active(matched)

        matched.last_used_at = self._now()
        self._db.flush()
        return matched

    def assert_runner_credential_active(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        credential_id: UUID,
    ) -> RunnerCredential:
        """Validate an issued credential row still authorizes the runner channel."""

        credential = self._db.execute(
            select(RunnerCredential).where(
                RunnerCredential.id == credential_id,
                RunnerCredential.tenant_id == tenant_id,
                RunnerCredential.runner_id == runner_id,
            )
        ).scalar_one_or_none()
        if credential is None:
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner credential is invalid.",
            )
        self._ensure_runner_credential_active(credential)
        return credential

    def revoke_runner_credential(self, credential: RunnerCredential) -> None:
        """Revoke a runner credential and stamp revocation time."""

        credential.status = "revoked"
        credential.revoked_at = self._now()
        self._db.flush()

    @staticmethod
    def mask_install_token(token: str | None) -> str:
        """Return a redacted token representation safe for logs."""

        return _mask_secret_material(token, empty_marker="<NO_INSTALL_TOKEN>")

    @staticmethod
    def mask_runner_secret(secret: str | None) -> str:
        """Return a redacted runner-secret representation safe for logs."""

        return _mask_secret_material(secret, empty_marker="<NO_RUNNER_SECRET>")

    @classmethod
    def build_masked_log_fields(
        cls,
        *,
        install_token: str | None = None,
        runner_secret: str | None = None,
        credential_fingerprint: str | None = None,
    ) -> dict[str, str]:
        """Build log-safe fields without exposing raw token/secret values."""

        fields = {
            "install_token": cls.mask_install_token(install_token),
            "runner_secret": cls.mask_runner_secret(runner_secret),
        }
        if credential_fingerprint:
            fields["credential_fingerprint"] = str(credential_fingerprint).strip()
        return fields

    def _raise_install_token_invalid(self) -> None:
        raise RunnerInstallTokenValidationError(
            error_code="RUNNER_INSTALL_TOKEN_INVALID",
            message="Runner install token is invalid.",
        )

    def _ensure_runner_credential_active(self, credential: RunnerCredential) -> None:
        if credential.revoked_at is not None or _normalize_status(credential.status) == "revoked":
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_REVOKED",
                message="Runner credential has been revoked.",
            )

        if credential.expires_at is not None and _as_utc(credential.expires_at) <= self._now():
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_EXPIRED",
                message="Runner credential has expired.",
            )

        if _normalize_status(credential.status) != "active":
            raise RunnerCredentialAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner credential is invalid.",
            )

    def _now(self) -> datetime:
        now = self._now_provider()
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _build_fingerprint(secret: str) -> str:
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return digest[:24]


def _hash_secret(secret: str) -> str:
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"{_HASH_ALGO}${digest}"


def _verify_secret_hash(*, plaintext: str, stored_hash: str) -> bool:
    expected_hash = _hash_secret(plaintext)
    return compare_digest(expected_hash, str(stored_hash or ""))


def _normalize_status(status: str | None) -> str:
    return str(status or "").strip().lower()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _mask_secret_material(value: str | None, *, empty_marker: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return empty_marker
    if len(normalized) <= 8:
        return "<MASKED>"
    return f"{normalized[:4]}...{normalized[-4:]}"
