"""Public API surface for candidate extraction components.

Scope:
- Re-export stable contracts, policy, and orchestration service symbols.

Boundary:
- Callers import from this package instead of module-level implementation files."""

from .contracts import (
    CandidateExtractionPolicyDecision,
    CandidateExtractionPolicyRequest,
    CandidateExtractionRequest,
    CandidateExtractionResult,
    CandidateExtractionStatus,
    CandidateExtractionUsageSummary,
    CandidatePolicyAction,
    ReplayEvidenceSource,
    build_candidate_run_metadata,
    candidate_reason_label,
    candidate_status_label,
    candidate_usage_dict,
    coerce_candidate_usage_summary,
)
from .evidence_reader import (
    build_bounded_evidence_for_mapping,
    normalize_post_tool_candidate_payload,
    resolve_archive_source_artifact_id,
)
from .policy import KnowledgeCandidateExtractionPolicy
from .service import (
    KnowledgeCandidateExtractionService,
    maybe_run_candidate_extraction,
    record_candidate_usage_if_task_present,
)
from .vulnerability_rules import (
    build_candidate_vulnerability_metrics,
    candidate_vulnerability_accepted_count,
    candidate_vulnerability_drop_reasons,
    candidate_vulnerability_metrics_from_metadata,
)

__all__ = [
    "CandidateExtractionPolicyDecision",
    "CandidateExtractionPolicyRequest",
    "CandidateExtractionRequest",
    "CandidateExtractionResult",
    "CandidateExtractionStatus",
    "CandidateExtractionUsageSummary",
    "CandidatePolicyAction",
    "KnowledgeCandidateExtractionPolicy",
    "KnowledgeCandidateExtractionService",
    "ReplayEvidenceSource",
    "build_candidate_run_metadata",
    "build_candidate_vulnerability_metrics",
    "build_bounded_evidence_for_mapping",
    "candidate_reason_label",
    "candidate_status_label",
    "candidate_usage_dict",
    "candidate_vulnerability_accepted_count",
    "candidate_vulnerability_drop_reasons",
    "candidate_vulnerability_metrics_from_metadata",
    "coerce_candidate_usage_summary",
    "maybe_run_candidate_extraction",
    "normalize_post_tool_candidate_payload",
    "record_candidate_usage_if_task_present",
    "resolve_archive_source_artifact_id",
]
