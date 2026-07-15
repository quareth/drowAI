"""Orchestrate task closure memo preparation and version promotion."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config.reporting import get_memo_preparing_stale_timeout_seconds
from backend.models.core import Engagement, Task
from backend.models.reporting import TaskClosureMemo
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import (
    GENERATION_METADATA_DURATION_MS_KEY,
    GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_MODEL_KEY,
    GENERATION_METADATA_PROMPT_FAMILY_KEY,
    GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY,
    GENERATION_METADATA_PROMPT_VERSION_KEY,
    GENERATION_METADATA_PROVIDER_KEY,
    GENERATION_METADATA_REASONING_EFFORT_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_USAGE_KEY,
    GENERATION_METADATA_VALIDATION_STATUS_KEY,
    GENERATION_METADATA_VALIDATION_VERSION_KEY,
    MEMO_MODE_SUPPORTED,
    TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE,
    TASK_MEMO_ERROR_GENERATION_FAILED,
    TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND,
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
    TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    TASK_MEMO_ERROR_PERSISTENCE_FAILED,
    TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
    TASK_MEMO_ERROR_PROMPT_RENDER_FAILED,
    TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    TASK_MEMO_ERROR_TASK_NOT_FOUND,
    TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
    TaskMemoServiceErrorReason,
)
from backend.services.reporting.memo_generator import (
    TaskClosureMemoGenerationError,
    TaskClosureMemoGenerator,
)
from backend.services.reporting.memo_prompt import TaskClosureMemoPromptRenderer
from backend.services.reporting.reporting_state_service import watermarks_match
from backend.services.reporting.runtime_readiness_service import (
    RuntimeReadiness,
    RuntimeReadinessService,
)
from backend.services.reporting.source_watermark_service import SourceWatermarkService
from backend.services.reporting.task_memo_context_builder import TaskMemoContextBuilder
from backend.services.reporting.validation import (
    TaskClosureMemoValidationError,
    TaskClosureMemoValidator,
)

logger = logging.getLogger(__name__)

_DEFAULT_FAILURE_MESSAGE = "Task closure memo preparation failed."
_PREPARATION_IN_PROGRESS_MESSAGE = "Task memo preparation is already in progress."
_STALE_PREPARING_FAILURE_MESSAGE = (
    "Task closure memo preparation exceeded the in-flight timeout."
)
_MAX_SAFE_FAILURE_MESSAGE_LENGTH = 512
_MAX_SAFE_METADATA_STRING_LENGTH = 1000
_SAFE_FAILURE_MESSAGES: dict[TaskMemoServiceErrorReason, str] = {
    TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE: "Task closure memo context could not be built.",
    TASK_MEMO_ERROR_PROMPT_RENDER_FAILED: "Task closure memo prompt could not be rendered.",
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE: (
        "LLM runtime is unavailable for memo generation."
    ),
    TASK_MEMO_ERROR_GENERATION_FAILED: "LLM memo generation failed.",
    TASK_MEMO_ERROR_VALIDATION_FAILED: "Generated task closure memo failed validation.",
    TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL: (
        "Task does not have reportable or limited memo source material."
    ),
    TASK_MEMO_ERROR_PERSISTENCE_FAILED: "Task closure memo persistence failed.",
}
_SAFE_FAILURE_METADATA_KEYS = {
    GENERATION_METADATA_PROMPT_FAMILY_KEY,
    GENERATION_METADATA_PROMPT_VERSION_KEY,
    GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY,
    GENERATION_METADATA_PROVIDER_KEY,
    GENERATION_METADATA_MODEL_KEY,
    GENERATION_METADATA_REASONING_EFFORT_KEY,
    GENERATION_METADATA_USAGE_KEY,
    GENERATION_METADATA_DURATION_MS_KEY,
    GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_VALIDATION_VERSION_KEY,
    GENERATION_METADATA_VALIDATION_STATUS_KEY,
    "issue_count",
    "issues",
}
_SENSITIVE_FAILURE_METADATA_KEY_PARTS = (
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "full_transcript",
    "llm_output",
    "model_output",
    "prompt_text",
    "raw_evidence",
    "raw_model",
    "raw_prompt",
    "response_text",
    "secret",
    "system_prompt",
    "transcript",
    "user_prompt",
)


@dataclass(frozen=True, slots=True)
class TaskMemoFailureDetails:
    """Safe failure details suitable for memo attempt persistence."""

    reason: TaskMemoServiceErrorReason
    safe_message: str
    metadata: Mapping[str, Any]


class TaskMemoServiceError(Exception):
    """Typed task memo service failure safe for router error mapping."""

    def __init__(
        self,
        *,
        reason: TaskMemoServiceErrorReason,
        safe_message: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.safe_message = safe_message
        self.metadata = dict(metadata or {})


class TaskMemoService:
    """Prepare, read, and list scoped task closure memo versions."""

    def __init__(
        self,
        db: Session,
        *,
        repository: TaskClosureMemoRepository | None = None,
        runtime_readiness: RuntimeReadinessService | None = None,
        source_watermarks: SourceWatermarkService | None = None,
        context_builder: TaskMemoContextBuilder | None = None,
        prompt_renderer: TaskClosureMemoPromptRenderer | None = None,
        generator: TaskClosureMemoGenerator | None = None,
        validator: TaskClosureMemoValidator | None = None,
        memo_preparing_stale_timeout_seconds: int | None = None,
    ) -> None:
        self._db = db
        self._repository = repository or TaskClosureMemoRepository(db)
        self._runtime_readiness = runtime_readiness or RuntimeReadinessService(db)
        self._source_watermarks = source_watermarks or SourceWatermarkService(db)
        self._context_builder = context_builder or TaskMemoContextBuilder(db)
        self._prompt_renderer = prompt_renderer or TaskClosureMemoPromptRenderer()
        self._generator = generator or TaskClosureMemoGenerator(db)
        self._validator = validator or TaskClosureMemoValidator()
        if memo_preparing_stale_timeout_seconds is not None:
            timeout_seconds = int(memo_preparing_stale_timeout_seconds)
            self._memo_preparing_stale_timeout_seconds = (
                timeout_seconds
                if timeout_seconds > 0
                else get_memo_preparing_stale_timeout_seconds()
            )
        else:
            self._memo_preparing_stale_timeout_seconds = (
                get_memo_preparing_stale_timeout_seconds()
            )

    async def prepare_task_memo(
        self,
        *,
        tenant_id: int,
        user_id: int,
        task_id: int,
        regenerate: bool = False,
    ) -> TaskClosureMemo:
        """Prepare or return the current ready memo for one scoped task."""

        task = self._resolve_scoped_task(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
        )
        engagement_id = int(task.engagement_id)
        readiness = self._runtime_readiness.compute_for_task(
            tenant_id=tenant_id,
            task_id=task_id,
        )
        self._ensure_runtime_ready(readiness)

        source_watermark = self._source_watermarks.compute_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        current_memo = self._repository.get_current_ready_memo(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        if (
            current_memo is not None
            and not regenerate
            and watermarks_match(current_memo.source_watermark, source_watermark)
        ):
            return current_memo

        self._clear_stale_preparing_attempts(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        preparing_memo = self._repository.get_preparing_memo_attempt(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        if preparing_memo is not None:
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
                safe_message=_PREPARATION_IN_PROGRESS_MESSAGE,
            )

        attempt = self._create_preparing_attempt(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            source_watermark=source_watermark,
        )

        try:
            context = self._build_context(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                regenerate=regenerate,
            )
            if not context.is_preparable or context.memo_mode is None:
                raise TaskMemoServiceError(
                    reason=(
                        context.not_preparable_reason
                        or TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL
                    ),
                    safe_message="Task does not have reportable or limited memo source material.",
                )

            rendered_prompt = self._render_prompt(context=context, task_id=task_id)
            generation_result = await self._maybe_await(
                self._generator.generate(
                    user_id=user_id,
                    task_id=task_id,
                    rendered_prompt=rendered_prompt,
                )
            )
            validation_result = self._validator.validate(
                payload=generation_result.payload,
                context=context,
            )
            generation_metadata = {
                **dict(generation_result.metadata),
                **dict(validation_result.metadata),
            }
            return self._promote_ready_attempt(
                attempt=attempt,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                memo_mode=context.memo_mode,
                source_watermark=_json_mapping(context.source_watermark),
                memo=_json_mapping(validation_result.payload),
                generation_metadata=_generation_metadata_for_persistence(
                    generation_metadata,
                    source_watermark=context.source_watermark,
                ),
            )
        except Exception as exc:
            self._rollback()
            failure = _failure_details(exc)
            self._mark_attempt_failed(
                attempt=attempt,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                failure=failure,
            )
            if isinstance(exc, TaskMemoServiceError):
                raise
            raise TaskMemoServiceError(
                reason=failure.reason,
                safe_message=failure.safe_message,
                metadata=failure.metadata,
            ) from exc

    def get_current_task_memo(
        self,
        *,
        tenant_id: int,
        user_id: int,
        task_id: int,
    ) -> TaskClosureMemo | None:
        """Return the current ready memo for one tenant/user-owned task."""

        task = self._resolve_scoped_task(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
        )
        return self._repository.get_current_ready_memo(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=int(task.engagement_id),
            task_id=task_id,
        )

    def list_task_memo_history(
        self,
        *,
        tenant_id: int,
        user_id: int,
        task_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskClosureMemo]:
        """Return memo attempts for one tenant/user-owned task."""

        task = self._resolve_scoped_task(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
        )
        return self._repository.list_memo_history_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=int(task.engagement_id),
            task_id=task_id,
            limit=limit,
            offset=offset,
        )

    def _resolve_scoped_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        task_id: int,
    ) -> Task:
        task = (
            self._db.query(Task)
            .filter(
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.id == int(task_id),
            )
            .one_or_none()
        )
        if task is None:
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_TASK_NOT_FOUND,
                safe_message="Task was not found.",
            )
        if task.engagement_id is None:
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
                safe_message="Task is not attached to an engagement.",
            )

        engagement = (
            self._db.query(Engagement.id)
            .filter(
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Engagement.id == int(task.engagement_id),
            )
            .one_or_none()
        )
        if engagement is None:
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND,
                safe_message="Engagement was not found.",
            )
        return task

    def _ensure_runtime_ready(self, readiness: RuntimeReadiness) -> None:
        if readiness.runtime_retired:
            return
        raise TaskMemoServiceError(
            reason=(
                readiness.not_preparable_reason
                or TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED
            ),
            safe_message="Task runtime is not ready for memo preparation.",
        )

    def _create_preparing_attempt(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        source_watermark: Mapping[str, Any],
    ) -> TaskClosureMemo:
        try:
            version = self._repository.next_memo_version(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            attempt = self._repository.create_memo_attempt(
                tenant_id=tenant_id,
                user_id=user_id,
                created_by_user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                version=version,
                memo_mode=MEMO_MODE_SUPPORTED,
                source_watermark=_json_mapping(source_watermark),
            )
            self._commit()
            return attempt
        except IntegrityError as exc:
            self._rollback()
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
                safe_message=_PREPARATION_IN_PROGRESS_MESSAGE,
            ) from exc
        except Exception as exc:
            self._rollback()
            logger.warning(
                "Task closure memo preparing attempt persistence failed for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_PERSISTENCE_FAILED,
                safe_message="Task closure memo attempt could not be persisted.",
            ) from exc

    def _clear_stale_preparing_attempts(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> None:
        stale_before = datetime.now(UTC) - timedelta(
            seconds=self._memo_preparing_stale_timeout_seconds
        )
        failed_count = self._repository.mark_stale_preparing_memos_failed(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            stale_before=stale_before,
            error_message=_STALE_PREPARING_FAILURE_MESSAGE,
        )
        if failed_count:
            self._commit()

    def _promote_ready_attempt(
        self,
        *,
        attempt: TaskClosureMemo,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        memo_mode: str,
        source_watermark: dict[str, Any],
        memo: dict[str, Any],
        generation_metadata: Mapping[str, Any],
    ) -> TaskClosureMemo:
        try:
            self._repository.clear_current_ready_memos_for_task(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            ready = self._repository.mark_memo_ready(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                memo_id=attempt.id,
                memo=memo,
                source_watermark=source_watermark,
                generation_metadata=dict(generation_metadata),
                generated_at=datetime.now(UTC),
                memo_mode=memo_mode,
            )
            if ready is None:
                raise TaskMemoServiceError(
                    reason=TASK_MEMO_ERROR_PERSISTENCE_FAILED,
                    safe_message="Task closure memo attempt could not be promoted.",
                )
            self._commit()
            return ready
        except Exception as exc:
            self._rollback()
            logger.warning(
                "Task closure memo ready promotion failed for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            if isinstance(exc, TaskMemoServiceError):
                raise
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_PERSISTENCE_FAILED,
                safe_message="Task closure memo attempt could not be promoted.",
            ) from exc

    def _build_context(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        regenerate: bool,
    ) -> Any:
        try:
            return self._context_builder.build_for_task(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                regenerate=regenerate,
            )
        except TaskMemoServiceError:
            raise
        except Exception as exc:
            logger.warning(
                "Task closure memo context build failed for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE,
                safe_message="Task closure memo context could not be built.",
            ) from exc

    def _render_prompt(self, *, context: Any, task_id: int) -> Any:
        try:
            return self._prompt_renderer.render(context)
        except TaskMemoServiceError:
            raise
        except Exception as exc:
            logger.warning(
                "Task closure memo prompt render failed for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_PROMPT_RENDER_FAILED,
                safe_message="Task closure memo prompt could not be rendered.",
            ) from exc

    def _mark_attempt_failed(
        self,
        *,
        attempt: TaskClosureMemo,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        failure: TaskMemoFailureDetails,
    ) -> None:
        try:
            failed = self._repository.mark_memo_failed(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                memo_id=attempt.id,
                error_message=failure.safe_message,
                generation_metadata=_generation_metadata_for_persistence(
                    failure.metadata,
                    source_watermark=_attempt_source_watermark(attempt),
                ),
                source_watermark=_attempt_source_watermark(attempt),
            )
            if failed is None:
                raise RuntimeError("memo attempt missing during failure update")
            self._commit()
        except Exception as exc:
            self._rollback()
            logger.warning(
                "Task closure memo failure update failed for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            raise TaskMemoServiceError(
                reason=TASK_MEMO_ERROR_PERSISTENCE_FAILED,
                safe_message="Task closure memo failure could not be persisted.",
            ) from exc

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _commit(self) -> None:
        self._db.commit()

    def _rollback(self) -> None:
        self._db.rollback()


def _failure_details(exc: Exception) -> TaskMemoFailureDetails:
    if isinstance(exc, TaskMemoServiceError):
        return TaskMemoFailureDetails(
            reason=exc.reason,
            safe_message=_safe_failure_message(exc.reason, exc.safe_message),
            metadata=_safe_failure_metadata(exc.metadata),
        )
    if isinstance(exc, TaskClosureMemoGenerationError):
        return TaskMemoFailureDetails(
            reason=exc.reason,
            safe_message=_safe_failure_message(exc.reason, exc.safe_message),
            metadata=_safe_failure_metadata(exc.metadata),
        )
    if isinstance(exc, TaskClosureMemoValidationError):
        return TaskMemoFailureDetails(
            reason=exc.reason,
            safe_message=_safe_failure_message(exc.reason, exc.safe_message),
            metadata=_safe_failure_metadata(exc.metadata),
        )
    return TaskMemoFailureDetails(
        reason=_reason_for_untyped_exception(exc),
        safe_message=_DEFAULT_FAILURE_MESSAGE,
        metadata={},
    )


def _reason_for_untyped_exception(exc: Exception) -> TaskMemoServiceErrorReason:
    name = exc.__class__.__name__.lower()
    if "prompt" in name or "template" in name:
        return TASK_MEMO_ERROR_PROMPT_RENDER_FAILED
    if isinstance(exc, ValueError):
        return TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE
    return TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE


def _safe_failure_message(
    reason: TaskMemoServiceErrorReason,
    candidate: str | None,
) -> str:
    message = _SAFE_FAILURE_MESSAGES.get(reason) or candidate or _DEFAULT_FAILURE_MESSAGE
    return str(message)[:_MAX_SAFE_FAILURE_MESSAGE_LENGTH]


def _safe_failure_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized_key = str(key)
        if normalized_key not in _SAFE_FAILURE_METADATA_KEYS:
            continue
        safe[normalized_key] = _safe_metadata_value(value)
    return safe


def _generation_metadata_for_persistence(
    metadata: Mapping[str, Any],
    *,
    source_watermark: Mapping[str, Any] | None,
) -> dict[str, Any]:
    safe = _safe_failure_metadata(metadata)
    watermark_schema_version = _source_watermark_schema_version(source_watermark)
    if watermark_schema_version is not None:
        safe[GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY] = (
            watermark_schema_version
        )
    return safe


def _source_watermark_schema_version(
    source_watermark: Mapping[str, Any] | None,
) -> Any:
    if source_watermark is None:
        return None
    return _safe_metadata_value(source_watermark.get("schema_version"))


def _safe_metadata_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:_MAX_SAFE_METADATA_STRING_LENGTH]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        safe_mapping: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if _is_sensitive_failure_metadata_key(normalized_key):
                continue
            safe_item = _safe_metadata_value(item)
            if safe_item is not None:
                safe_mapping[normalized_key] = safe_item
        return safe_mapping
    if isinstance(value, tuple | list):
        return [
            item
            for item in (_safe_metadata_value(item) for item in value)
            if item is not None
        ]
    return str(value)[:_MAX_SAFE_METADATA_STRING_LENGTH]


def _is_sensitive_failure_metadata_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in _SENSITIVE_FAILURE_METADATA_KEY_PARTS)


def _attempt_source_watermark(attempt: TaskClosureMemo) -> dict[str, Any] | None:
    if isinstance(attempt.source_watermark, Mapping):
        return _json_mapping(attempt.source_watermark)
    return None


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, frozenset | set):
        return sorted(_json_value(item) for item in value)
    return value


__all__ = [
    "TaskMemoFailureDetails",
    "TaskMemoService",
    "TaskMemoServiceError",
]
