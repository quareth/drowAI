"""Resolve explicit memory embedding and memory LLM runtime selections."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.core.identity import (
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    normalize_model_id,
    normalize_provider_id,
)
from agent.providers.llm.profiles.registry import require_model_profile

from backend.models.llm import UserEmbeddingSelection, UserMemoryLLMSelection
from backend.services.llm_provider.selection_deployment_resolver import (
    LLMSelectionDeploymentResolver,
)
from backend.services.llm_provider.migration_service import LLMProviderMigrationService
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMCredentialRef,
    ProviderConfigurationError,
)

from .profiles import DEFAULT_OPENAI_EMBEDDING_MODEL, require_embedding_profile

DEFAULT_MEMORY_GATE_MODEL = "gpt-5-nano"
DEFAULT_MEMORY_EXTRACTION_MODEL = "gpt-5-mini"


@dataclass(frozen=True, slots=True)
class EmbeddingRuntimeSelection:
    """Runtime embedding dependency selected independently from chat."""

    provider: str
    model: str
    credential_ref: LLMCredentialRef
    dimensions: int
    vector_family: str


@dataclass(frozen=True, slots=True)
class MemoryLLMRuntimeSelection:
    """Runtime memory LLM dependency selected independently from chat."""

    provider: str
    gate_model: str
    extraction_model: str
    credential_ref: LLMCredentialRef
    gate_deployment_ref: DeploymentRef | None = None
    extraction_deployment_ref: DeploymentRef | None = None


class EmbeddingRuntimeSelectionService:
    """Resolve durable embedding and memory text-LLM selections."""

    def __init__(
        self,
        *,
        credential_ref_resolver: Callable[[int, str], LLMCredentialRef],
        env_getter: Callable[[str, str | None], str | None] = os.getenv,
        db: Session | None = None,
    ) -> None:
        self._credential_ref_resolver = credential_ref_resolver
        self._env_getter = env_getter
        self._db = db
        self._deployment_resolver = (
            LLMSelectionDeploymentResolver(db) if db is not None else None
        )
        self._migration = LLMProviderMigrationService(db) if db is not None else None

    def get_embedding_selection(self, *, user_id: int) -> UserEmbeddingSelection:
        """Return or create the durable memory embedding selection for a user."""

        if self._db is None:
            raise RuntimeError("Durable embedding selection requires a database session")
        row = self._get_embedding_row(user_id)
        if row is None:
            provider, model = self._default_embedding_provider_model()
            profile = require_embedding_profile(provider, model)
            row = UserEmbeddingSelection(
                user_id=int(user_id),
                provider=profile.ref.provider,
                model=profile.ref.model,
                dimensions=profile.dimensions,
                vector_family=profile.vector_family,
            )
            self._db.add(row)
            self._db.flush()
            return row

        profile = require_embedding_profile(row.provider, row.model)
        if (
            row.provider != profile.ref.provider
            or row.model != profile.ref.model
            or int(row.dimensions) != profile.dimensions
            or row.vector_family != profile.vector_family
        ):
            row.provider = profile.ref.provider
            row.model = profile.ref.model
            row.dimensions = profile.dimensions
            row.vector_family = profile.vector_family
            self._db.flush()
        return row

    def set_embedding_selection(
        self,
        *,
        user_id: int,
        provider: str,
        model: str,
    ) -> UserEmbeddingSelection:
        """Persist a user's memory embedding provider/model selection."""

        if self._db is None:
            raise RuntimeError("Durable embedding selection requires a database session")
        profile = require_embedding_profile(provider, model)
        row = self._get_embedding_row(user_id)
        if row is None:
            row = UserEmbeddingSelection(user_id=int(user_id))
            self._db.add(row)
        row.provider = profile.ref.provider
        row.model = profile.ref.model
        row.dimensions = profile.dimensions
        row.vector_family = profile.vector_family
        self._db.flush()
        return row

    def get_memory_llm_selection(self, *, user_id: int) -> UserMemoryLLMSelection:
        """Return or create the durable memory LLM dependency selection."""

        if self._db is None:
            raise RuntimeError("Durable memory LLM selection requires a database session")
        row = self._get_memory_llm_row(user_id)
        if row is None:
            provider, gate_model, extraction_model = self._default_memory_llm_provider_models()
            gate_model, extraction_model = _validate_memory_llm_selection(
                provider,
                gate_model,
                extraction_model,
            )
            row = UserMemoryLLMSelection(
                user_id=int(user_id),
                provider=OPENAI_PROVIDER_ID,
                gate_model=gate_model,
                extraction_model=extraction_model,
            )
            self._db.add(row)
            self._db.flush()
            return row

        if row.gate_deployment_id is not None or row.extraction_deployment_id is not None:
            if (
                row.gate_deployment_id is None
                or row.extraction_deployment_id is None
                or self._deployment_resolver is None
            ):
                raise ProviderConfigurationError(
                    "Memory LLM deployment selection is incomplete"
                )
            gate = self._deployment_resolver.resolve(
                user_id=user_id,
                deployment_id=row.gate_deployment_id,
                role="memory gate",
                require_structured_output=True,
            )
            extraction = self._deployment_resolver.resolve(
                user_id=user_id,
                deployment_id=row.extraction_deployment_id,
                role="memory extraction",
                require_structured_output=True,
            )
            if gate.provider != extraction.provider:
                raise ProviderConfigurationError(
                    "Memory gate and extraction deployments must use the same provider"
                )
            if (
                row.provider != gate.provider
                or row.gate_model != gate.model
                or row.extraction_model != extraction.model
            ):
                row.provider = gate.provider
                row.gate_model = gate.model
                row.extraction_model = extraction.model
                self._db.flush()
            return row

        if self._migration is not None:
            self._migration.backfill_deployment_identity_for_user(int(user_id))
            if row.gate_deployment_id is not None and row.extraction_deployment_id is not None:
                return self.get_memory_llm_selection(user_id=user_id)

        gate_model, extraction_model = _validate_memory_llm_selection(
            row.provider,
            row.gate_model,
            row.extraction_model,
        )
        if (
            row.provider != OPENAI_PROVIDER_ID
            or row.gate_model != gate_model
            or row.extraction_model != extraction_model
        ):
            row.provider = OPENAI_PROVIDER_ID
            row.gate_model = gate_model
            row.extraction_model = extraction_model
            self._db.flush()
        return row

    def set_memory_llm_selection(
        self,
        *,
        user_id: int,
        provider: str,
        gate_model: str,
        extraction_model: str,
    ) -> UserMemoryLLMSelection:
        """Persist a user's memory LLM provider/model selection."""

        if self._db is None:
            raise RuntimeError("Durable memory LLM selection requires a database session")
        normalized_gate_model, normalized_extraction_model = _validate_memory_llm_selection(
            provider,
            gate_model,
            extraction_model,
        )
        if self._migration is None:
            raise RuntimeError("Durable memory LLM selection requires a database session")
        gate_deployment = self._migration.ensure_legacy_default_deployment_for_model(
            user_id=int(user_id),
            provider=OPENAI_PROVIDER_ID,
            wire_model_id=normalized_gate_model,
        )
        extraction_deployment = self._migration.ensure_legacy_default_deployment_for_model(
            user_id=int(user_id),
            provider=OPENAI_PROVIDER_ID,
            wire_model_id=normalized_extraction_model,
        )
        if gate_deployment is None or extraction_deployment is None:
            raise ProviderConfigurationError(
                "Memory LLM selection requires deployment bindings"
            )
        row = self._get_memory_llm_row(user_id)
        if row is None:
            row = UserMemoryLLMSelection(user_id=int(user_id))
            self._db.add(row)
        row.provider = OPENAI_PROVIDER_ID
        row.gate_model = normalized_gate_model
        row.extraction_model = normalized_extraction_model
        row.gate_deployment_id = gate_deployment.id
        row.extraction_deployment_id = extraction_deployment.id
        self._db.flush()
        return row

    def set_memory_llm_deployment_selection(
        self,
        *,
        user_id: int,
        gate_deployment_id: str,
        expected_gate_revision: int,
        extraction_deployment_id: str,
        expected_extraction_revision: int,
    ) -> UserMemoryLLMSelection:
        """Persist compatible gate/extraction deployment refs and snapshots."""

        if self._db is None or self._deployment_resolver is None:
            raise RuntimeError("Durable memory LLM selection requires a database session")
        gate = self._deployment_resolver.resolve(
            user_id=user_id,
            deployment_id=gate_deployment_id,
            expected_revision=expected_gate_revision,
            role="memory gate",
            require_structured_output=True,
        )
        extraction = self._deployment_resolver.resolve(
            user_id=user_id,
            deployment_id=extraction_deployment_id,
            expected_revision=expected_extraction_revision,
            role="memory extraction",
            require_structured_output=True,
        )
        if gate.provider != extraction.provider:
            raise ProviderConfigurationError(
                "Memory gate and extraction deployments must use the same provider"
            )
        row = self._get_memory_llm_row(user_id)
        if row is None:
            row = UserMemoryLLMSelection(user_id=int(user_id))
            self._db.add(row)
        row.provider = gate.provider
        row.gate_model = gate.model
        row.extraction_model = extraction.model
        row.gate_deployment_id = gate.deployment.id
        row.extraction_deployment_id = extraction.deployment.id
        self._db.flush()
        return row

    def resolve_embedding_selection(self, *, user_id: int) -> EmbeddingRuntimeSelection:
        """Resolve the embedding dependency for semantic memory."""

        if self._db is not None:
            try:
                row = self.get_embedding_selection(user_id=int(user_id))
                provider = row.provider
                model = row.model
            except SQLAlchemyError:
                provider, model = self._default_embedding_provider_model()
        else:
            provider, model = self._default_embedding_provider_model()
        profile = require_embedding_profile(provider, model)
        return EmbeddingRuntimeSelection(
            provider=profile.ref.provider,
            model=profile.ref.model,
            credential_ref=self._credential_ref_resolver(int(user_id), profile.ref.provider),
            dimensions=profile.dimensions,
            vector_family=profile.vector_family,
        )

    def resolve_memory_llm_selection(self, *, user_id: int) -> MemoryLLMRuntimeSelection:
        """Resolve legacy or deployment-backed memory LLM dependencies."""

        row = None
        if self._db is not None:
            try:
                row = self.get_memory_llm_selection(user_id=int(user_id))
                provider = row.provider
                gate_model = row.gate_model
                extraction_model = row.extraction_model
            except SQLAlchemyError:
                provider, gate_model, extraction_model = self._default_memory_llm_provider_models()
        else:
            provider, gate_model, extraction_model = self._default_memory_llm_provider_models()
        gate_ref = None
        extraction_ref = None
        if (
            self._db is not None
            and self._deployment_resolver is not None
            and row is not None
            and row.gate_deployment_id is not None
            and row.extraction_deployment_id is not None
        ):
            gate = self._deployment_resolver.resolve(
                user_id=user_id,
                deployment_id=row.gate_deployment_id,
                role="memory gate",
                require_structured_output=True,
            )
            extraction = self._deployment_resolver.resolve(
                user_id=user_id,
                deployment_id=row.extraction_deployment_id,
                role="memory extraction",
                require_structured_output=True,
            )
            if gate.provider != extraction.provider:
                raise ProviderConfigurationError(
                    "Memory gate and extraction deployments must use the same provider"
                )
            provider = gate.provider
            gate_model = gate.model
            extraction_model = extraction.model
            gate_ref = DeploymentRef(str(gate.deployment.id), int(gate.deployment.revision))
            extraction_ref = DeploymentRef(
                str(extraction.deployment.id), int(extraction.deployment.revision)
            )
        else:
            if row is not None:
                raise ProviderConfigurationError(
                    "Memory LLM selection has no deployment binding"
                )
            gate_model, extraction_model = _validate_memory_llm_selection(
                provider,
                gate_model,
                extraction_model,
            )
        return MemoryLLMRuntimeSelection(
            provider=provider,
            gate_model=gate_model,
            extraction_model=extraction_model,
            credential_ref=self._credential_ref_resolver(int(user_id), provider),
            gate_deployment_ref=gate_ref,
            extraction_deployment_ref=extraction_ref,
        )

    def _get_embedding_row(self, user_id: int) -> UserEmbeddingSelection | None:
        if self._db is None:
            return None
        return self._db.execute(
            select(UserEmbeddingSelection).where(UserEmbeddingSelection.user_id == int(user_id))
        ).scalar_one_or_none()

    def _get_memory_llm_row(self, user_id: int) -> UserMemoryLLMSelection | None:
        if self._db is None:
            return None
        return self._db.execute(
            select(UserMemoryLLMSelection).where(UserMemoryLLMSelection.user_id == int(user_id))
        ).scalar_one_or_none()

    def _default_embedding_provider_model(self) -> tuple[str, str]:
        provider = normalize_provider_id(
            self._env_getter("MEMORY_EMBEDDING_PROVIDER", OPENAI_PROVIDER_ID)
            or OPENAI_PROVIDER_ID
        )
        model = (
            self._env_getter("MEMORY_EMBEDDING_MODEL", DEFAULT_OPENAI_EMBEDDING_MODEL)
            or DEFAULT_OPENAI_EMBEDDING_MODEL
        )
        return provider, model

    def _default_memory_llm_provider_models(self) -> tuple[str, str, str]:
        provider = normalize_provider_id(
            self._env_getter("MEMORY_LLM_PROVIDER", OPENAI_PROVIDER_ID)
            or OPENAI_PROVIDER_ID
        )
        gate_model = (
            self._env_getter("MEMORY_EXTRACTION_GATE_MODEL", DEFAULT_MEMORY_GATE_MODEL)
            or DEFAULT_MEMORY_GATE_MODEL
        )
        extraction_model = (
            self._env_getter("MEMORY_EXTRACTION_MODEL", DEFAULT_MEMORY_EXTRACTION_MODEL)
            or DEFAULT_MEMORY_EXTRACTION_MODEL
        )
        return provider, gate_model, extraction_model


def _validate_memory_llm_selection(
    provider: str,
    gate_model: str,
    extraction_model: str,
) -> tuple[str, str]:
    normalized_provider = normalize_provider_id(provider)
    if normalized_provider != OPENAI_PROVIDER_ID:
        raise ValueError(f"Unsupported memory LLM provider: {normalized_provider}")

    try:
        gate_profile = require_model_profile(
            ProviderModelRef(
                provider=OPENAI_PROVIDER_ID,
                model=normalize_model_id(gate_model),
            )
        )
        extraction_profile = require_model_profile(
            ProviderModelRef(
                provider=OPENAI_PROVIDER_ID,
                model=normalize_model_id(extraction_model),
            )
        )
    except (LLMProfileNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Unsupported memory LLM model selection") from exc
    return gate_profile.ref.model, extraction_profile.ref.model


__all__ = [
    "DEFAULT_MEMORY_EXTRACTION_MODEL",
    "DEFAULT_MEMORY_GATE_MODEL",
    "EmbeddingRuntimeSelection",
    "EmbeddingRuntimeSelectionService",
    "MemoryLLMRuntimeSelection",
]
