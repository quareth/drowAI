"""Generate one structured engagement report section through the LLM boundary."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.llm.structured_schemas import ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT

from backend.config import E2E_DETERMINISTIC_MODE
from backend.services.llm_provider import (
    LLMRuntimeConfigService,
    classify_llm_runtime_failure,
)
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionService,
)
from backend.services.llm_provider.types import LLMRuntimeSelection
from backend.services.reporting.contracts import (
    GENERATION_METADATA_DURATION_MS_KEY,
    GENERATION_METADATA_MODEL_KEY,
    GENERATION_METADATA_PROVIDER_KEY,
    GENERATION_METADATA_REASONING_EFFORT_KEY,
    GENERATION_METADATA_USAGE_KEY,
    REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
    REPORT_GENERATION_ERROR_SECTION_TIMEOUT,
    REPORT_SECTION_SCHEMA_VERSION,
    ReportGenerationServiceErrorReason,
)
from backend.services.reporting.report_section_prompt import (
    RenderedReportSectionPrompt,
)

logger = logging.getLogger(__name__)

_GENERATION_PURPOSE = "reporting.engagement_report_section"
_STRUCTURED_SCHEMA_VERSION_KEY = "structured_schema_version"
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
class ReportSectionGenerationResult:
    """Structured section payload and safe generation metadata."""

    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]


class ReportSectionGenerationError(Exception):
    """Typed section generation failure safe for report job persistence."""

    def __init__(
        self,
        *,
        reason: ReportGenerationServiceErrorReason,
        safe_message: str,
        metadata: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.safe_message = safe_message
        self.metadata = dict(metadata or {})
        self.retryable = bool(retryable)


class ReportSectionGenerator:
    """Generate one report section without importing direct provider SDKs."""

    def __init__(
        self,
        db: Any,
        *,
        runtime_config_service: LLMRuntimeConfigService | None = None,
        reporting_selection_service: ReportingLLMSelectionService | None = None,
    ) -> None:
        self._runtime_config_service = (
            runtime_config_service or LLMRuntimeConfigService(db)
        )
        self._reporting_selection_service = (
            reporting_selection_service or ReportingLLMSelectionService(db)
        )

    async def generate(
        self,
        *,
        user_id: int,
        rendered_prompt: RenderedReportSectionPrompt,
        task_id: int | None = None,
        runtime_selection: LLMRuntimeSelection | dict[str, Any] | None = None,
    ) -> ReportSectionGenerationResult:
        """Call the configured LLM runtime for exactly one rendered section."""

        started_at = time.perf_counter()
        base_metadata = dict(rendered_prompt.metadata)
        section_id = _safe_section_id(base_metadata)

        if E2E_DETERMINISTIC_MODE:
            payload = _deterministic_e2e_payload(rendered_prompt)
            return ReportSectionGenerationResult(
                payload=payload,
                metadata=_metadata_with_duration(
                    {
                        **base_metadata,
                        GENERATION_METADATA_PROVIDER_KEY: "deterministic_e2e",
                        GENERATION_METADATA_MODEL_KEY: "offline-report-section-v1",
                        GENERATION_METADATA_REASONING_EFFORT_KEY: None,
                        GENERATION_METADATA_USAGE_KEY: {},
                        _STRUCTURED_SCHEMA_VERSION_KEY: _structured_schema_version(payload),
                    },
                    started_at=started_at,
                ),
            )

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
                purpose=_GENERATION_PURPOSE,
            )
        except Exception as exc:
            retryable, reason = _failure_policy(exc)
            logger.warning(
                "Report section LLM runtime unavailable for section %s (%s)",
                section_id,
                exc.__class__.__name__,
            )
            raise ReportSectionGenerationError(
                reason=reason,
                safe_message="LLM runtime is unavailable for report section generation.",
                metadata=_metadata_with_duration(
                    _failure_metadata(base_metadata, exc),
                    started_at=started_at,
                ),
                retryable=retryable,
            ) from exc

        provider = str(getattr(runtime_selection_value, "provider", "unknown"))
        model = str(
            getattr(
                client, "model", getattr(runtime_selection_value, "model", "unknown")
            )
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
                structured_output=ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT,
            )
        except Exception as exc:
            retryable, reason = _failure_policy(exc)
            logger.warning(
                "Report section generation failed for section %s (%s)",
                section_id,
                exc.__class__.__name__,
            )
            raise ReportSectionGenerationError(
                reason=reason,
                safe_message="LLM report section generation failed.",
                metadata=_metadata_with_duration(
                    _failure_metadata(call_metadata, exc),
                    started_at=started_at,
                ),
                retryable=retryable,
            ) from exc

        payload = _structured_payload(response)
        usage = _usage_metadata(getattr(response, "usage", None))
        if payload is None:
            raise ReportSectionGenerationError(
                reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
                safe_message=(
                    "LLM report section generation did not return structured output."
                ),
                metadata=_metadata_with_duration(
                    call_metadata,
                    started_at=started_at,
                    usage=usage,
                ),
                retryable=True,
            )

        return ReportSectionGenerationResult(
            payload=payload,
            metadata=_metadata_with_duration(
                {
                    **call_metadata,
                    GENERATION_METADATA_USAGE_KEY: usage,
                    _STRUCTURED_SCHEMA_VERSION_KEY: _structured_schema_version(payload),
                },
                started_at=started_at,
            ),
        )


def _safe_section_id(metadata: Mapping[str, Any]) -> str:
    section_id = str(metadata.get("section_id") or "").strip()
    return section_id or "unknown"


def _deterministic_e2e_payload(
    rendered_prompt: RenderedReportSectionPrompt,
) -> dict[str, Any]:
    """Build one validator-compatible offline section for explicit E2E mode."""
    context = json.loads(rendered_prompt.report_context_json)
    section = json.loads(rendered_prompt.section_plan_json)
    if not isinstance(context, Mapping) or not isinstance(section, Mapping):
        raise ValueError("Deterministic report context and section plan must be objects.")

    section_id = str(section.get("section_id") or "").strip()
    section_type = str(section.get("section_type") or "").strip()
    title = str(section.get("title") or "").strip()
    if not section_id or not section_type or not title:
        raise ValueError("Deterministic report section plan is incomplete.")

    source_refs = {
        "task_memo_ids": _string_refs(context.get("allowed_task_memo_ids")),
        "knowledge_refs": _string_refs(context.get("allowed_knowledge_refs")),
        "evidence_refs": _string_refs(context.get("allowed_evidence_refs")),
    }
    return {
        "schema_version": REPORT_SECTION_SCHEMA_VERSION,
        "section_id": section_id,
        "section_type": section_type,
        "title": title,
        "status": "ready",
        "content_markdown": (
            f"Deterministic E2E {title.lower()} generated from persisted "
            "suite-owned reporting inputs."
        ),
        "blocks": [],
        "source_refs": source_refs,
        "unsupported_notes": [],
        "generation_notes": ["Offline deterministic E2E generation."],
    }


def _string_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _failure_policy(
    exc: Exception,
) -> tuple[bool, ReportGenerationServiceErrorReason]:
    disposition = classify_llm_runtime_failure(exc)
    if disposition.kind == "timeout":
        return True, REPORT_GENERATION_ERROR_SECTION_TIMEOUT
    return disposition.retryable, REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED


def _failure_metadata(
    metadata: Mapping[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    status_code = getattr(exc, "status_code", None)
    return {
        **dict(metadata),
        "failure_class": exc.__class__.__name__,
        **(
            {"provider_status_code": int(status_code)}
            if isinstance(status_code, int)
            else {}
        ),
    }


def _structured_payload(response: Any) -> Mapping[str, Any] | None:
    payload = getattr(response, "structured_output", None)
    return payload if isinstance(payload, Mapping) else None


def _structured_schema_version(payload: Mapping[str, Any]) -> str:
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, str) and schema_version.strip():
        return schema_version.strip()
    return REPORT_SECTION_SCHEMA_VERSION


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
            usage_payload = to_dict(_GENERATION_PURPOSE)
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
    "ReportSectionGenerationError",
    "ReportSectionGenerationResult",
    "ReportSectionGenerator",
]
