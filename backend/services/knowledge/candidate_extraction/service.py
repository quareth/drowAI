"""Candidate extraction orchestration service.

Scope:
- Coordinate bounded evidence collection, prompt generation, LLM invocation,
  and structured payload mapping into candidate observation results.
- Ingestion-side candidate orchestration: feature-flag gating, policy checks,
  post-tool candidate payload dispatch.
- Candidate LLM usage recording for task accounting.

Boundary:
- Keeps orchestration flow only; collaborators own policy, evidence, prompting,
  and mapping details."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

from agent.context.chunking.redactor import ArtifactRedactor
from agent.providers.llm.core.base import LLMClient, LLMResponse
from core.llm import (
    LLM_TIMEOUT_KNOWLEDGE_CANDIDATE_EXTRACTION_SEC,
    wait_for_with_timeout,
)
from core.llm.structured_schemas import GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT
from core.prompts.registry import PromptRegistry
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config.feature_flags import (
    get_knowledge_candidate_max_cost_usd,
    get_knowledge_candidate_max_prompt_tokens,
    get_knowledge_vulnerability_min_confidence,
    is_knowledge_candidate_extraction_enabled,
    is_knowledge_vulnerability_candidates_enabled,
)
from ..evidence_read_service import KnowledgeEvidenceReadService
from backend.services.metrics.utils import safe_gauge, safe_inc
from backend.models.core import Engagement

from .contracts import (
    CandidateExtractionPolicyRequest,
    CandidateExtractionPolicyDecision,
    CandidateExtractionRequest,
    CandidateExtractionResult,
    CandidateExtractionUsageSummary,
    coerce_candidate_usage_summary,
)
from .evidence_reader import (
    CandidateEvidenceCollector,
    build_bounded_evidence_for_mapping,
    normalize_post_tool_candidate_payload,
)
from .mapping import build_usage_summary, map_structured_payload, resolve_structured_payload
from .policy import KnowledgeCandidateExtractionPolicy
from .prompting import CandidatePromptBuilder

from collections.abc import Callable, Mapping
from typing import Any

from backend.models import KnowledgeEvidenceArchive, KnowledgeIngestionRun
from ..contracts import parse_semantic_inputs_from_execution
from backend.services.usage_tracking import UsageTrackingService
from backend.services.usage_tracking.models import ProviderUsageComponents, UsageData

logger = logging.getLogger(__name__)


class KnowledgeCandidateExtractionService:
    """Extract low-authority candidate observations from bounded evidence."""

    def __init__(
        self,
        db: Session,
        *,
        llm_client: LLMClient | None,
        prompt_registry: PromptRegistry | None = None,
        evidence_read_service: KnowledgeEvidenceReadService | None = None,
        redactor: ArtifactRedactor | None = None,
    ) -> None:
        self.db = db
        self.llm_client = llm_client
        self.prompt_registry = prompt_registry or PromptRegistry()
        self.evidence_read_service = evidence_read_service or KnowledgeEvidenceReadService(db)
        self.redactor = redactor or ArtifactRedactor()
        self._evidence_collector = CandidateEvidenceCollector(
            db,
            evidence_read_service=self.evidence_read_service,
        )
        self._prompt_builder = CandidatePromptBuilder(
            prompt_registry=self.prompt_registry,
            redactor=self.redactor,
        )

    def extract_candidates_sync(
        self,
        *,
        request: CandidateExtractionRequest,
    ) -> CandidateExtractionResult:
        """Synchronous facade for services that are not async-aware yet."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop and running_loop.is_running():
            raise RuntimeError(
                "extract_candidates_sync() cannot run inside an active event loop; "
                "use extract_candidates()"
            )
        return asyncio.run(self.extract_candidates(request=request))

    async def extract_candidates(
        self,
        *,
        request: CandidateExtractionRequest,
    ) -> CandidateExtractionResult:
        """Extract candidate observations from durable/replay evidence sources."""
        started = time.perf_counter()

        def _emit_metrics(status: str) -> None:
            safe_inc("knowledge_candidate_extraction_total")
            if status == "failed":
                safe_inc("knowledge_candidate_extraction_failed_total")
            if status == "no_signal":
                safe_inc("knowledge_candidate_no_signal_total")
            safe_gauge(
                "knowledge_extraction_duration_seconds",
                max(0.0, time.perf_counter() - started),
            )

        if not is_knowledge_candidate_extraction_enabled():
            return CandidateExtractionResult.skipped(
                reason="candidate_feature_disabled",
                policy_decision=CandidateExtractionPolicyDecision(
                    action="skip",
                    reason="candidate_feature_disabled",
                    policy_metadata={"feature_flag_enabled": False},
                ),
            )

        if self.llm_client is None:
            result = CandidateExtractionResult.failed(reason="llm_client_not_configured")
            _emit_metrics("failed")
            return result

        bounded_evidence = self._evidence_collector.collect_bounded_evidence(request=request)
        if not bounded_evidence:
            result = CandidateExtractionResult.no_signal_result(
                reason="no_readable_evidence",
                policy_decision=CandidateExtractionPolicyDecision(
                    action="no_signal",
                    reason="no_readable_evidence",
                    policy_metadata={"evidence_count": 0},
                ),
                evidence_archive_ids_used=(),
                durable_masking_applied=False,
                usage_summary=CandidateExtractionUsageSummary(),
            )
            _emit_metrics("no_signal")
            return result

        redacted_evidence = self._prompt_builder.redact_evidence_bundle(bounded_evidence)
        durable_masking_applied = (
            redacted_evidence != bounded_evidence
            or self._prompt_builder.compact_hint_masking_applied(request.compact_output_hint)
        )
        prompts = self._prompt_builder.build_prompts(request=request, bounded_evidence=redacted_evidence)

        llm_response: LLMResponse
        try:
            llm_response = await wait_for_with_timeout(
                self.llm_client.chat_with_usage(
                    system_prompt=prompts["system_prompt"],
                    user_prompt=prompts["user_prompt"],
                    temperature=float(request.llm_temperature),
                    max_tokens=int(request.llm_max_tokens),
                    structured_output=GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT,
                ),
                timeout_sec=LLM_TIMEOUT_KNOWLEDGE_CANDIDATE_EXTRACTION_SEC,
                component="KNOWLEDGE_CANDIDATE_EXTRACTION",
                operation="candidate_extractor_llm_call",
                logger=logger,
                task_id=request.task_id,
                outcome="candidate_extraction_timeout",
            )
        except Exception as exc:
            result = CandidateExtractionResult.failed(
                reason=f"candidate_extractor_llm_call_failed:{exc.__class__.__name__}",
            )
            _emit_metrics("failed")
            return result

        usage_summary = build_usage_summary(llm_response.usage)
        payload = resolve_structured_payload(llm_response)
        if payload is None:
            result = CandidateExtractionResult.failed(
                reason="candidate_extractor_invalid_structured_output",
                usage_summary=usage_summary,
            )
            _emit_metrics("failed")
            return result

        user_id = self._resolve_user_id(engagement_id=int(request.engagement_id))
        if user_id is None:
            result = CandidateExtractionResult.failed(reason="candidate_extractor_missing_user_context")
            _emit_metrics("failed")
            return result

        result = map_structured_payload(
            request=request,
            user_id=user_id,
            payload=payload,
            bounded_evidence=redacted_evidence,
            durable_masking_applied=durable_masking_applied,
            usage_summary=usage_summary,
            minimum_vulnerability_confidence=get_knowledge_vulnerability_min_confidence(),
            enable_vulnerability_candidates=is_knowledge_vulnerability_candidates_enabled(),
        )
        _emit_metrics(result.status)
        return result

    def _resolve_user_id(self, *, engagement_id: int) -> int | None:
        user_id = self.db.execute(
            select(Engagement.user_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if user_id is None:
            return None
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return None

    async def aclose(self) -> None:
        """Close underlying LLM transport resources when supported."""
        client = self.llm_client
        if client is None:
            return
        try:
            close_fn = getattr(client, "aclose", None)
            if callable(close_fn):
                await close_fn()
                return
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                maybe_result = close_fn()
                if inspect.isawaitable(maybe_result):
                    await maybe_result
                return
            raw_client = getattr(client, "_client", None)
            if raw_client is not None:
                raw_close = getattr(raw_client, "aclose", None) or getattr(raw_client, "close", None)
                if callable(raw_close):
                    maybe_result = raw_close()
                    if inspect.isawaitable(maybe_result):
                        await maybe_result
        except Exception:
            return

    def close_sync(self) -> None:
        """Synchronous close facade for non-async orchestrators."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop and running_loop.is_running():
            raise RuntimeError("close_sync() cannot run inside an active event loop; use aclose()")
        asyncio.run(self.aclose())


def maybe_run_candidate_extraction(
    *,
    run: KnowledgeIngestionRun,
    execution_payload: dict[str, Any],
    archived_rows: list[KnowledgeEvidenceArchive],
    deterministic_observations: list,
    extraction_stats: Mapping[str, Any],
    post_tool_candidate_payload: Mapping[str, Any] | None,
    post_tool_candidate_usage: Mapping[str, Any] | None,
    candidate_extractor_family: str,
    candidate_extractor_version: str,
    candidate_extraction_mode: str,
) -> CandidateExtractionResult:
    """Run candidate extraction if enabled and not already processed.

    This is the ingestion-side candidate orchestration entry point. It checks
    feature flags, skips already-processed runs, and delegates to
    ``map_structured_payload`` for post-tool candidate payloads.
    """
    if not is_knowledge_candidate_extraction_enabled():
        return CandidateExtractionResult.skipped(
            reason="candidate_feature_disabled",
            policy_decision=CandidateExtractionPolicyDecision(
                action="skip",
                reason="candidate_feature_disabled",
                policy_metadata={"feature_flag_enabled": False},
            ),
        )
    existing_metadata = dict(run.run_metadata or {})
    existing_status = str(existing_metadata.get("candidate_extraction_status") or "").strip().lower()
    if existing_status in {"ran", "no_signal", "failed", "skipped"}:
        return CandidateExtractionResult.skipped(
            reason="already_processed",
            policy_decision=CandidateExtractionPolicyDecision(
                action="skip",
                reason="already_processed",
                policy_metadata={"feature_flag_enabled": True},
            ),
        )

    usage_summary = coerce_candidate_usage_summary(post_tool_candidate_usage)
    if not isinstance(post_tool_candidate_payload, Mapping):
        return CandidateExtractionResult.no_signal_result(
            reason="post_tool_candidate_payload_missing",
            policy_decision=CandidateExtractionPolicyDecision(
                action="no_signal",
                reason="post_tool_candidate_payload_missing",
                policy_metadata={
                    "bounded_evidence_count": len(archived_rows),
                    "deterministic_observation_count": len(deterministic_observations),
                },
            ),
            usage_summary=usage_summary,
        )

    if not archived_rows:
        return CandidateExtractionResult.no_signal_result(
            reason="no_archived_evidence",
            policy_decision=CandidateExtractionPolicyDecision(
                action="no_signal",
                reason="no_archived_evidence",
                policy_metadata={
                    "bounded_evidence_count": 0,
                    "deterministic_observation_count": len(deterministic_observations),
                },
            ),
            usage_summary=usage_summary,
        )

    execution = execution_payload.get("execution")
    execution_dict = dict(execution) if isinstance(execution, Mapping) else {}
    semantic_inputs = parse_semantic_inputs_from_execution(execution_dict)
    capability_family_raw = semantic_inputs.get("capability_family")
    capability_family = (
        str(capability_family_raw).strip()
        if isinstance(capability_family_raw, str)
        else None
    )

    existing_metadata = dict(run.run_metadata or {})
    replay_source_type = str(existing_metadata.get("replay_source_type") or "").strip().lower()
    if str(run.extractor_family).strip() == candidate_extractor_family:
        resolved_extractor_family = str(run.extractor_family)
        resolved_extractor_version = str(run.extractor_version)
    else:
        resolved_extractor_family = candidate_extractor_family
        resolved_extractor_version = candidate_extractor_version
    extraction_mode = (
        "candidate_replay"
        if replay_source_type in {"runtime", "durable_archive"}
        else candidate_extraction_mode
    )
    request = CandidateExtractionRequest(
        engagement_id=int(run.engagement_id),
        task_id=run.task_id,
        source_execution_id=str(run.source_execution_id),
        ingestion_run_id=str(run.id),
        extractor_family=resolved_extractor_family,
        extractor_version=resolved_extractor_version,
        extraction_mode=extraction_mode,
        tool_name=str(
            execution_dict.get("tool_name")
            or extraction_stats.get("source_tool_name")
            or ""
        ),
        capability_family=capability_family,
        evidence_archive_ids=tuple(str(row.id) for row in archived_rows),
    )
    artifact_kinds = _artifact_kinds_from_archives(archived_rows)
    if usage_summary.total_tokens > 0 or usage_summary.input_tokens > 0:
        policy_decision = KnowledgeCandidateExtractionPolicy.evaluate(
            CandidateExtractionPolicyRequest(
                deterministic_observation_count=len(deterministic_observations),
                native_observation_count=0,
                capability_family=capability_family,
                archived_evidence_count=len(archived_rows),
                artifact_kinds_present=artifact_kinds,
                artifact_kind_allowlist=artifact_kinds,
                estimated_prompt_tokens=int(usage_summary.input_tokens or 0),
                max_prompt_tokens=get_knowledge_candidate_max_prompt_tokens(),
                estimated_cost_usd=float(usage_summary.estimated_cost_usd or 0.0),
                max_cost_usd=get_knowledge_candidate_max_cost_usd(),
                pricing_status=str(usage_summary.pricing_status or "available"),
            )
        )
        if policy_decision.action == "skip":
            return CandidateExtractionResult.skipped(
                reason=policy_decision.reason,
                policy_decision=policy_decision,
            )
        if policy_decision.action == "no_signal":
            return CandidateExtractionResult.no_signal_result(
                reason=policy_decision.reason,
                policy_decision=policy_decision,
                usage_summary=usage_summary,
            )
    payload = normalize_post_tool_candidate_payload(
        payload=post_tool_candidate_payload,
        archived_rows=archived_rows,
    )
    bounded_evidence = build_bounded_evidence_for_mapping(archived_rows=archived_rows)
    return map_structured_payload(
        request=request,
        user_id=int(run.user_id),
        payload=payload,
        bounded_evidence=bounded_evidence,
        durable_masking_applied=False,
        usage_summary=usage_summary,
        minimum_vulnerability_confidence=get_knowledge_vulnerability_min_confidence(),
        enable_vulnerability_candidates=is_knowledge_vulnerability_candidates_enabled(),
    )


def record_candidate_usage_if_task_present(
    *,
    task_id: int | None,
    usage_summary: Mapping[str, Any] | None,
    source_label: str,
    source_execution_id: str,
    ingestion_run_id: str,
    resolve_task_user_id: Callable[[int], int | None],
    usage_tracking_service_factory: Callable[[], UsageTrackingService],
) -> None:
    """Record candidate extraction LLM usage for task accounting."""
    if task_id is None or not isinstance(usage_summary, Mapping):
        return
    try:
        user_id = resolve_task_user_id(int(task_id))
        if user_id is None:
            return
        provider_components = ProviderUsageComponents.from_mapping(
            usage_summary.get("provider_usage_components")
        )
        provider = str(usage_summary.get("provider") or "openai").strip().lower() or "openai"
        model = str(usage_summary.get("model") or "gpt-5-mini").strip() or "gpt-5-mini"
        api_surface = (
            str(usage_summary.get("api_surface") or "unknown").strip().lower()
            or "unknown"
        )
        usage = UsageData(
            prompt_tokens=int(usage_summary.get("input_tokens") or 0),
            completion_tokens=int(usage_summary.get("output_tokens") or 0),
            total_tokens=int(usage_summary.get("total_tokens") or 0),
            model=model,
            provider=provider,
            cached_tokens=int(usage_summary.get("cached_tokens") or 0),
            reasoning_tokens=int(usage_summary.get("reasoning_tokens") or 0),
            api_surface=api_surface,
            provider_usage_components=provider_components,
        )
        usage_tracking_service_factory().record_usage(
            task_id=int(task_id),
            user_id=user_id,
            usage=usage,
            source=str(source_label),
            metadata={
                "ingestion_run_id": str(ingestion_run_id),
                "source_execution_id": str(source_execution_id),
                "usage_kind": "durable_knowledge_candidate",
                "provider": provider,
                "api_surface": api_surface,
                "pricing_status": str(usage_summary.get("pricing_status") or "available"),
            },
        )
    except Exception:
        pass


def _artifact_kinds_from_archives(
    archived_rows: list[KnowledgeEvidenceArchive],
) -> tuple[str, ...]:
    """Return stable artifact kind labels for candidate policy metadata."""

    kinds: list[str] = []
    for row in archived_rows:
        metadata = row.archive_metadata if isinstance(row.archive_metadata, Mapping) else {}
        kind = (
            metadata.get("artifact_kind")
            or metadata.get("kind")
            or metadata.get("label")
            or row.mime_type
            or row.storage_mode
        )
        normalized = str(kind or "").strip().lower()
        if normalized:
            kinds.append(normalized)
    return tuple(sorted(set(kinds)))


__all__ = [
    "KnowledgeCandidateExtractionService",
    "maybe_run_candidate_extraction",
    "record_candidate_usage_if_task_present",
]
