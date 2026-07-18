"""Legacy provider reconciliation and deployment identity backfill service.

This service is the only backend LLM provider service that treats legacy
`UserSettings.openai_*` fields as migration inputs. It copies encrypted
OpenAI key ciphertext directly and never calls plaintext credential writers
with legacy ciphertext.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
)
from agent.providers.llm.profiles.registry import (
    OPENAI_API_SURFACE_RESPONSES,
    OPENAI_DEFAULT_MODEL_ID,
    require_model_profile,
)

from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
    UserSettings,
)

logger = logging.getLogger(__name__)

LEGACY_DEPLOYMENT_BACKFILL_NAMESPACE = UUID(
    "155b4c21-9f15-4c52-bfec-7fbf407bc63d"
)
_SUPPORTED_LEGACY_PROVIDERS = frozenset(
    {OPENAI_PROVIDER_ID, ANTHROPIC_PROVIDER_ID}
)
_RUNTIME_FAMILY_BY_PROVIDER = {
    OPENAI_PROVIDER_ID: "openai_native",
    ANTHROPIC_PROVIDER_ID: "anthropic_native",
}


@dataclass(slots=True)
class LLMDeploymentBackfillStats:
    """Safe aggregate counters for one deployment identity backfill run."""

    inspected_users: int = 0
    copied_credentials: int = 0
    created_legacy_selections: int = 0
    created_connections: int = 0
    created_deployments: int = 0
    mapped_selection_refs: int = 0
    skipped: int = 0
    unmapped: int = 0
    failed: int = 0

    @property
    def created(self) -> int:
        """Return the total number of newly persisted identity rows."""

        return (
            self.copied_credentials
            + self.created_legacy_selections
            + self.created_connections
            + self.created_deployments
        )

    def merge(self, other: "LLMDeploymentBackfillStats") -> None:
        """Add another result's counters to this aggregate."""

        for field_name in self.__dataclass_fields__:
            setattr(
                self,
                field_name,
                int(getattr(self, field_name)) + int(getattr(other, field_name)),
            )

    def to_dict(self) -> dict[str, int]:
        """Return a JSON-safe report containing no row values or secrets."""

        return {
            "created": self.created,
            "skipped": self.skipped,
            "unmapped": self.unmapped,
            "failed": self.failed,
            "inspected_users": self.inspected_users,
            "copied_credentials": self.copied_credentials,
            "created_legacy_selections": self.created_legacy_selections,
            "created_connections": self.created_connections,
            "created_deployments": self.created_deployments,
            "mapped_selection_refs": self.mapped_selection_refs,
        }


@dataclass(frozen=True, slots=True)
class _SelectionTarget:
    row: object
    deployment_field: str
    provider: str
    wire_model_id: str


class LLMProviderMigrationService:
    """Idempotent legacy OpenAI to provider-neutral row reconciliation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def backfill_legacy_openai_for_user(self, user_id: int) -> None:
        """Create missing provider-neutral OpenAI rows from legacy settings."""

        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        ).scalar_one_or_none()
        if settings is None:
            return

        self._backfill_credential(settings)
        self._backfill_selection(settings)
        self._db.flush()

    def backfill_all_legacy_openai(self) -> int:
        """Backfill all legacy settings rows and return the number inspected."""

        settings_rows = self._db.execute(select(UserSettings)).scalars().all()
        for settings in settings_rows:
            self._backfill_credential(settings)
            self._backfill_selection(settings)
        self._db.flush()
        return len(settings_rows)

    def backfill_deployment_identity_for_user(
        self,
        user_id: int,
    ) -> LLMDeploymentBackfillStats:
        """Map one user's legacy credentials and text selections exactly once."""

        owner_id = int(user_id)
        stats = LLMDeploymentBackfillStats(inspected_users=1)
        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == owner_id)
        ).scalar_one_or_none()
        if settings is not None:
            if self._backfill_credential(settings):
                stats.copied_credentials += 1
            if self._backfill_selection(settings, preserve_wire_model=True):
                stats.created_legacy_selections += 1
            self._db.flush()

        credentials = self._credential_groups(owner_id)
        connections: dict[str, LLMInferenceConnection] = {}
        for provider, provider_credentials in credentials.items():
            connection, created = self._ensure_legacy_connection(
                user_id=owner_id,
                provider=provider,
                credentials=provider_credentials,
            )
            if created:
                stats.created_connections += 1
            else:
                stats.skipped += 1
            connections[provider] = connection

        for target in self._selection_targets(owner_id):
            if getattr(target.row, target.deployment_field) is not None:
                stats.skipped += 1
                continue
            provider = _normalized_legacy_provider(target.provider)
            connection = connections.get(provider) if provider is not None else None
            if connection is None or not _valid_wire_model_id(target.wire_model_id):
                stats.unmapped += 1
                continue
            deployment = self._db.execute(
                select(LLMModelDeployment).where(
                    LLMModelDeployment.connection_id == connection.id,
                    LLMModelDeployment.wire_model_id == target.wire_model_id,
                )
            ).scalar_one_or_none()
            if deployment is None:
                deployment = LLMModelDeployment(
                    id=deterministic_legacy_deployment_id(
                        connection.id,
                        target.wire_model_id,
                    ),
                    connection_id=connection.id,
                    wire_model_id=target.wire_model_id,
                    canonical_model_id=None,
                    display_name=target.wire_model_id,
                    discovery_source="legacy_backfill",
                    source_metadata=None,
                    lifecycle_state="active",
                    availability_state="unknown",
                    enabled=True,
                    revision=1,
                )
                self._db.add(deployment)
                self._db.flush()
                stats.created_deployments += 1
            setattr(target.row, target.deployment_field, deployment.id)
            stats.mapped_selection_refs += 1

        self._db.flush()
        return stats

    def backfill_all_deployment_identity(
        self,
        *,
        continue_on_error: bool = False,
    ) -> LLMDeploymentBackfillStats:
        """Backfill all candidate users in deterministic user-id order."""

        aggregate = LLMDeploymentBackfillStats()
        for user_id in self._candidate_user_ids():
            if not continue_on_error:
                aggregate.merge(
                    self.backfill_deployment_identity_for_user(user_id)
                )
                continue
            try:
                with self._db.begin_nested():
                    result = self.backfill_deployment_identity_for_user(user_id)
            except Exception:
                logger.warning(
                    "LLM deployment identity backfill failed for user_id=%s",
                    user_id,
                )
                aggregate.inspected_users += 1
                aggregate.failed += 1
            else:
                aggregate.merge(result)
        return aggregate

    def ensure_legacy_default_connection_for_provider(
        self,
        *,
        user_id: int,
        provider: str,
    ) -> LLMInferenceConnection | None:
        """Create only the designated connection for an existing credential."""

        owner_id = int(user_id)
        normalized_provider = _normalized_legacy_provider(provider)
        if normalized_provider is None:
            return None
        credentials = self._credential_groups(owner_id).get(normalized_provider)
        if not credentials:
            return None
        connection, _ = self._ensure_legacy_connection(
            user_id=owner_id,
            provider=normalized_provider,
            credentials=credentials,
        )
        return connection

    def _ensure_legacy_connection(
        self,
        *,
        user_id: int,
        provider: str,
        credentials: tuple[UserLLMProviderCredential, ...],
    ) -> tuple[LLMInferenceConnection, bool]:
        connection = self._db.execute(
            select(LLMInferenceConnection).where(
                LLMInferenceConnection.user_id == user_id,
                LLMInferenceConnection.legacy_default_provider == provider,
            )
        ).scalar_one_or_none()
        if connection is not None:
            return connection, False
        usable = any(
            bool(credential.enabled and credential.has_api_key)
            for credential in credentials
        )
        connection = LLMInferenceConnection(
            id=deterministic_legacy_connection_id(user_id, provider),
            user_id=user_id,
            display_name=f"Legacy {provider.title()}",
            connection_preset_id=provider,
            runtime_family_id=_RUNTIME_FAMILY_BY_PROVIDER[provider],
            serving_operator_id=provider,
            transport_origin="backend",
            endpoint_url=None,
            endpoint_policy_id="fixed_provider_v1",
            config_schema_version=1,
            non_secret_config=None,
            state="enabled" if usable else "disabled",
            revision=1,
            legacy_default_provider=provider,
        )
        self._db.add(connection)
        self._db.flush()
        return connection, True

    def _backfill_credential(self, settings: UserSettings) -> bool:
        encrypted_key = getattr(settings, "openai_api_key", None)
        if not isinstance(encrypted_key, str) or not encrypted_key.strip():
            return False

        existing = self._db.execute(
            select(UserLLMProviderCredential).where(
                UserLLMProviderCredential.user_id == settings.user_id,
                UserLLMProviderCredential.provider == OPENAI_PROVIDER_ID,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False

        self._db.add(
            UserLLMProviderCredential(
                user_id=settings.user_id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key=encrypted_key,
                enabled=True,
            )
        )
        return True

    def _backfill_selection(
        self,
        settings: UserSettings,
        *,
        preserve_wire_model: bool = False,
    ) -> bool:
        model = (getattr(settings, "openai_model", None) or "").strip()
        if not model:
            return False

        existing = self._db.execute(
            select(UserLLMSelection).where(UserLLMSelection.user_id == settings.user_id)
        ).scalar_one_or_none()
        if existing is not None:
            return False

        self._db.add(
            UserLLMSelection(
                user_id=settings.user_id,
                provider=OPENAI_PROVIDER_ID,
                model=(
                    model
                    if preserve_wire_model
                    else _normalize_legacy_openai_selection_model(model)
                ),
            )
        )
        return True

    def _credential_groups(
        self,
        user_id: int,
    ) -> dict[str, tuple[UserLLMProviderCredential, ...]]:
        rows = self._db.execute(
            select(UserLLMProviderCredential)
            .where(UserLLMProviderCredential.user_id == user_id)
            .order_by(
                UserLLMProviderCredential.provider.asc(),
                UserLLMProviderCredential.id.asc(),
            )
        ).scalars()
        grouped: dict[str, list[UserLLMProviderCredential]] = {}
        for row in rows:
            provider = _normalized_legacy_provider(row.provider)
            if provider is None:
                continue
            grouped.setdefault(provider, []).append(row)
        return {key: tuple(value) for key, value in grouped.items()}

    def _selection_targets(self, user_id: int) -> tuple[_SelectionTarget, ...]:
        targets: list[_SelectionTarget] = []
        conversation = self._db.execute(
            select(UserLLMSelection).where(UserLLMSelection.user_id == user_id)
        ).scalar_one_or_none()
        if conversation is not None:
            targets.append(
                _SelectionTarget(
                    row=conversation,
                    deployment_field="deployment_id",
                    provider=conversation.provider,
                    wire_model_id=conversation.model,
                )
            )
        reporting = self._db.execute(
            select(UserReportingLLMSelection).where(
                UserReportingLLMSelection.user_id == user_id
            )
        ).scalar_one_or_none()
        if reporting is not None:
            targets.append(
                _SelectionTarget(
                    row=reporting,
                    deployment_field="deployment_id",
                    provider=reporting.provider,
                    wire_model_id=reporting.model,
                )
            )
        memory = self._db.execute(
            select(UserMemoryLLMSelection).where(
                UserMemoryLLMSelection.user_id == user_id
            )
        ).scalar_one_or_none()
        if memory is not None:
            targets.extend(
                (
                    _SelectionTarget(
                        row=memory,
                        deployment_field="gate_deployment_id",
                        provider=memory.provider,
                        wire_model_id=memory.gate_model,
                    ),
                    _SelectionTarget(
                        row=memory,
                        deployment_field="extraction_deployment_id",
                        provider=memory.provider,
                        wire_model_id=memory.extraction_model,
                    ),
                )
            )
        return tuple(targets)

    def _candidate_user_ids(self) -> tuple[int, ...]:
        user_ids: set[int] = set()
        for model in (
            UserSettings,
            UserLLMProviderCredential,
            UserLLMSelection,
            UserReportingLLMSelection,
            UserMemoryLLMSelection,
        ):
            user_ids.update(
                int(value)
                for value in self._db.execute(select(model.user_id)).scalars()
            )
        return tuple(sorted(user_ids))


def deterministic_legacy_connection_id(user_id: int, provider: str) -> UUID:
    """Return the stable UUID for one user's legacy provider connection."""

    normalized_provider = _normalized_legacy_provider(provider)
    if normalized_provider is None:
        raise ValueError("Unsupported legacy provider")
    return uuid5(
        LEGACY_DEPLOYMENT_BACKFILL_NAMESPACE,
        f"legacy-connection:{int(user_id)}:{normalized_provider}",
    )


def deterministic_legacy_deployment_id(
    connection_id: UUID | str,
    wire_model_id: str,
) -> UUID:
    """Return the stable UUID for one exact connection wire-model identity."""

    return uuid5(
        LEGACY_DEPLOYMENT_BACKFILL_NAMESPACE,
        f"legacy-deployment:{UUID(str(connection_id))}:{wire_model_id}",
    )


def _normalized_legacy_provider(provider: str) -> str | None:
    if not isinstance(provider, str):
        return None
    normalized = provider.strip().lower()
    return normalized if normalized in _SUPPORTED_LEGACY_PROVIDERS else None


def _valid_wire_model_id(model: str) -> bool:
    return isinstance(model, str) and bool(model.strip()) and len(model) <= 512


def _normalize_legacy_openai_selection_model(model: str) -> str:
    """Normalize legacy OpenAI model settings during explicit backfill."""

    normalized = model.strip().lower()
    try:
        profile = require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, normalized))
    except LLMProfileNotFoundError:
        return OPENAI_DEFAULT_MODEL_ID
    if profile.api_surface != OPENAI_API_SURFACE_RESPONSES:
        return OPENAI_DEFAULT_MODEL_ID
    return profile.ref.model


__all__ = [
    "LEGACY_DEPLOYMENT_BACKFILL_NAMESPACE",
    "LLMDeploymentBackfillStats",
    "LLMProviderMigrationService",
    "deterministic_legacy_connection_id",
    "deterministic_legacy_deployment_id",
]
