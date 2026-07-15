"""Generate task closure memo payloads through the provider-neutral LLM boundary."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent.providers.llm.core.base import LLMResponse
from core.llm.structured_schemas import TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT

from backend.services.llm_provider import LLMRuntimeConfigService
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionService,
)
from backend.services.llm_provider.types import LLMRuntimeSelection
from backend.services.reporting.contracts import (
    GENERATION_METADATA_DURATION_MS_KEY,
    GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_MODEL_KEY,
    GENERATION_METADATA_PROVIDER_KEY,
    GENERATION_METADATA_REASONING_EFFORT_KEY,
    GENERATION_METADATA_USAGE_KEY,
    TASK_CLOSURE_MEMO_CONTRACTS,
    TASK_CLOSURE_MEMO_SCHEMA_VERSION,
    TASK_MEMO_ERROR_GENERATION_FAILED,
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
    TaskMemoServiceErrorReason,
)
from backend.services.reporting.memo_prompt import RenderedTaskClosureMemoPrompt

logger = logging.getLogger(__name__)

_USAGE_METADATA_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "model",
    "provider",
    "cached_tokens",
    "reasoning_tokens",
    "api_surface",
    "cache_reporting",
    "source",
    "request_mode",
    "provider_usage_components",
)


@dataclass(frozen=True, slots=True)
class TaskClosureMemoGenerationResult:
    """Structured memo output and safe generation metadata."""

    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]


class TaskClosureMemoGenerationError(Exception):
    """Typed memo generation failure safe for failed-attempt persistence."""

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


class TaskClosureMemoGenerator:
    """Generate structured task closure memos without direct provider imports."""

    def __init__(
        self,
        db: Any,
        *,
        runtime_config_service: LLMRuntimeConfigService | None = None,
        reporting_selection_service: ReportingLLMSelectionService | None = None,
    ) -> None:
        self._runtime_config_service = runtime_config_service or LLMRuntimeConfigService(
            db
        )
        self._reporting_selection_service = (
            reporting_selection_service or ReportingLLMSelectionService(db)
        )

    async def generate(
        self,
        *,
        user_id: int,
        task_id: int,
        rendered_prompt: RenderedTaskClosureMemoPrompt,
        runtime_selection: LLMRuntimeSelection | dict[str, Any] | None = None,
    ) -> TaskClosureMemoGenerationResult:
        """Call the configured LLM runtime and return structured memo output."""

        started_at = time.perf_counter()
        base_metadata = dict(rendered_prompt.metadata)

        try:
            runtime_selection_value = (
                LLMRuntimeSelection.from_mapping(runtime_selection)
                if runtime_selection is not None
                else self._reporting_selection_service.build_runtime_selection(
                    user_id=user_id
                )
            )
            runtime_services = self._runtime_config_service.build_runtime_services()
            client = runtime_services.client_resolver.get_client(
                runtime_selection_value,
                runtime_user_id=user_id,
                task_id=task_id,
                purpose=TASK_CLOSURE_MEMO_CONTRACTS.generation_purpose,
            )
        except Exception as exc:
            logger.warning(
                "Task closure memo LLM runtime unavailable for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            raise TaskClosureMemoGenerationError(
                reason=TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
                safe_message="LLM runtime is unavailable for memo generation.",
                metadata=_metadata_with_duration(
                    base_metadata,
                    started_at=started_at,
                ),
            ) from exc

        provider = str(getattr(runtime_selection_value, "provider", "unknown"))
        model = str(
            getattr(client, "model", getattr(runtime_selection_value, "model", "unknown"))
        )
        call_metadata = {
            **base_metadata,
            GENERATION_METADATA_PROVIDER_KEY: provider,
            GENERATION_METADATA_MODEL_KEY: model,
            GENERATION_METADATA_REASONING_EFFORT_KEY: (
                runtime_selection_value.reasoning_effort
            ),
        }

        try:
            response = await client.chat_with_usage(
                rendered_prompt.system_prompt,
                rendered_prompt.user_prompt,
                structured_output=TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT,
            )
        except Exception as exc:
            logger.warning(
                "Task closure memo generation failed for task %s (%s)",
                task_id,
                exc.__class__.__name__,
            )
            raise TaskClosureMemoGenerationError(
                reason=TASK_MEMO_ERROR_GENERATION_FAILED,
                safe_message="LLM memo generation failed.",
                metadata=_metadata_with_duration(
                    call_metadata,
                    started_at=started_at,
                ),
            ) from exc

        payload = _structured_payload(response)
        if payload is None:
            raise TaskClosureMemoGenerationError(
                reason=TASK_MEMO_ERROR_GENERATION_FAILED,
                safe_message="LLM memo generation did not return structured output.",
                metadata=_metadata_with_duration(
                    call_metadata,
                    started_at=started_at,
                    usage=_usage_metadata(getattr(response, "usage", None)),
                ),
            )

        return TaskClosureMemoGenerationResult(
            payload=payload,
            metadata=_metadata_with_duration(
                {
                    **call_metadata,
                    GENERATION_METADATA_USAGE_KEY: _usage_metadata(response.usage),
                    GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY: (
                        TASK_CLOSURE_MEMO_SCHEMA_VERSION
                    ),
                },
                started_at=started_at,
            ),
        )


def _structured_payload(response: LLMResponse) -> Mapping[str, Any] | None:
    payload = getattr(response, "structured_output", None)
    return payload if isinstance(payload, Mapping) else None


def _metadata_with_duration(
    metadata: Mapping[str, Any],
    *,
    started_at: float,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(metadata)
    if usage is not None:
        result[GENERATION_METADATA_USAGE_KEY] = usage
    result[GENERATION_METADATA_DURATION_MS_KEY] = max(
        0,
        round((time.perf_counter() - started_at) * 1000),
    )
    return result


def _usage_metadata(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None

    usage_payload: Any = None
    to_dict = getattr(usage, "to_dict", None)
    if callable(to_dict):
        try:
            usage_payload = to_dict(TASK_CLOSURE_MEMO_CONTRACTS.generation_purpose)
        except Exception:
            usage_payload = None

    if usage_payload is None and isinstance(usage, Mapping):
        usage_payload = usage

    if usage_payload is None:
        usage_payload = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "model": getattr(usage, "model", "unknown"),
            "provider": getattr(usage, "provider", "unknown"),
        }

    if not isinstance(usage_payload, Mapping):
        return None

    return {
        key: value
        for key in _USAGE_METADATA_KEYS
        if (value := usage_payload.get(key)) is not None
    }


__all__ = [
    "TaskClosureMemoGenerationError",
    "TaskClosureMemoGenerationResult",
    "TaskClosureMemoGenerator",
]
