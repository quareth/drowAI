"""Deterministic identity resolution service.

Responsibilities:
- Resolve observations into stable identity domains and canonical keys.
- Merge repeated observations into projector-ready decisions.

Boundary:
- No database writes.
- No projection-table mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any, Iterable, Mapping

from .identity.canonical_keys import build_relationship_edge_key
from .identity.merge_rules import (
    derive_identity_state,
    derive_rich_finding_details,
    merge_evidence_refs,
    merge_confidence_with_corroboration,
    merge_rich_details,
    merge_seen_timestamps,
    merge_state_with_contradictions,
)
from .contracts import ObservationCreate, validate_subject_key_matches_type
from .severity_policy import resolve_finding_severity


@dataclass(frozen=True)
class ResolvedIdentityObservation:
    """One observation resolved into a stable identity key."""

    identity_domain: str
    identity_key: str
    observation_type: str
    subject_type: str
    subject_key: str
    assertion_level: str
    observed_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    observation_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IdentityMergeDecision:
    """Deterministic merge decision for one identity key."""

    identity_domain: str
    identity_key: str
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int = 0
    confidence: str | None = None
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    source_subject_types: set[str] = field(default_factory=set)
    source_observation_types: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityResolutionResult:
    """Resolved observations and aggregate merge decisions."""

    resolved_observations: list[ResolvedIdentityObservation]
    merge_decisions: dict[str, IdentityMergeDecision]


class KnowledgeIdentityService:
    """Resolve observations into stable identity keys and merge state."""

    def resolve_observations(
        self,
        observations: Iterable[ObservationCreate],
    ) -> IdentityResolutionResult:
        resolved: list[ResolvedIdentityObservation] = []
        decisions: dict[str, IdentityMergeDecision] = {}

        for observation in sorted(observations, key=self._observation_sort_key):
            resolved_item = self._resolve_one(observation)
            if resolved_item is None:
                continue
            resolved.append(resolved_item)
            marker = f"{resolved_item.identity_domain}:{resolved_item.identity_key}"
            existing = decisions.get(marker)
            if existing is None:
                existing = IdentityMergeDecision(
                    identity_domain=resolved_item.identity_domain,
                    identity_key=resolved_item.identity_key,
                    first_seen_at=resolved_item.observed_at,
                    last_seen_at=resolved_item.observed_at,
                )
                decisions[marker] = existing

            existing.first_seen_at, existing.last_seen_at = merge_seen_timestamps(
                first_seen_at=existing.first_seen_at,
                last_seen_at=existing.last_seen_at,
                observed_at=resolved_item.observed_at,
            )
            existing.observation_count += 1
            existing.source_subject_types.add(resolved_item.subject_type)
            existing.source_observation_types.add(resolved_item.observation_type)

            payload = dict(resolved_item.payload or {})
            existing.confidence = merge_confidence_with_corroboration(
                current=existing.confidence,
                incoming=str(payload.get("confidence") or ""),
                is_corroborated=existing.observation_count > 1,
            )
            incoming_refs = payload.get("evidence_refs")
            existing.evidence_refs = merge_evidence_refs(
                existing=existing.evidence_refs,
                incoming=incoming_refs if isinstance(incoming_refs, list) else None,
            )
            incoming_state = derive_identity_state(
                observation_type=resolved_item.observation_type,
                subject_type=resolved_item.subject_type,
                payload=payload,
            )
            severity_resolution = None
            if resolved_item.observation_type.startswith("finding."):
                severity_resolution = resolve_finding_severity(
                    observation_type=resolved_item.observation_type,
                    assertion_level=resolved_item.assertion_level,
                    payload=payload,
                )
                if severity_resolution is not None:
                    incoming_state["severity"] = severity_resolution.severity
            merged_state, contradictions = merge_state_with_contradictions(
                existing_state=existing.metadata.get("state"),
                incoming_state=incoming_state,
                observed_at=resolved_item.observed_at,
            )
            metadata = dict(existing.metadata or {})
            metadata["state"] = merged_state
            if contradictions:
                previous = metadata.get("contradictions")
                contradiction_rows = list(previous) if isinstance(previous, list) else []
                contradiction_rows.extend(contradictions)
                metadata["contradictions"] = contradiction_rows
            if severity_resolution is not None:
                metadata["severity_resolution"] = severity_resolution.to_metadata()

            # Merge additive rich finding details outside contradiction-tracked state
            incoming_rich = derive_rich_finding_details(
                observation_type=resolved_item.observation_type,
                payload=payload,
            )
            if incoming_rich:
                merged_rich = merge_rich_details(
                    existing=metadata.get("rich_details"),
                    incoming=incoming_rich,
                )
                if merged_rich:
                    metadata["rich_details"] = merged_rich

            existing.metadata = metadata

        return IdentityResolutionResult(
            resolved_observations=resolved,
            merge_decisions=decisions,
        )

    def _resolve_one(self, observation: ObservationCreate) -> ResolvedIdentityObservation | None:
        observation_type = str(observation.observation_type or "").strip().lower()
        subject_type, subject_key = validate_subject_key_matches_type(
            subject_type=str(observation.subject_type or ""),
            subject_key=str(observation.subject_key or ""),
        )
        payload = dict(observation.payload or {})
        observed_at = observation.observed_at

        identity = self._resolve_identity_key(
            observation_type=observation_type,
            subject_type=subject_type,
            subject_key=subject_key,
            payload=payload,
        )
        if identity is None:
            return None

        return ResolvedIdentityObservation(
            identity_domain=identity[0],
            identity_key=identity[1],
            observation_type=observation_type,
            subject_type=subject_type,
            subject_key=subject_key,
            assertion_level=str(observation.assertion_level or "").strip().lower(),
            observed_at=observed_at,
            payload=payload,
            observation_metadata=dict(observation.observation_metadata or {}),
        )

    def _resolve_identity_key(
        self,
        *,
        observation_type: str,
        subject_type: str,
        subject_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        if subject_type in {"host.ip", "host.dns"}:
            return "asset", subject_key

        if subject_type == "service.socket":
            return "service", subject_key

        if observation_type.startswith("finding."):
            return "finding", subject_key

        if observation_type.startswith("relationship.") or subject_type == "relationship.edge":
            canonical_relationship_key = self._resolve_relationship_key(subject_key=subject_key, payload=payload)
            return "relationship", canonical_relationship_key

        return None

    @staticmethod
    def _resolve_relationship_key(*, subject_key: str, payload: Mapping[str, Any]) -> str:
        source_subject_key = str(payload.get("source_subject_key") or "").strip().lower()
        relationship_type = str(payload.get("relationship_type") or "").strip().lower()
        target_subject_key = str(payload.get("target_subject_key") or "").strip().lower()
        if source_subject_key and relationship_type and target_subject_key:
            return build_relationship_edge_key(
                source_subject_key=source_subject_key,
                relationship_type=relationship_type,
                target_subject_key=target_subject_key,
            )
        raise ValueError(
            "relationship.edge observations require payload.source_subject_key, "
            "payload.relationship_type, and payload.target_subject_key"
        )

    @staticmethod
    def _observation_sort_key(observation: ObservationCreate) -> tuple[str, str, str, str, str, str, str]:
        """Build deterministic order key so merge outcomes are replay-stable."""
        observed_at = observation.observed_at
        observed_marker = (
            observed_at.isoformat() if isinstance(observed_at, datetime) else str(observed_at or "")
        )
        payload = dict(observation.payload or {})
        try:
            payload_marker = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        except TypeError:
            payload_marker = json.dumps(
                {str(key): str(value) for key, value in payload.items()},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        return (
            observed_marker,
            str(observation.observation_type or "").strip().lower(),
            str(observation.subject_type or "").strip().lower(),
            str(observation.subject_key or "").strip().lower(),
            str(observation.source_execution_id or "").strip(),
            str(observation.assertion_level or "").strip().lower(),
            payload_marker,
        )
