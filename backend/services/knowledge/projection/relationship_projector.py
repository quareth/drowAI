"""Relationship projector for deterministic tenant/user-scoped knowledge_relationships upserts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import KnowledgeRelationship
from ..identity.canonical_keys import build_relationship_edge_key
from ..contracts import validate_subject_key_matches_type
from ..identity_service import IdentityMergeDecision, ResolvedIdentityObservation


@dataclass(frozen=True)
class RelationshipUpsertResult:
    """Result envelope for one relationship upsert call."""

    row: KnowledgeRelationship
    inserted: bool


class RelationshipProjector:
    """Upsert durable relationship rows from identity merge decisions."""

    def upsert(
        self,
        *,
        db: Session,
        user_id: int,
        decision: IdentityMergeDecision,
        merged_state: Mapping[str, Any],
        resolved_observations: Iterable[ResolvedIdentityObservation],
        engagement_id: int | None = None,
        tenant_id: int,
    ) -> RelationshipUpsertResult:
        existing = self._resolve_existing_row(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            relationship_key=decision.identity_key,
        )
        inserted = existing is None
        row = existing or KnowledgeRelationship(
            tenant_id=tenant_id,
            user_id=int(user_id),
            engagement_id=engagement_id,
            relationship_key=decision.identity_key,
            source_subject_key="",
            relationship_type="",
            target_subject_key="",
            first_seen_at=decision.first_seen_at,
            last_seen_at=decision.last_seen_at,
        )

        representative = list(resolved_observations)[-1] if resolved_observations else None
        payload = dict(representative.payload or {}) if representative is not None else {}
        metadata = dict(merged_state.get("metadata") or {})

        source_key, relationship_type, target_key = self._resolve_triplet(
            relationship_key=decision.identity_key,
            payload=payload,
        )

        row.source_subject_key = source_key
        row.tenant_id = int(tenant_id)
        row.relationship_type = relationship_type
        row.target_subject_key = target_key
        row.confidence = merged_state.get("confidence")
        row.first_seen_at = merged_state.get("first_seen_at")
        row.last_seen_at = merged_state.get("last_seen_at")
        row.relationship_metadata = metadata

        if inserted:
            db.add(row)
        db.flush()
        return RelationshipUpsertResult(row=row, inserted=inserted)

    @staticmethod
    def _resolve_existing_row(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        relationship_key: str,
    ) -> KnowledgeRelationship | None:
        return db.execute(
            select(KnowledgeRelationship).where(
                KnowledgeRelationship.tenant_id == int(tenant_id),
                KnowledgeRelationship.user_id == int(user_id),
                KnowledgeRelationship.relationship_key == str(relationship_key),
            )
        ).scalar_one_or_none()

    @staticmethod
    def _resolve_triplet(
        *,
        relationship_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        source = str(payload.get("source_subject_key") or "").strip().lower()
        relation = str(payload.get("relationship_type") or "").strip().lower()
        target = str(payload.get("target_subject_key") or "").strip().lower()
        if source and relation and target:
            return source, relation, target

        key = str(relationship_key or "").strip().lower()
        prefix = "relationship.edge:"
        if key.startswith(prefix):
            tail = key[len(prefix):]
            parts = tail.split(":")
            for relation_index in range(1, len(parts) - 1):
                relation_guess = parts[relation_index].strip()
                source_guess = ":".join(parts[:relation_index]).strip()
                target_guess = ":".join(parts[relation_index + 1:]).strip()
                resolved_source = source or source_guess
                resolved_relation = relation or relation_guess
                resolved_target = target or target_guess
                if not (
                    resolved_source
                    and resolved_relation
                    and resolved_target
                    and RelationshipProjector._is_subject_key(resolved_source)
                    and RelationshipProjector._is_subject_key(resolved_target)
                ):
                    continue
                try:
                    canonical = build_relationship_edge_key(
                        source_subject_key=resolved_source,
                        relationship_type=resolved_relation,
                        target_subject_key=resolved_target,
                    )
                except ValueError:
                    continue
                if canonical == key:
                    return resolved_source, resolved_relation, resolved_target

        return source, relation, target

    @staticmethod
    def _is_subject_key(value: str) -> bool:
        key = str(value or "").strip().lower()
        subject_type, separator, _ = key.partition(":")
        if not separator:
            return False
        try:
            validate_subject_key_matches_type(subject_type=subject_type, subject_key=key)
        except ValueError:
            return False
        return True
