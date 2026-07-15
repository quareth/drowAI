"""Backend-owned memory runtime boundary for provider-neutral LLM plumbing.

This module resolves semantic-memory embedding and memory LLM dependencies
explicitly, separate from the active chat provider. Decrypted provider secrets
stay inside backend runtime methods.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, NamedTuple

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.services.embeddings.factory import EmbeddingProviderFactory
from backend.services.embeddings.selection_service import (
    EmbeddingRuntimeSelection,
    EmbeddingRuntimeSelectionService,
    MemoryLLMRuntimeSelection,
)
from backend.services.llm_provider.runtime_client_resolver import LLMRuntimeClientResolver
from backend.services.llm_provider.types import (
    LLMCredentialRef,
    LLMCallTarget,
    LLMProviderServiceError,
    LLMRuntimeSelection,
)
from core.memory.retrieval_summary import render_memory_summary, split_retrieval_limits

from .memory_models import MemorySearchFilters, MemorySearchResult, MemoryTier

logger = logging.getLogger(__name__)

MEMORY_EXTRACTION_GATE_ROLE = "memory_extraction_gate"
MEMORY_EXTRACTION_ROLE = "memory_extraction"


class MemoryRuntimeService:
    """Resolve live memory LLM and embedding dependencies for one runtime."""

    def __init__(
        self,
        *,
        client_resolver: LLMRuntimeClientResolver,
        session_factory: Callable[[], Session] | None = None,
        env_getter: Callable[[str, str | None], str | None] = os.getenv,
        embedding_factory: EmbeddingProviderFactory | None = None,
        selection_service: EmbeddingRuntimeSelectionService | None = None,
    ) -> None:
        self._client_resolver = client_resolver
        self._session_factory = session_factory
        self._env_getter = env_getter
        self._embedding_factory = embedding_factory or EmbeddingProviderFactory()
        self._selection_service_injected = selection_service is not None
        self._selection_service = selection_service or EmbeddingRuntimeSelectionService(
            credential_ref_resolver=self._resolve_credential_ref,
            env_getter=env_getter,
        )

    async def retrieve_summary(
        self,
        *,
        selection: Mapping[str, Any],
        runtime_user_id: int,
        task_id: int | None,
        user_id: int,
        query: str,
        max_results: int,
        max_chars: int,
    ) -> str:
        """Retrieve a bounded semantic-memory summary using authorized runtime creds."""

        runtime_selection = LLMRuntimeSelection.from_mapping(selection)
        if (
            int(user_id) != int(runtime_user_id)
            or int(runtime_selection.credential_ref.user_id) != int(runtime_user_id)
        ):
            logger.warning("[MEMORY_RUNTIME] Refusing retrieval with mismatched user scope")
            return ""

        user_profile_max, engagement_max = split_retrieval_limits(max_results)
        if user_profile_max <= 0 and engagement_max <= 0:
            return ""

        db: Session | None = None
        try:
            from backend.database import SessionLocal
            from backend.services.memory.memory_store import MemoryStore

            db = self._open_session(SessionLocal)
            embedding_dependency = self._resolve_embedding_dependency(
                runtime_user_id=int(runtime_user_id),
                task_id=task_id,
                purpose="memory_retrieval_embedding",
                db=db,
            )
            if embedding_dependency is None:
                return ""
            _embedding_selection, embedding_provider = embedding_dependency
            memory_store = MemoryStore(db, embedding_provider)
            task_scope = _resolve_task_scope(db, task_id)
            engagement_id = task_scope.engagement_id
            tenant_id = task_scope.tenant_id

            tier_presence = await memory_store.get_candidate_tier_presence(
                user_id=int(user_id),
                tenant_id=int(tenant_id) if tenant_id is not None else None,
                engagement_id=int(engagement_id) if engagement_id is not None else None,
                task_id=int(task_id) if task_id is not None else None,
            )
            user_has_candidates = (
                tier_presence.get(MemoryTier.USER_PROFILE, False) and user_profile_max > 0
            )
            task_engagement_has_candidates = (
                tier_presence.get(MemoryTier.TASK_ENGAGEMENT, False)
                and tenant_id is not None
                and engagement_max > 0
            )
            if not user_has_candidates and not task_engagement_has_candidates:
                return ""

            query_embedding = await embedding_provider.embed(query)

            user_results: list[MemorySearchResult] = []
            if user_has_candidates:
                user_results = await memory_store.retrieve_with_embedding(
                    query_embedding,
                    MemorySearchFilters(
                        user_id=int(user_id),
                        memory_tier=MemoryTier.USER_PROFILE,
                        max_results=user_profile_max,
                    ),
                )

            engagement_results: list[MemorySearchResult] = []
            if task_engagement_has_candidates and task_id is not None and tenant_id is not None:
                engagement_results = await memory_store.retrieve_with_embedding(
                    query_embedding,
                    MemorySearchFilters(
                        tenant_id=int(tenant_id),
                        memory_tier=MemoryTier.TASK_ENGAGEMENT,
                        task_id=int(task_id),
                        max_results=engagement_max,
                    ),
                )
            if (
                not engagement_results
                and task_engagement_has_candidates
                and engagement_id is not None
                and tenant_id is not None
            ):
                engagement_results = await memory_store.retrieve_with_embedding(
                    query_embedding,
                    MemorySearchFilters(
                        tenant_id=int(tenant_id),
                        memory_tier=MemoryTier.TASK_ENGAGEMENT,
                        engagement_id=int(engagement_id),
                        max_results=engagement_max,
                    ),
                )

            summary = render_memory_summary(
                user_results,
                engagement_results,
                max_chars=max_chars,
            )
            db.commit()
            return summary
        except Exception:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    logger.debug("[MEMORY_RUNTIME] Rollback failed during retrieval", exc_info=True)
            raise
        finally:
            if db is not None:
                db.close()

    async def run_extraction(
        self,
        *,
        db: Session,
        selection: Mapping[str, Any],
        user_message: str,
        assistant_response: str,
        user_id: int,
        task_id: int | None,
        conversation_id: str | None,
        turn_id: str | None,
    ) -> None:
        """Run best-effort memory extraction from a completed turn snapshot."""

        runtime_selection = LLMRuntimeSelection.from_mapping(selection)
        if int(runtime_selection.credential_ref.user_id) != int(user_id):
            logger.warning("[MEMORY_RUNTIME] Refusing extraction with mismatched user scope")
            return

        from backend.services.memory.memory_extraction import MemoryExtractionService
        from backend.services.memory.memory_store import MemoryStore

        embedding_dependency = self._resolve_embedding_dependency(
            runtime_user_id=int(user_id),
            task_id=task_id,
            purpose="memory_extraction_embedding",
            db=db,
        )
        if embedding_dependency is None:
            return
        _embedding_selection, embedding_provider = embedding_dependency

        memory_llm_selection = self._resolve_memory_llm_selection(
            runtime_user_id=int(user_id),
            db=db,
        )
        if memory_llm_selection is None:
            return

        memory_store = MemoryStore(db, embedding_provider)
        memory_runtime_selection = LLMRuntimeSelection(
            provider=memory_llm_selection.provider,
            model=memory_llm_selection.extraction_model,
            credential_ref=memory_llm_selection.credential_ref,
            reasoning_effort=None,
        )

        try:
            gate_client = self._client_resolver.get_client(
                memory_runtime_selection,
                target=LLMCallTarget(
                    provider=memory_llm_selection.provider,
                    model=memory_llm_selection.gate_model,
                    role=MEMORY_EXTRACTION_GATE_ROLE,
                ),
                runtime_user_id=int(user_id),
                task_id=task_id,
                purpose=MEMORY_EXTRACTION_GATE_ROLE,
            )
            extraction_client = self._client_resolver.get_client(
                memory_runtime_selection,
                target=LLMCallTarget(
                    provider=memory_llm_selection.provider,
                    model=memory_llm_selection.extraction_model,
                    role=MEMORY_EXTRACTION_ROLE,
                ),
                runtime_user_id=int(user_id),
                task_id=task_id,
                purpose=MEMORY_EXTRACTION_ROLE,
            )
        except LLMProviderServiceError:
            logger.info("[MEMORY_RUNTIME] Skipping extraction: memory LLM credential unavailable")
            return
        service = MemoryExtractionService(memory_store, gate_client, extraction_client)
        task_scope = _resolve_task_scope(db, int(task_id) if task_id else None)

        await service.extract_if_needed(
            user_message=user_message,
            assistant_response=assistant_response,
            user_id=int(user_id),
            tenant_id=task_scope.tenant_id,
            engagement_id=task_scope.engagement_id,
            task_id=task_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )

    def _open_session(self, default_factory: Callable[[], Session]) -> Session:
        return self._session_factory() if self._session_factory is not None else default_factory()

    def _resolve_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        """Resolve credential refs while keeping tests free to use narrow fakes."""

        resolver = getattr(self._client_resolver, "get_credential_ref", None)
        if callable(resolver):
            return resolver(user_id, provider)
        return LLMCredentialRef(user_id=int(user_id), provider=str(provider))

    def _resolve_embedding_dependency(
        self,
        *,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
        db: Session | None = None,
    ) -> tuple[EmbeddingRuntimeSelection, Any] | None:
        """Return the selected embedding provider or no-op on credential failures."""

        try:
            selection = self._selection_service_for(db).resolve_embedding_selection(
                user_id=int(runtime_user_id)
            )
            secret = self._client_resolver.resolve_secret(
                LLMRuntimeSelection(
                    provider=selection.provider,
                    model=selection.model,
                    credential_ref=selection.credential_ref,
                    reasoning_effort=None,
                ),
                runtime_user_id=int(runtime_user_id),
                task_id=task_id,
                purpose=purpose,
            )
            api_key = str(secret.value or "").strip()
            if not api_key:
                logger.info("[MEMORY_RUNTIME] Skipping memory embeddings: empty resolved credential")
                return None
            embedding_provider = self._embedding_factory.create(selection, api_key=api_key)
        except (LLMProviderServiceError, SQLAlchemyError, ValueError):
            logger.info("[MEMORY_RUNTIME] Skipping memory embeddings: selection or credential unavailable")
            return None

        return selection, embedding_provider

    def _resolve_memory_llm_selection(
        self,
        *,
        runtime_user_id: int,
        db: Session | None = None,
    ) -> MemoryLLMRuntimeSelection | None:
        """Return the selected memory LLM dependency or no-op on credential failures."""

        try:
            return self._selection_service_for(db).resolve_memory_llm_selection(
                user_id=int(runtime_user_id)
            )
        except (LLMProviderServiceError, SQLAlchemyError, ValueError):
            logger.info("[MEMORY_RUNTIME] Skipping extraction: memory LLM selection or credential unavailable")
            return None

    def _selection_service_for(
        self,
        db: Session | None,
    ) -> EmbeddingRuntimeSelectionService:
        """Return a selection service scoped to the available persistence boundary."""

        if self._selection_service_injected or db is None or not hasattr(db, "execute"):
            return self._selection_service
        return EmbeddingRuntimeSelectionService(
            credential_ref_resolver=self._resolve_credential_ref,
            env_getter=self._env_getter,
            db=db,
        )


class _TaskScope(NamedTuple):
    """Resolved tenant/engagement ownership for one task context."""

    tenant_id: int | None
    engagement_id: int | None


def _resolve_task_scope(db: Session, task_id: int | None) -> _TaskScope:
    """Resolve task ownership scope from Task in the backend layer."""

    if task_id is None:
        return _TaskScope(tenant_id=None, engagement_id=None)
    from backend.models.core import Task

    task = db.query(Task).filter(Task.id == int(task_id)).first()
    if task is None:
        return _TaskScope(tenant_id=None, engagement_id=None)
    return _TaskScope(
        tenant_id=int(task.tenant_id) if task.tenant_id is not None else None,
        engagement_id=int(task.engagement_id) if task.engagement_id is not None else None,
    )


__all__ = [
    "MEMORY_EXTRACTION_GATE_ROLE",
    "MEMORY_EXTRACTION_ROLE",
    "MemoryRuntimeService",
    "render_memory_summary",
    "split_retrieval_limits",
]
