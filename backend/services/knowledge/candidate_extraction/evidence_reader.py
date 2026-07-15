"""Evidence loading, bounded selection, and payload normalization for candidate extraction.

Scope:
- Read durable evidence rows and replay evidence payloads.
- Normalize bounded content and produce deduplicated evidence bundles.
- Normalize post-tool candidate payloads and remap artifact refs to archive ids.
- Resolve archive source artifact identifiers from lineage snapshots.

Boundary:
- Pure evidence preparation and normalization; no prompt, LLM, or orchestration logic.
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.core import Engagement
from backend.models.knowledge import KnowledgeEvidenceArchive
from ..evidence_read_service import (
    KnowledgeEvidenceReadRequest,
    KnowledgeEvidenceReadService,
)

from .contracts import CandidateExtractionRequest


class CandidateEvidenceCollector:
    """Collect bounded durable/replay evidence for candidate extraction."""

    def __init__(
        self,
        db: Session,
        *,
        evidence_read_service: KnowledgeEvidenceReadService,
    ) -> None:
        self.db = db
        self.evidence_read_service = evidence_read_service

    def collect_bounded_evidence(
        self,
        *,
        request: CandidateExtractionRequest,
    ) -> list[dict[str, Any]]:
        durable_evidence = self.read_durable_evidence(request=request)
        replay_evidence = self.read_replay_evidence(request=request)
        combined: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in [*durable_evidence, *replay_evidence]:
            evidence_id = str(item.get("evidence_archive_id") or "").strip()
            content = str(item.get("content") or "")
            if not evidence_id or not content.strip():
                continue
            if evidence_id in seen_ids:
                continue
            seen_ids.add(evidence_id)
            combined.append(item)
            if len(combined) >= int(request.max_evidence_items):
                break
        return combined

    def read_durable_evidence(
        self,
        *,
        request: CandidateExtractionRequest,
    ) -> list[dict[str, Any]]:
        tenant_id = self._resolve_engagement_tenant_id(engagement_id=int(request.engagement_id))
        if tenant_id is None:
            return []
        user_id = self._resolve_engagement_user_id(engagement_id=int(request.engagement_id))
        if user_id is None:
            return []
        row_query = select(KnowledgeEvidenceArchive).where(
            KnowledgeEvidenceArchive.tenant_id == int(tenant_id),
            KnowledgeEvidenceArchive.user_id == int(user_id),
            KnowledgeEvidenceArchive.engagement_id == int(request.engagement_id),
            KnowledgeEvidenceArchive.source_execution_id == str(request.source_execution_id),
        )
        requested_ids = [item for item in request.evidence_archive_ids if str(item).strip()]
        if requested_ids:
            row_query = row_query.where(KnowledgeEvidenceArchive.id.in_(requested_ids))
        row_query = row_query.order_by(KnowledgeEvidenceArchive.created_at.asc())
        rows = self.db.execute(row_query).scalars().all()
        if not rows:
            return []

        results: list[dict[str, Any]] = []
        read_request = KnowledgeEvidenceReadRequest(
            mode="head",
            max_chars=int(request.max_evidence_chars_per_item),
        )
        for row in rows:
            read_result = self.evidence_read_service.read_evidence(
                tenant_id=int(tenant_id),
                user_id=int(user_id),
                engagement_id=int(request.engagement_id),
                evidence_id=str(row.id),
                request=read_request,
            )
            if read_result.status != "ready" or not str(read_result.content or "").strip():
                continue
            lineage = dict(row.lineage_snapshot or {})
            results.append(
                {
                    "evidence_archive_id": str(row.id),
                    "artifact_kind": str(lineage.get("artifact_kind") or "unknown"),
                    "mode_used": read_result.mode_used,
                    "content": str(read_result.content),
                }
            )
            if len(results) >= int(request.max_evidence_items):
                break
        return results

    def _resolve_engagement_user_id(self, *, engagement_id: int) -> int | None:
        resolved_user_id = self.db.execute(
            select(Engagement.user_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if resolved_user_id is None:
            return None
        try:
            return int(resolved_user_id)
        except (TypeError, ValueError):
            return None

    def _resolve_engagement_tenant_id(self, *, engagement_id: int) -> int | None:
        resolved_tenant_id = self.db.execute(
            select(Engagement.tenant_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if resolved_tenant_id is None:
            return None
        try:
            parsed = int(resolved_tenant_id)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def read_replay_evidence(
        self,
        *,
        request: CandidateExtractionRequest,
    ) -> list[dict[str, Any]]:
        if not request.replay_sources:
            return []
        results: list[dict[str, Any]] = []
        for source in request.replay_sources:
            content = str(source.content or "")
            if not content.strip():
                continue
            bounded, _, mode_used = KnowledgeEvidenceReadService._apply_mode(  # noqa: SLF001
                text=content,
                request=KnowledgeEvidenceReadRequest(
                    mode=source.mode,
                    query=source.query,
                    max_chars=int(request.max_evidence_chars_per_item),
                ),
            )
            if not str(bounded or "").strip():
                continue
            results.append(
                {
                    "evidence_archive_id": str(source.evidence_archive_id),
                    "artifact_kind": str(source.artifact_kind or "replay_payload"),
                    "mode_used": mode_used,
                    "content": str(bounded),
                }
            )
            if len(results) >= int(request.max_evidence_items):
                break
        return results


def resolve_archive_source_artifact_id(row: KnowledgeEvidenceArchive) -> str:
    """Resolve source artifact identifier from archive row lineage."""
    direct = str(row.source_artifact_id or "").strip()
    if direct:
        return direct
    lineage = dict(row.lineage_snapshot or {})
    return str(lineage.get("artifact_id") or "").strip()


def build_bounded_evidence_for_mapping(
    *,
    archived_rows: list[KnowledgeEvidenceArchive],
) -> list[dict[str, str]]:
    """Build minimal bounded evidence envelope accepted by mapping logic."""
    bounded: list[dict[str, str]] = []
    for row in archived_rows:
        evidence_archive_id = str(row.id or "").strip()
        if not evidence_archive_id:
            continue
        entry = {"evidence_archive_id": evidence_archive_id}
        source_artifact_id = resolve_archive_source_artifact_id(row)
        if source_artifact_id:
            entry["source_artifact_id"] = source_artifact_id
        bounded.append(entry)
    return bounded


def normalize_post_tool_candidate_payload(
    *,
    payload: Mapping[str, Any],
    archived_rows: list[KnowledgeEvidenceArchive],
) -> dict[str, Any]:
    """Normalize candidate payload and remap source artifact refs to archive ids."""
    archive_id_set = {
        str(row.id).strip()
        for row in archived_rows
        if str(row.id or "").strip()
    }
    source_artifact_to_archive: dict[str, str] = {}
    for row in archived_rows:
        evidence_archive_id = str(row.id or "").strip()
        if not evidence_archive_id:
            continue
        source_artifact_id = resolve_archive_source_artifact_id(row)
        if source_artifact_id:
            source_artifact_to_archive.setdefault(source_artifact_id, evidence_archive_id)

    candidate_rows_raw = payload.get("candidate_observations")
    normalized_rows: list[dict[str, Any]] = []
    if isinstance(candidate_rows_raw, list):
        for row in candidate_rows_raw:
            if not isinstance(row, Mapping):
                continue
            normalized_row = dict(row)
            refs_raw = normalized_row.get("evidence_refs")
            normalized_refs: list[dict[str, str]] = []
            if isinstance(refs_raw, list):
                for ref in refs_raw:
                    if not isinstance(ref, Mapping):
                        continue
                    excerpt = str(ref.get("excerpt") or "").strip()
                    if not excerpt:
                        continue
                    direct_archive_id = str(ref.get("evidence_archive_id") or "").strip()
                    source_artifact_id = str(ref.get("source_artifact_id") or "").strip()
                    resolved_archive_id = ""
                    if direct_archive_id in archive_id_set:
                        resolved_archive_id = direct_archive_id
                    else:
                        lookup_key = source_artifact_id or direct_archive_id
                        if lookup_key:
                            resolved_archive_id = str(
                                source_artifact_to_archive.get(lookup_key) or ""
                            ).strip()
                    if not resolved_archive_id:
                        continue
                    normalized_refs.append(
                        {
                            "evidence_archive_id": resolved_archive_id,
                            "excerpt": excerpt,
                        }
                    )
            normalized_row["evidence_refs"] = normalized_refs
            normalized_rows.append(normalized_row)

    analyst_notes_raw = payload.get("analyst_notes")
    analyst_notes = (
        [dict(item) for item in analyst_notes_raw if isinstance(item, Mapping)]
        if isinstance(analyst_notes_raw, list)
        else []
    )
    return {
        "candidate_observations": normalized_rows,
        "analyst_notes": analyst_notes,
        "no_signal": bool(payload.get("no_signal")),
    }


__all__ = [
    "CandidateEvidenceCollector",
    "build_bounded_evidence_for_mapping",
    "normalize_post_tool_candidate_payload",
    "resolve_archive_source_artifact_id",
]
