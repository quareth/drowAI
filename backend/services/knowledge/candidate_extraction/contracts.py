"""Contracts and result interpretation for candidate extraction.

Scope:
- Input/output dataclasses and status/action literals used by extraction flow.
- Result interpretation helpers: usage coercion, status/reason labels, usage dicts.
- Candidate run metadata assembly from extraction results.

Boundary:
- Pure data transformation and validation; no orchestration or persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from ..contracts import ObservationCreate
from ..evidence_read_service import KnowledgeEvidenceReadMode


CandidatePolicyAction = Literal["skip", "run", "no_signal"]
CandidateExtractionStatus = Literal["skipped", "succeeded", "no_signal", "failed"]


@dataclass(slots=True, frozen=True)
class CandidateExtractionPolicyRequest:
    """Input contract for one deterministic candidate-extraction policy decision."""

    deterministic_observation_count: int
    native_observation_count: int
    capability_family: str | None
    archived_evidence_count: int
    artifact_kinds_present: tuple[str, ...]
    artifact_kind_allowlist: tuple[str, ...]
    capability_family_allowlist: tuple[str, ...] = ()
    observation_count_finding_total: int = 0
    observation_count_finding_authoritative: int = 0
    observation_count_non_finding_total: int = 0
    estimated_prompt_tokens: int = 0
    max_prompt_tokens: int = 0
    estimated_cost_usd: float = 0.0
    max_cost_usd: float = 0.0
    pricing_status: str = "available"
    compact_output_hint: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.deterministic_observation_count < 0:
            raise ValueError("deterministic_observation_count must be >= 0")
        if self.native_observation_count < 0:
            raise ValueError("native_observation_count must be >= 0")
        if self.observation_count_finding_total < 0:
            raise ValueError("observation_count_finding_total must be >= 0")
        if self.observation_count_finding_authoritative < 0:
            raise ValueError("observation_count_finding_authoritative must be >= 0")
        if self.observation_count_non_finding_total < 0:
            raise ValueError("observation_count_non_finding_total must be >= 0")
        if self.observation_count_finding_authoritative > self.observation_count_finding_total:
            raise ValueError(
                "observation_count_finding_authoritative cannot exceed observation_count_finding_total"
            )
        if (
            self.observation_count_finding_total + self.observation_count_non_finding_total
            > self.deterministic_observation_count
        ):
            raise ValueError(
                "finding/non-finding observation totals cannot exceed deterministic_observation_count"
            )
        if self.archived_evidence_count < 0:
            raise ValueError("archived_evidence_count must be >= 0")
        if self.estimated_prompt_tokens < 0:
            raise ValueError("estimated_prompt_tokens must be >= 0")
        if self.max_prompt_tokens < 0:
            raise ValueError("max_prompt_tokens must be >= 0")
        if self.estimated_cost_usd < 0:
            raise ValueError("estimated_cost_usd must be >= 0")
        if self.max_cost_usd < 0:
            raise ValueError("max_cost_usd must be >= 0")


@dataclass(slots=True, frozen=True)
class CandidateExtractionPolicyDecision:
    """Deterministic policy decision for one ingestion execution unit."""

    action: CandidatePolicyAction
    reason: str
    policy_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ReplayEvidenceSource:
    """Replay-resolved evidence source normalized through bounded read policy."""

    evidence_archive_id: str
    artifact_kind: str
    content: str
    mode: KnowledgeEvidenceReadMode = "head"
    query: str | None = None


@dataclass(slots=True, frozen=True)
class CandidateExtractionRequest:
    """Typed request contract for one candidate extraction call."""

    engagement_id: int
    source_execution_id: str
    ingestion_run_id: str
    extractor_family: str
    extractor_version: str
    extraction_mode: str
    tool_name: str
    capability_family: str | None
    task_id: int | None = None
    evidence_archive_ids: tuple[str, ...] = ()
    replay_sources: tuple[ReplayEvidenceSource, ...] = ()
    compact_output_hint: Mapping[str, Any] | None = None
    max_evidence_items: int = 6
    max_evidence_chars_per_item: int = 1800
    llm_max_tokens: int = 1200
    llm_temperature: float = 0.0

    def __post_init__(self) -> None:
        if int(self.engagement_id) <= 0:
            raise ValueError("engagement_id must be a positive integer")
        if not str(self.source_execution_id).strip():
            raise ValueError("source_execution_id is required")
        if not str(self.ingestion_run_id).strip():
            raise ValueError("ingestion_run_id is required")
        if not str(self.extractor_family).strip():
            raise ValueError("extractor_family is required")
        if not str(self.extractor_version).strip():
            raise ValueError("extractor_version is required")
        if not str(self.extraction_mode).strip():
            raise ValueError("extraction_mode is required")
        if not str(self.tool_name).strip():
            raise ValueError("tool_name is required")
        if int(self.max_evidence_items) <= 0:
            raise ValueError("max_evidence_items must be >= 1")
        if int(self.max_evidence_chars_per_item) <= 0:
            raise ValueError("max_evidence_chars_per_item must be >= 1")
        if int(self.llm_max_tokens) <= 0:
            raise ValueError("llm_max_tokens must be >= 1")
        if float(self.llm_temperature) < 0:
            raise ValueError("llm_temperature must be >= 0")


@dataclass(slots=True, frozen=True)
class CandidateExtractionUsageSummary:
    """Portable usage summary preserved on durable run metadata."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    pricing_status: str = "available"
    provider: str = "openai"
    model: str = "gpt-5-mini"
    api_surface: str = "unknown"
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    provider_usage_components: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError("input_tokens must be >= 0")
        if self.output_tokens < 0:
            raise ValueError("output_tokens must be >= 0")
        if self.total_tokens < 0:
            raise ValueError("total_tokens must be >= 0")
        if self.estimated_cost_usd < 0:
            raise ValueError("estimated_cost_usd must be >= 0")
        if self.cached_tokens < 0:
            raise ValueError("cached_tokens must be >= 0")
        if self.reasoning_tokens < 0:
            raise ValueError("reasoning_tokens must be >= 0")


@dataclass(slots=True, frozen=True)
class CandidateExtractionResult:
    """Typed outcome envelope for candidate-extraction orchestration."""

    status: CandidateExtractionStatus
    observations: tuple[ObservationCreate, ...] = ()
    analyst_notes: tuple[str, ...] = ()
    no_signal: bool = False
    evidence_archive_ids_used: tuple[str, ...] = ()
    durable_masking_applied: bool = False
    usage_summary: CandidateExtractionUsageSummary | None = None
    failure_reason: str | None = None
    policy_decision: CandidateExtractionPolicyDecision | None = None

    def __post_init__(self) -> None:
        if self.status == "failed" and not str(self.failure_reason or "").strip():
            raise ValueError("failure_reason is required for failed status")
        if self.status in {"succeeded", "no_signal", "skipped"} and self.failure_reason is not None:
            raise ValueError("failure_reason is only allowed for failed status")
        if self.status == "no_signal" and not self.no_signal:
            raise ValueError("no_signal status requires no_signal=True")
        if self.status == "succeeded" and self.no_signal:
            raise ValueError("succeeded status cannot set no_signal=True")
        if self.status in {"skipped", "no_signal", "failed"} and self.observations:
            raise ValueError("only succeeded status can include observations")
        for observation in self.observations:
            if str(observation.assertion_level).strip().lower() != "candidate":
                raise ValueError("candidate extraction observations must use assertion_level='candidate'")

    @classmethod
    def skipped(
        cls,
        *,
        reason: str,
        policy_decision: CandidateExtractionPolicyDecision,
    ) -> CandidateExtractionResult:
        return cls(
            status="skipped",
            policy_decision=CandidateExtractionPolicyDecision(
                action="skip",
                reason=str(reason or "").strip() or "policy_skip",
                policy_metadata=dict(policy_decision.policy_metadata or {}),
            ),
        )

    @classmethod
    def no_signal_result(
        cls,
        *,
        reason: str,
        policy_decision: CandidateExtractionPolicyDecision,
        evidence_archive_ids_used: Sequence[str] = (),
        durable_masking_applied: bool = False,
        usage_summary: CandidateExtractionUsageSummary | None = None,
    ) -> CandidateExtractionResult:
        return cls(
            status="no_signal",
            no_signal=True,
            evidence_archive_ids_used=tuple(str(item) for item in evidence_archive_ids_used if str(item)),
            durable_masking_applied=bool(durable_masking_applied),
            usage_summary=usage_summary,
            policy_decision=CandidateExtractionPolicyDecision(
                action="no_signal",
                reason=str(reason or "").strip() or "no_signal",
                policy_metadata=dict(policy_decision.policy_metadata or {}),
            ),
        )

    @classmethod
    def succeeded(
        cls,
        *,
        observations: Sequence[ObservationCreate],
        analyst_notes: Sequence[str] = (),
        evidence_archive_ids_used: Sequence[str] = (),
        durable_masking_applied: bool = False,
        usage_summary: CandidateExtractionUsageSummary | None = None,
        policy_decision: CandidateExtractionPolicyDecision | None = None,
    ) -> CandidateExtractionResult:
        return cls(
            status="succeeded",
            observations=tuple(observations),
            analyst_notes=tuple(str(note) for note in analyst_notes if str(note).strip()),
            no_signal=False,
            evidence_archive_ids_used=tuple(str(item) for item in evidence_archive_ids_used if str(item)),
            durable_masking_applied=bool(durable_masking_applied),
            usage_summary=usage_summary,
            policy_decision=policy_decision,
        )

    @classmethod
    def failed(
        cls,
        *,
        reason: str,
        policy_decision: CandidateExtractionPolicyDecision | None = None,
        usage_summary: CandidateExtractionUsageSummary | None = None,
    ) -> CandidateExtractionResult:
        return cls(
            status="failed",
            failure_reason=str(reason or "").strip() or "candidate_extraction_failed",
            usage_summary=usage_summary,
            policy_decision=policy_decision,
        )


def coerce_candidate_usage_summary(
    usage: Mapping[str, Any] | None,
) -> CandidateExtractionUsageSummary:
    """Coerce optional usage mapping into candidate usage summary contract."""
    if not isinstance(usage, Mapping):
        return CandidateExtractionUsageSummary()

    def _to_int(*keys: str) -> int:
        for key in keys:
            try:
                value = usage.get(key)
                if value is None:
                    continue
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    def _to_float(*keys: str) -> float:
        for key in keys:
            try:
                value = usage.get(key)
                if value is None:
                    continue
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    input_tokens = max(0, _to_int("input_tokens", "prompt_tokens"))
    output_tokens = max(0, _to_int("output_tokens", "completion_tokens"))
    total_tokens = max(
        0,
        _to_int("total_tokens", "all_tokens", "tokens_total") or (input_tokens + output_tokens),
    )
    estimated_cost_usd = max(0.0, _to_float("estimated_cost_usd"))
    pricing_status = str(usage.get("pricing_status") or "available").strip() or "available"
    provider = str(usage.get("provider") or "openai").strip().lower() or "openai"
    model = str(usage.get("model") or "gpt-5-mini").strip() or "gpt-5-mini"
    api_surface = str(usage.get("api_surface") or "unknown").strip().lower() or "unknown"
    provider_usage_components = usage.get("provider_usage_components")
    return CandidateExtractionUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost_usd,
        pricing_status=pricing_status,
        provider=provider,
        model=model,
        api_surface=api_surface,
        cached_tokens=max(0, _to_int("cached_tokens")),
        reasoning_tokens=max(0, _to_int("reasoning_tokens")),
        provider_usage_components=(
            dict(provider_usage_components)
            if isinstance(provider_usage_components, Mapping)
            else None
        ),
    )


def candidate_status_label(result: CandidateExtractionResult) -> str:
    """Map extraction result status to durable run metadata label."""
    mapping = {
        "succeeded": "ran",
        "no_signal": "no_signal",
        "failed": "failed",
        "skipped": "skipped",
    }
    return mapping.get(str(result.status), str(result.status))


def candidate_reason_label(result: CandidateExtractionResult) -> str | None:
    """Derive human-readable reason label from extraction result."""
    if result.failure_reason:
        return str(result.failure_reason)
    if result.policy_decision is not None:
        return str(result.policy_decision.reason)
    if str(result.status).strip().lower() == "succeeded":
        return "candidates_extracted"
    return None


def candidate_usage_dict(result: CandidateExtractionResult) -> dict[str, Any] | None:
    """Convert extraction result usage summary to serializable dict, or None."""
    usage = result.usage_summary
    if usage is None:
        return None
    input_tokens = int(usage.input_tokens or 0)
    output_tokens = int(usage.output_tokens or 0)
    total_tokens = int(usage.total_tokens or 0)
    estimated_cost_usd = float(usage.estimated_cost_usd or 0.0)
    if input_tokens <= 0 and output_tokens <= 0 and total_tokens <= 0 and estimated_cost_usd <= 0:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


def build_candidate_run_metadata(
    *,
    candidate_result: CandidateExtractionResult,
    existing_run_metadata: Mapping[str, Any],
    candidate_duration_seconds: float,
    minimum_confidence: float,
    candidate_extractor_family: str,
    candidate_extractor_version: str,
    candidate_extraction_mode: str,
) -> dict[str, Any]:
    """Build the ``candidate_*`` keys for ``run.run_metadata``.

    Pure data transformation on candidate types. Includes the
    ``already_processed`` merge branch.
    """
    from .vulnerability_rules import (
        build_candidate_vulnerability_metrics,
        candidate_vulnerability_metrics_from_metadata,
    )

    status = candidate_status_label(candidate_result)
    reason = candidate_reason_label(candidate_result)
    observation_count = len(candidate_result.observations)
    evidence_count = len(candidate_result.evidence_archive_ids_used)
    durable_masking_applied = bool(candidate_result.durable_masking_applied)
    usage_summary = candidate_usage_dict(candidate_result)
    vuln_metrics = build_candidate_vulnerability_metrics(
        candidate_result, minimum_confidence=minimum_confidence,
    )

    if reason == "already_processed":
        status = str(existing_run_metadata.get("candidate_extraction_status") or status)
        reason = str(existing_run_metadata.get("candidate_extraction_reason") or reason)
        observation_count = int(
            existing_run_metadata.get("candidate_observation_count") or observation_count
        )
        evidence_count = int(
            existing_run_metadata.get("candidate_evidence_count") or evidence_count
        )
        durable_masking_applied = bool(
            existing_run_metadata.get("candidate_durable_masking_applied")
            if existing_run_metadata.get("candidate_durable_masking_applied") is not None
            else durable_masking_applied
        )
        previous_usage = existing_run_metadata.get("candidate_usage_summary")
        if usage_summary is None and isinstance(previous_usage, Mapping):
            usage_summary = candidate_usage_dict(
                CandidateExtractionResult.succeeded(
                    observations=(),
                    usage_summary=coerce_candidate_usage_summary(previous_usage),
                )
            )
        vuln_metrics = candidate_vulnerability_metrics_from_metadata(
            existing_run_metadata=existing_run_metadata,
            fallback_metrics=vuln_metrics,
            minimum_confidence=minimum_confidence,
        )

    return {
        "candidate_extraction_status": status,
        "candidate_extraction_reason": reason,
        "candidate_observation_count": observation_count,
        "candidate_evidence_count": evidence_count,
        "candidate_evidence_archive_ids_used": list(candidate_result.evidence_archive_ids_used),
        "candidate_durable_masking_applied": durable_masking_applied,
        "candidate_extraction_duration_seconds": candidate_duration_seconds,
        "candidate_audit_summary": {
            "outcome_status": status,
            "outcome_reason": reason,
            "durable_masking_applied": durable_masking_applied,
            "evidence_archive_ids_used": list(candidate_result.evidence_archive_ids_used),
            "duration_seconds": candidate_duration_seconds,
            "vulnerability_attempt_count": vuln_metrics["attempt_count"],
            "vulnerability_below_threshold_drop_count": vuln_metrics["below_threshold_drop_count"],
            "vulnerability_accepted_count": vuln_metrics["accepted_count"],
            "vulnerability_threshold_used": vuln_metrics["threshold_used"],
        },
        "candidate_usage_summary": usage_summary,
        "candidate_vulnerability_attempt_count": vuln_metrics["attempt_count"],
        "candidate_vulnerability_below_threshold_drop_count": vuln_metrics["below_threshold_drop_count"],
        "candidate_vulnerability_accepted_count": vuln_metrics["accepted_count"],
        "candidate_vulnerability_threshold_used": vuln_metrics["threshold_used"],
        "candidate_vulnerability_drop_reasons": vuln_metrics["drop_reasons"],
        "candidate_extractor_family": candidate_extractor_family,
        "candidate_extractor_version": candidate_extractor_version,
        "candidate_extraction_mode": candidate_extraction_mode,
    }


__all__ = [
    "CandidateExtractionPolicyDecision",
    "CandidateExtractionPolicyRequest",
    "CandidateExtractionRequest",
    "CandidateExtractionResult",
    "CandidateExtractionStatus",
    "CandidateExtractionUsageSummary",
    "CandidatePolicyAction",
    "ReplayEvidenceSource",
    "build_candidate_run_metadata",
    "candidate_reason_label",
    "candidate_status_label",
    "candidate_usage_dict",
    "coerce_candidate_usage_summary",
]
