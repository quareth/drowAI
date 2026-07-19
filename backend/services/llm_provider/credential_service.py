"""Credential storage and resolution for provider-neutral LLM settings.

This service centralizes encryption, masking, legacy OpenAI mirrors, and
runtime credential authorization. Decrypted provider secrets are returned only
from explicit boundary methods and are never stored on long-lived service
objects.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, normalize_provider_id

from backend.models import (
    LLMInferenceConnection,
    Task,
    UserLLMProviderCredential,
    UserSettings,
)
from backend.config.generated_config import resolve_config_value, validate_encryption_key

from .catalog_service import LLMProviderCatalogService
from .migration_service import LLMProviderMigrationService
from .operation_registry import GPT_OSS_20B_PROVING_PRESET_ID
from .types import (
    CredentialAuthorizationError,
    CredentialEncryptionError,
    CredentialNotFoundError,
    CredentialStatus,
    LLMAuthMode,
    LLMConnectionCredentialRef,
    LLMCredentialRef,
    ProviderSecret,
    ResolvedAuth,
)

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_FILE = ".encryption_key"
_ENCRYPTION_KEY_CACHE: bytes | None = None


def get_encryption_key() -> bytes:
    """Get or create the persistent encryption key used for API-key storage."""

    global _ENCRYPTION_KEY_CACHE
    if _ENCRYPTION_KEY_CACHE:
        return _ENCRYPTION_KEY_CACHE

    if os.path.exists(_ENCRYPTION_KEY_FILE):
        try:
            with open(_ENCRYPTION_KEY_FILE, "rb") as file_obj:
                persisted_key = file_obj.read().strip()
            _ENCRYPTION_KEY_CACHE = validate_encryption_key(persisted_key)
            return _ENCRYPTION_KEY_CACHE
        except ValueError:
            logger.warning("Ignoring invalid persisted encryption key; resolving configured key")
        except Exception as exc:
            logger.error("Failed to read encryption key file: %s", exc)

    key = resolve_config_value("ENCRYPTION_KEY") or os.environ.get("ENCRYPTION_KEY")
    if key:
        try:
            key_bytes = validate_encryption_key(key)
            _ENCRYPTION_KEY_CACHE = key_bytes
            try:
                with open(_ENCRYPTION_KEY_FILE, "wb") as file_obj:
                    file_obj.write(key_bytes)
            except Exception as exc:
                logger.error("Failed to save encryption key: %s", exc)
            return key_bytes
        except Exception as exc:
            logger.error("Invalid encryption key from environment: %s", exc)

    new_key = Fernet.generate_key()
    _ENCRYPTION_KEY_CACHE = new_key
    try:
        with open(_ENCRYPTION_KEY_FILE, "wb") as file_obj:
            file_obj.write(new_key)
        logger.info("Generated and saved new encryption key for API key storage")
    except Exception as exc:
        logger.error("Failed to save encryption key: %s", exc)
    return new_key


def encrypt_api_key(api_key: str) -> str:
    """Encrypt plaintext provider credential material for storage."""

    if not api_key:
        return ""
    try:
        encrypted = Fernet(get_encryption_key()).encrypt(api_key.encode())
        return base64.urlsafe_b64encode(encrypted).decode()
    except Exception as exc:
        logger.error("Failed to encrypt provider credential: %s", exc)
        raise CredentialEncryptionError("Failed to encrypt provider credential") from exc


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt provider credential material for an approved boundary."""

    if not encrypted_key:
        return ""
    try:
        decrypted = Fernet(get_encryption_key()).decrypt(base64.urlsafe_b64decode(encrypted_key.encode()))
        return decrypted.decode()
    except Exception as exc:
        logger.error("Failed to decrypt provider credential: %s", exc)
        return ""


class LLMCredentialService:
    """Store, mask, migrate, and resolve provider credentials."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        migration_service: LLMProviderMigrationService | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._migration = migration_service or LLMProviderMigrationService(db)

    def get_masked_status(self, user_id: int, provider: str) -> CredentialStatus:
        """Return non-secret credential status for a user/provider pair."""

        normalized_provider = self._normalize_provider(provider)
        credential = self._get_credential(user_id=user_id, provider=normalized_provider, migrate=True)
        connection = self._get_legacy_default_connection(
            user_id=user_id,
            provider=normalized_provider,
            migrate=False,
        )
        if credential is None:
            return CredentialStatus(
                user_id=user_id,
                provider=normalized_provider,
                enabled=False,
                has_api_key=False,
                masked_api_key=None,
                connection_id=str(connection.id) if connection is not None else None,
                auth_mode=(
                    LLMAuthMode.API_KEY if connection is not None else None
                ),
            )
        return self._status_from_credential(credential, connection=connection)

    def has_enabled_credential(self, user_id: int, provider: str) -> bool:
        """Return True when an enabled credential exists for a provider."""

        credential = self._get_credential(
            user_id=user_id,
            provider=self._normalize_connection_provider(provider),
            migrate=True,
        )
        return bool(credential and credential.enabled and credential.has_api_key)

    def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        """Return a credential ref after proving an enabled credential exists."""

        normalized_provider = self._normalize_provider(provider)
        if not self.has_enabled_credential(user_id, normalized_provider):
            raise CredentialNotFoundError(f"{normalized_provider} credential is not configured")
        return LLMCredentialRef(user_id=user_id, provider=normalized_provider)

    def upsert_api_key(
        self,
        *,
        user_id: int,
        provider: str,
        api_key: str,
        enabled: bool = True,
        sync_legacy_openai: bool = True,
    ) -> CredentialStatus:
        """Encrypt and store a plaintext provider API key."""

        normalized_provider = self._normalize_provider(provider)
        plaintext = (api_key or "").strip()
        if not plaintext or not enabled:
            return self.disable(user_id=user_id, provider=normalized_provider)

        encrypted_key = encrypt_api_key(plaintext)
        credential = self._get_credential(user_id=user_id, provider=normalized_provider, migrate=False)
        if credential is None:
            credential = UserLLMProviderCredential(
                user_id=user_id,
                provider=normalized_provider,
                encrypted_api_key=encrypted_key,
                enabled=enabled,
            )
            self._db.add(credential)
        else:
            credential.encrypted_api_key = encrypted_key
            credential.enabled = enabled

        if sync_legacy_openai and normalized_provider == OPENAI_PROVIDER_ID:
            settings = self._get_or_create_user_settings(user_id)
            settings.openai_api_key = encrypted_key

        self._invalidate_decrypted_compat_cache(user_id)
        self._db.flush()
        connection = self._ensure_legacy_default_connection(
            user_id=user_id,
            provider=normalized_provider,
        )
        return self._status_from_credential(credential, connection=connection)

    def upsert_connection_api_key(
        self,
        *,
        user_id: int,
        connection_ref: LLMConnectionCredentialRef,
        provider: str,
        api_key: str,
    ) -> CredentialStatus:
        """Bind encrypted API-key material to one user-owned connection."""

        connection = self._authorize_connection_ref(
            connection_ref,
            runtime_user_id=user_id,
            task_id=None,
        )
        normalized_provider = self._normalize_connection_provider(provider)
        if (
            self._normalize_connection_provider(connection.connection_preset_id)
            != normalized_provider
        ):
            raise CredentialAuthorizationError(
                "Connection credential binding is invalid"
            )
        plaintext = (api_key or "").strip()
        if not plaintext:
            raise CredentialNotFoundError(
                f"{normalized_provider} credential is not configured"
            )

        encrypted_key = encrypt_api_key(plaintext)
        credential = self._get_credential(
            user_id=user_id,
            provider=normalized_provider,
            migrate=False,
        )
        if credential is None:
            credential = UserLLMProviderCredential(
                user_id=user_id,
                provider=normalized_provider,
                encrypted_api_key=encrypted_key,
                enabled=True,
            )
            self._db.add(credential)
        else:
            credential.encrypted_api_key = encrypted_key
            credential.enabled = True

        connection.legacy_default_provider = normalized_provider
        connection.revision += 1
        self._invalidate_decrypted_compat_cache(user_id)
        self._db.flush()
        return self._status_from_credential(credential, connection=connection)

    def connection_credential_fingerprint(
        self,
        *,
        user_id: int,
        connection_ref: LLMConnectionCredentialRef,
        provider: str,
    ) -> str:
        """Return a secret-safe fingerprint of the stored connection credential."""

        connection = self._authorize_connection_ref(
            connection_ref,
            runtime_user_id=user_id,
            task_id=None,
        )
        normalized_provider = self._normalize_connection_provider(provider)
        if (
            self._normalize_connection_provider(connection.connection_preset_id)
            != normalized_provider
        ):
            raise CredentialAuthorizationError(
                "Connection credential binding is invalid"
            )
        credential = self._get_credential(
            user_id=user_id,
            provider=normalized_provider,
            migrate=False,
        )
        if credential is None or not credential.enabled or not credential.encrypted_api_key:
            raise CredentialNotFoundError(
                f"{normalized_provider} credential is not configured"
            )
        return hashlib.sha256(
            str(credential.encrypted_api_key).encode("utf-8")
        ).hexdigest()

    def upsert_encrypted_api_key(
        self,
        *,
        user_id: int,
        provider: str,
        encrypted_api_key: str,
        enabled: bool = True,
        sync_legacy_openai: bool = False,
    ) -> CredentialStatus:
        """Store already-encrypted credential material without re-encrypting it."""

        normalized_provider = self._normalize_provider(provider)
        encrypted = (encrypted_api_key or "").strip()
        if not encrypted or not enabled:
            return self.disable(user_id=user_id, provider=normalized_provider)

        credential = self._get_credential(user_id=user_id, provider=normalized_provider, migrate=False)
        if credential is None:
            credential = UserLLMProviderCredential(
                user_id=user_id,
                provider=normalized_provider,
                encrypted_api_key=encrypted,
                enabled=enabled,
            )
            self._db.add(credential)
        else:
            credential.encrypted_api_key = encrypted
            credential.enabled = enabled

        if sync_legacy_openai and normalized_provider == OPENAI_PROVIDER_ID:
            settings = self._get_or_create_user_settings(user_id)
            settings.openai_api_key = encrypted

        self._invalidate_decrypted_compat_cache(user_id)
        self._db.flush()
        connection = self._ensure_legacy_default_connection(
            user_id=user_id,
            provider=normalized_provider,
        )
        return self._status_from_credential(credential, connection=connection)

    def disable(self, *, user_id: int, provider: str) -> CredentialStatus:
        """Disable a provider credential and clear legacy OpenAI mirrors."""

        normalized_provider = self._normalize_provider(provider)
        credential = self._get_credential(user_id=user_id, provider=normalized_provider, migrate=False)
        if credential is None:
            credential = UserLLMProviderCredential(
                user_id=user_id,
                provider=normalized_provider,
                encrypted_api_key="",
                enabled=False,
            )
            self._db.add(credential)
        else:
            credential.enabled = False
            credential.encrypted_api_key = ""

        if normalized_provider == OPENAI_PROVIDER_ID:
            settings = self._get_or_create_user_settings(user_id)
            settings.openai_api_key = None

        self._invalidate_decrypted_compat_cache(user_id)
        self._db.flush()
        connection = self._ensure_legacy_default_connection(
            user_id=user_id,
            provider=normalized_provider,
        )
        return self._status_from_credential(credential, connection=connection)

    def delete(self, *, user_id: int, provider: str) -> None:
        """Delete a provider credential and clear legacy OpenAI mirrors."""

        normalized_provider = self._normalize_provider(provider)
        credential = self._get_credential(user_id=user_id, provider=normalized_provider, migrate=False)
        if credential is not None:
            self._ensure_legacy_default_connection(
                user_id=user_id,
                provider=normalized_provider,
            )
            self._db.delete(credential)

        if normalized_provider == OPENAI_PROVIDER_ID:
            settings = self._get_or_create_user_settings(user_id)
            settings.openai_api_key = None

        self._invalidate_decrypted_compat_cache(user_id)
        self._db.flush()

    def resolve_connection_auth(
        self,
        connection_ref: LLMConnectionCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None = None,
        purpose: str,
        auth_mode: LLMAuthMode | str,
    ) -> ResolvedAuth:
        """Resolve typed auth after revalidating a live connection reference."""

        connection = self._authorize_connection_ref(
            connection_ref,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
        )
        try:
            mode = (
                auth_mode
                if isinstance(auth_mode, LLMAuthMode)
                else LLMAuthMode(str(auth_mode).strip().lower())
            )
        except ValueError as exc:
            raise CredentialAuthorizationError(
                "Connection auth mode is not permitted"
            ) from exc
        if mode is LLMAuthMode.NONE:
            return ResolvedAuth.none()
        if mode is LLMAuthMode.OPERATOR_MANAGED:
            return ResolvedAuth.operator_managed()

        provider = connection.legacy_default_provider
        if not provider:
            raise CredentialAuthorizationError(
                "Connection has no explicitly bound local credential"
            )
        normalized_provider = self._normalize_connection_provider(provider)
        if (
            self._normalize_connection_provider(connection.connection_preset_id)
            != normalized_provider
        ):
            raise CredentialAuthorizationError(
                "Connection credential binding is invalid"
            )
        credential = self._get_credential(
            user_id=runtime_user_id,
            provider=normalized_provider,
            migrate=False,
        )
        if credential is None or not credential.enabled or not credential.has_api_key:
            raise CredentialNotFoundError(
                f"{normalized_provider} credential is not configured"
            )
        secret_value = decrypt_api_key(credential.encrypted_api_key)
        if not secret_value:
            raise CredentialNotFoundError(
                f"{normalized_provider} credential could not be decrypted"
            )
        logger.debug(
            "Resolved connection credential connection_id=%s mode=%s",
            connection.id,
            mode.value,
        )
        secret = ProviderSecret(provider=normalized_provider, value=secret_value)
        return ResolvedAuth.with_secret(
            mode=mode,
            provider=normalized_provider,
            secret=secret,
        )

    def resolve_secret(
        self,
        credential_ref: LLMCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None = None,
        purpose: str,
    ) -> ProviderSecret:
        """Resolve a credential ref to a short-lived decrypted provider secret."""

        if credential_ref.user_id != runtime_user_id:
            raise CredentialAuthorizationError("Credential ref user does not match runtime user")

        if task_id is not None:
            owns_task = self._db.execute(
                select(Task.id).where(Task.id == task_id, Task.user_id == runtime_user_id)
            ).scalar_one_or_none()
            if owns_task is None:
                raise CredentialAuthorizationError("Credential ref is not authorized for this task")

        normalized_provider = self._normalize_provider(credential_ref.provider)
        credential = self._get_credential(
            user_id=credential_ref.user_id,
            provider=normalized_provider,
            migrate=True,
        )
        if credential is None or not credential.enabled or not credential.has_api_key:
            raise CredentialNotFoundError(f"{normalized_provider} credential is not configured")

        secret = decrypt_api_key(credential.encrypted_api_key)
        if not secret:
            raise CredentialNotFoundError(f"{normalized_provider} credential could not be decrypted")

        logger.debug(
            "Resolved provider credential for provider=%s user_id=%s",
            normalized_provider,
            runtime_user_id,
        )
        return ProviderSecret(provider=normalized_provider, value=secret)

    def get_openai_api_key_compat(self, user_id: int) -> str:
        """Compatibility helper for old OpenAI key callers."""

        try:
            ref = self.get_credential_ref(user_id, OPENAI_PROVIDER_ID)
            return self.resolve_secret(
                ref,
                runtime_user_id=user_id,
                task_id=None,
                purpose="legacy_openai_compat",
            ).value
        except Exception as exc:
            logger.error("Failed to resolve OpenAI credential for user %s: %s", user_id, exc)
            return ""

    def _get_credential(
        self,
        *,
        user_id: int,
        provider: str,
        migrate: bool,
    ) -> UserLLMProviderCredential | None:
        if migrate:
            self._migration.backfill_deployment_identity_for_user(user_id)

        connection = self._get_legacy_default_connection(
            user_id=user_id,
            provider=provider,
            migrate=False,
        )
        if migrate and connection is None:
            return None

        credential = self._db.execute(
            select(UserLLMProviderCredential).where(
                UserLLMProviderCredential.user_id == user_id,
                UserLLMProviderCredential.provider == provider,
            )
        ).scalar_one_or_none()
        return credential

    def _ensure_legacy_default_connection(
        self,
        *,
        user_id: int,
        provider: str,
    ) -> LLMInferenceConnection:
        connection = self._migration.ensure_legacy_default_connection_for_provider(
            user_id=user_id,
            provider=provider,
        )
        if connection is None:
            raise CredentialAuthorizationError(
                "Legacy credential has no explicit default connection"
            )
        return connection

    def _get_legacy_default_connection(
        self,
        *,
        user_id: int,
        provider: str,
        migrate: bool,
    ) -> LLMInferenceConnection | None:
        if migrate:
            self._migration.backfill_deployment_identity_for_user(user_id)
        return self._db.execute(
            select(LLMInferenceConnection).where(
                LLMInferenceConnection.user_id == user_id,
                LLMInferenceConnection.legacy_default_provider == provider,
            )
        ).scalar_one_or_none()

    def _authorize_connection_ref(
        self,
        connection_ref: LLMConnectionCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None,
    ) -> LLMInferenceConnection:
        if not isinstance(connection_ref, LLMConnectionCredentialRef):
            raise TypeError("connection_ref must be LLMConnectionCredentialRef")
        if (
            isinstance(runtime_user_id, bool)
            or not isinstance(runtime_user_id, int)
            or runtime_user_id <= 0
        ):
            raise CredentialAuthorizationError(
                "Connection credential ref is unavailable"
            )
        if task_id is not None and (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id <= 0
        ):
            raise CredentialAuthorizationError(
                "Connection credential ref is not authorized for this task"
            )
        try:
            connection_id = UUID(str(connection_ref.connection_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise CredentialAuthorizationError(
                "Connection credential ref is unavailable"
            ) from exc
        connection = self._db.execute(
            select(LLMInferenceConnection)
            .where(LLMInferenceConnection.id == connection_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if connection is None or int(connection.user_id) != int(runtime_user_id):
            raise CredentialAuthorizationError(
                "Connection credential ref is unavailable"
            )
        if (
            isinstance(connection_ref.expected_revision, bool)
            or not isinstance(connection_ref.expected_revision, int)
            or int(connection.revision) != connection_ref.expected_revision
        ):
            raise CredentialAuthorizationError(
                "Connection credential ref revision is stale"
            )
        if task_id is not None:
            owns_task = self._db.execute(
                select(Task.id).where(
                    Task.id == task_id,
                    Task.user_id == runtime_user_id,
                )
            ).scalar_one_or_none()
            if owns_task is None:
                raise CredentialAuthorizationError(
                    "Connection credential ref is not authorized for this task"
                )
        return connection

    def _get_or_create_user_settings(self, user_id: int) -> UserSettings:
        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        ).scalar_one_or_none()
        if settings is None:
            settings = UserSettings(user_id=user_id)
            self._db.add(settings)
            self._db.flush()
        return settings

    def _normalize_provider(self, provider: str) -> str:
        normalized_provider = normalize_provider_id(provider)
        self._catalog.require_provider(normalized_provider)
        return normalized_provider

    def _normalize_connection_provider(self, provider: str) -> str:
        normalized_provider = normalize_provider_id(provider)
        if normalized_provider == GPT_OSS_20B_PROVING_PRESET_ID:
            return normalized_provider
        self._catalog.require_provider(normalized_provider)
        return normalized_provider

    @staticmethod
    def _status_from_credential(
        credential: UserLLMProviderCredential,
        *,
        connection: LLMInferenceConnection | None = None,
    ) -> CredentialStatus:
        return CredentialStatus(
            user_id=credential.user_id,
            provider=credential.provider,
            enabled=bool(credential.enabled),
            has_api_key=credential.has_api_key,
            masked_api_key="***" if credential.has_api_key else None,
            connection_id=str(connection.id) if connection is not None else None,
            auth_mode=(LLMAuthMode.API_KEY if connection is not None else None),
        )

    @staticmethod
    def _invalidate_decrypted_compat_cache(user_id: int) -> None:
        # Deprecated container-level decrypted credential cache is now a no-op;
        # keep this hook for call-site stability without importing runtime helpers.
        _ = user_id


__all__ = [
    "LLMCredentialService",
    "decrypt_api_key",
    "encrypt_api_key",
    "get_encryption_key",
]
