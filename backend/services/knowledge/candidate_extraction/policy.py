"""Deterministic policy logic for candidate-extractor invocation gating.

Scope:
- Evaluate a candidate extraction request policy envelope into run/skip/no-signal.

Boundary:
- Pure policy authority with no database, LLM, or prompt dependencies.
"""

from __future__ import annotations

from .contracts import CandidateExtractionPolicyDecision, CandidateExtractionPolicyRequest


class KnowledgeCandidateExtractionPolicy:
    """Deterministic policy authority for candidate-extractor invocation gating."""

    @staticmethod
    def evaluate(request: CandidateExtractionPolicyRequest) -> CandidateExtractionPolicyDecision:
        allowlisted_artifact_kinds = {
            str(kind).strip().lower() for kind in request.artifact_kind_allowlist if str(kind).strip()
        }
        present_artifact_kinds = {
            str(kind).strip().lower() for kind in request.artifact_kinds_present if str(kind).strip()
        }
        capability_family = str(request.capability_family or "").strip().lower()
        allowlisted_capability_families = {
            str(item).strip().lower() for item in request.capability_family_allowlist if str(item).strip()
        }
        matched_artifact_kinds = sorted(present_artifact_kinds.intersection(allowlisted_artifact_kinds))

        metadata = {
            "deterministic_observation_count": request.deterministic_observation_count,
            "native_observation_count": request.native_observation_count,
            "observation_count_finding_total": request.observation_count_finding_total,
            "observation_count_finding_authoritative": request.observation_count_finding_authoritative,
            "observation_count_non_finding_total": request.observation_count_non_finding_total,
            "archived_evidence_count": request.archived_evidence_count,
            "matched_artifact_kinds": matched_artifact_kinds,
            "estimated_prompt_tokens": request.estimated_prompt_tokens,
            "max_prompt_tokens": request.max_prompt_tokens,
            "estimated_cost_usd": request.estimated_cost_usd,
            "max_cost_usd": request.max_cost_usd,
            "pricing_status": request.pricing_status,
            "capability_family": capability_family or None,
            "compact_hint_present": bool(request.compact_output_hint),
        }

        if request.observation_count_finding_authoritative > 0:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="authoritative_signal_present",
                policy_metadata=metadata,
            )

        if not allowlisted_artifact_kinds:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="artifact_allowlist_empty",
                policy_metadata=metadata,
            )

        if request.archived_evidence_count <= 0:
            return CandidateExtractionPolicyDecision(
                action="no_signal",
                reason="no_archived_evidence",
                policy_metadata=metadata,
            )

        if not matched_artifact_kinds:
            return CandidateExtractionPolicyDecision(
                action="no_signal",
                reason="no_allowlisted_artifact_signal",
                policy_metadata=metadata,
            )

        if allowlisted_capability_families and capability_family not in allowlisted_capability_families:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="capability_family_not_allowlisted",
                policy_metadata=metadata,
            )

        if request.estimated_prompt_tokens <= 0:
            return CandidateExtractionPolicyDecision(
                action="no_signal",
                reason="empty_bounded_prompt_payload",
                policy_metadata=metadata,
            )

        if request.max_prompt_tokens <= 0:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="prompt_budget_disabled",
                policy_metadata=metadata,
            )

        if request.estimated_prompt_tokens > request.max_prompt_tokens:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="prompt_budget_exceeded",
                policy_metadata=metadata,
            )

        if request.max_cost_usd <= 0:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="cost_budget_disabled",
                policy_metadata=metadata,
            )

        if str(request.pricing_status or "").strip().lower() in {"unavailable", "partial"}:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="cost_pricing_unavailable",
                policy_metadata=metadata,
            )

        if request.estimated_cost_usd > request.max_cost_usd:
            return CandidateExtractionPolicyDecision(
                action="skip",
                reason="cost_budget_exceeded",
                policy_metadata=metadata,
            )

        return CandidateExtractionPolicyDecision(
            action="run",
            reason="eligible_for_candidate_extraction",
            policy_metadata=metadata,
        )


__all__ = ["KnowledgeCandidateExtractionPolicy"]
