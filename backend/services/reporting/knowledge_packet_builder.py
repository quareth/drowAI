"""Build task-local durable knowledge packets for memo preparation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.knowledge import (
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeFinding,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)

KnowledgeRecordType = Literal[
    "asset",
    "service",
    "finding",
    "relationship",
    "web_path",
    "observation",
]

_DEFAULT_MAX_ITEMS = 120
_DEFAULT_MAX_SUMMARY_CHARACTERS = 700
_CANDIDATE_ASSERTION_LEVEL = "candidate"
_ENTITY_MODELS: dict[str, type[Any]] = {
    "asset": KnowledgeAsset,
    "service": KnowledgeService,
    "finding": KnowledgeFinding,
    "relationship": KnowledgeRelationship,
    "web_path": KnowledgeWebPath,
}


@dataclass(frozen=True)
class KnowledgePacketItem:
    """One bounded durable knowledge record available to memo preparation."""

    ref: str
    record_id: str
    tenant_id: int
    user_id: int
    engagement_id: int
    task_id: int
    record_type: KnowledgeRecordType
    summary: str
    confidence: str | None
    assertion_level: str | None
    first_observed_at: str | None
    last_observed_at: str | None
    source_execution_ids: tuple[str, ...]
    ingestion_run_ids: tuple[str, ...]
    evidence_archive_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    authoritative: bool
    authority: str


@dataclass(frozen=True)
class KnowledgePacket:
    """Bounded task-local knowledge packet for one tenant-owned task."""

    task_id: int
    items: tuple[KnowledgePacketItem, ...]
    canonical_item_count: int
    observation_item_count: int
    candidate_item_count: int
    truncated: bool
    max_items: int


class KnowledgePacketBuilder:
    """Read durable task-local knowledge rows into a memo-safe packet."""

    def __init__(
        self,
        db: Session,
        *,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_summary_characters: int = _DEFAULT_MAX_SUMMARY_CHARACTERS,
    ) -> None:
        if max_items <= 0:
            raise ValueError("max_items must be greater than zero")
        if max_summary_characters <= 0:
            raise ValueError("max_summary_characters must be greater than zero")
        self._db = db
        self._max_items = int(max_items)
        self._max_summary_characters = int(max_summary_characters)

    def build_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> KnowledgePacket:
        """Return bounded durable knowledge items for the requested task scope."""

        observations = self._select_observations(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        provenance_rows = self._select_provenance(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        raw_items = [
            *self._map_canonical_items(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                provenance_rows=provenance_rows,
            ),
            *self._map_observation_items(observations),
        ]
        ordered_items = sorted(
            raw_items,
            key=lambda item: (
                item.first_observed_at or "",
                item.record_type,
                item.ref,
            ),
        )
        bounded_items = ordered_items[: self._max_items]
        truncated = len(ordered_items) > len(bounded_items)
        return KnowledgePacket(
            task_id=int(task_id),
            items=tuple(bounded_items),
            canonical_item_count=sum(
                1 for item in bounded_items if item.record_type != "observation"
            ),
            observation_item_count=sum(
                1 for item in bounded_items if item.record_type == "observation"
            ),
            candidate_item_count=sum(
                1 for item in bounded_items if not item.authoritative
            ),
            truncated=truncated,
            max_items=self._max_items,
        )

    def _select_observations(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> list[KnowledgeObservation]:
        return (
            self._db.query(KnowledgeObservation)
            .select_from(KnowledgeObservation)
            .join(Task, Task.id == KnowledgeObservation.task_id)
            .filter(
                KnowledgeObservation.tenant_id == int(tenant_id),
                KnowledgeObservation.user_id == int(user_id),
                KnowledgeObservation.engagement_id == int(engagement_id),
                KnowledgeObservation.task_id == int(task_id),
                Task.id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
            )
            .order_by(
                KnowledgeObservation.observed_at.asc(), KnowledgeObservation.id.asc()
            )
            .all()
        )

    def _select_provenance(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> list[KnowledgeEntityProvenance]:
        return (
            self._db.query(KnowledgeEntityProvenance)
            .select_from(KnowledgeEntityProvenance)
            .join(Task, Task.id == KnowledgeEntityProvenance.task_id)
            .filter(
                KnowledgeEntityProvenance.tenant_id == int(tenant_id),
                KnowledgeEntityProvenance.user_id == int(user_id),
                KnowledgeEntityProvenance.task_id == int(task_id),
                or_(
                    KnowledgeEntityProvenance.engagement_id == int(engagement_id),
                    KnowledgeEntityProvenance.engagement_id.is_(None),
                ),
                Task.id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
            )
            .order_by(
                KnowledgeEntityProvenance.observed_at.asc(),
                KnowledgeEntityProvenance.id.asc(),
            )
            .all()
        )

    def _map_canonical_items(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        provenance_rows: Sequence[KnowledgeEntityProvenance],
    ) -> list[KnowledgePacketItem]:
        provenance_by_entity: dict[tuple[str, str], list[KnowledgeEntityProvenance]] = (
            defaultdict(list)
        )
        for row in provenance_rows:
            entity_type = _normalize_text(row.entity_type)
            if entity_type in _ENTITY_MODELS:
                provenance_by_entity[(entity_type, str(row.entity_id))].append(row)

        items: list[KnowledgePacketItem] = []
        for (entity_type, entity_id), rows in sorted(provenance_by_entity.items()):
            entity = self._load_canonical_entity(
                entity_type=entity_type,
                entity_id=entity_id,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
            )
            if entity is None:
                continue
            items.append(
                self._canonical_item(
                    entity_type=entity_type,
                    entity=entity,
                    provenance_rows=rows,
                )
            )
        return items

    def _load_canonical_entity(
        self,
        *,
        entity_type: str,
        entity_id: str,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> Any | None:
        model = _ENTITY_MODELS[entity_type]
        query = self._db.query(model).filter(
            model.id == entity_id,
            model.tenant_id == int(tenant_id),
            model.user_id == int(user_id),
        )
        if hasattr(model, "engagement_id"):
            query = query.filter(
                or_(
                    model.engagement_id == int(engagement_id),
                    model.engagement_id.is_(None),
                )
            )
        return query.first()

    def _canonical_item(
        self,
        *,
        entity_type: str,
        entity: Any,
        provenance_rows: Sequence[KnowledgeEntityProvenance],
    ) -> KnowledgePacketItem:
        first_observed = min((row.observed_at for row in provenance_rows), default=None)
        last_observed = max((row.observed_at for row in provenance_rows), default=None)
        is_candidate = entity_type == "finding" and _finding_is_candidate_only(entity)
        return KnowledgePacketItem(
            ref=f"knowledge_{entity_type}:{entity.id}",
            record_id=str(entity.id),
            tenant_id=int(entity.tenant_id),
            user_id=int(entity.user_id),
            engagement_id=_first_int(
                getattr(entity, "engagement_id", None),
                *(row.engagement_id for row in provenance_rows),
            ),
            task_id=_first_int(*(row.task_id for row in provenance_rows)),
            record_type=entity_type,  # type: ignore[arg-type]
            summary=self._canonical_summary(entity_type=entity_type, entity=entity),
            confidence=_canonical_confidence(entity_type=entity_type, entity=entity),
            assertion_level=_canonical_assertion_level(
                entity_type=entity_type,
                entity=entity,
            ),
            first_observed_at=_datetime_to_json(first_observed),
            last_observed_at=_datetime_to_json(last_observed),
            source_execution_ids=_unique_strings(
                row.execution_id for row in provenance_rows
            ),
            ingestion_run_ids=_unique_strings(
                row.ingestion_run_id for row in provenance_rows
            ),
            evidence_archive_refs=_unique_strings(
                [
                    *[row.evidence_archive_id for row in provenance_rows],
                    *_canonical_evidence_refs(entity_type=entity_type, entity=entity),
                ]
            ),
            provenance_refs=tuple(
                f"knowledge_entity_provenance:{row.id}" for row in provenance_rows
            ),
            authoritative=not is_candidate,
            authority="candidate_low_authority"
            if is_candidate
            else "task_local_canonical",
        )

    def _map_observation_items(
        self,
        observations: Sequence[KnowledgeObservation],
    ) -> list[KnowledgePacketItem]:
        items: list[KnowledgePacketItem] = []
        for observation in observations:
            is_candidate = _normalize_text(observation.assertion_level) == (
                _CANDIDATE_ASSERTION_LEVEL
            )
            items.append(
                KnowledgePacketItem(
                    ref=f"knowledge_observation:{observation.id}",
                    record_id=str(observation.id),
                    tenant_id=int(observation.tenant_id),
                    user_id=int(observation.user_id),
                    engagement_id=int(observation.engagement_id),
                    task_id=int(observation.task_id),
                    record_type="observation",
                    summary=self._observation_summary(observation),
                    confidence=_observation_confidence(observation),
                    assertion_level=_normalize_optional_text(
                        observation.assertion_level
                    ),
                    first_observed_at=_datetime_to_json(observation.observed_at),
                    last_observed_at=_datetime_to_json(observation.observed_at),
                    source_execution_ids=_unique_strings(
                        [observation.source_execution_id]
                    ),
                    ingestion_run_ids=_unique_strings([observation.ingestion_run_id]),
                    evidence_archive_refs=_unique_strings(
                        _extract_evidence_refs(observation.payload)
                        + _extract_evidence_refs(observation.observation_metadata)
                    ),
                    provenance_refs=(),
                    authoritative=not is_candidate,
                    authority="candidate_low_authority"
                    if is_candidate
                    else "task_local_observation",
                )
            )
        return items

    def _canonical_summary(self, *, entity_type: str, entity: Any) -> str:
        if entity_type == "asset":
            summary = {
                "asset_key": entity.asset_key,
                "asset_type": entity.asset_type,
                "display_name": entity.display_name,
                "ip_address": entity.ip_address,
                "hostname": entity.hostname,
                "status": entity.status,
            }
        elif entity_type == "service":
            summary = {
                "service_key": entity.service_key,
                "protocol": entity.protocol,
                "port": entity.port,
                "service_name": entity.service_name,
                "product": entity.product,
                "version": entity.version,
                "status": entity.status,
            }
        elif entity_type == "finding":
            summary = {
                "finding_key": entity.finding_key,
                "finding_type": entity.finding_type,
                "title": entity.title,
                "severity": entity.severity,
                "status": entity.status,
                "subject_type": entity.subject_type,
                "subject_key": entity.subject_key,
                "state": _compact_mapping(entity.finding_metadata, max_items=6),
            }
        elif entity_type == "relationship":
            summary = {
                "relationship_key": entity.relationship_key,
                "source_subject_key": entity.source_subject_key,
                "relationship_type": entity.relationship_type,
                "target_subject_key": entity.target_subject_key,
            }
        elif entity_type == "web_path":
            summary = {
                "canonical_url": entity.canonical_url,
                "origin_key": entity.origin_key,
                "path": entity.path,
                "last_status_code": entity.last_status_code,
                "last_response_size": entity.last_response_size,
                "producer_summary": _compact_mapping(
                    entity.producer_summary, max_items=6
                ),
            }
        else:
            summary = {"id": str(entity.id)}
        return _compact_text(_format_summary(summary), self._max_summary_characters)

    def _observation_summary(self, observation: KnowledgeObservation) -> str:
        summary = {
            "observation_type": observation.observation_type,
            "subject_type": observation.subject_type,
            "subject_key": observation.subject_key,
            "payload": _compact_mapping(observation.payload, max_items=8),
        }
        return _compact_text(_format_summary(summary), self._max_summary_characters)


def _finding_is_candidate_only(finding: KnowledgeFinding) -> bool:
    status = _normalize_text(finding.status)
    assertion_level = _normalize_text(finding.assertion_level)
    if (
        status == _CANDIDATE_ASSERTION_LEVEL
        or assertion_level == _CANDIDATE_ASSERTION_LEVEL
    ):
        return True

    metadata = (
        finding.finding_metadata
        if isinstance(finding.finding_metadata, Mapping)
        else {}
    )
    authority = metadata.get("authority")
    if isinstance(authority, Mapping):
        if bool(authority.get("candidate_only")):
            return True
        return _normalize_text(authority.get("source_kind")) == "llm_candidate"
    return False


def _canonical_confidence(*, entity_type: str, entity: Any) -> str | None:
    if entity_type == "asset":
        return _normalize_optional_text(getattr(entity, "max_confidence", None))
    return _normalize_optional_text(getattr(entity, "confidence", None))


def _canonical_assertion_level(*, entity_type: str, entity: Any) -> str | None:
    if entity_type == "finding":
        return _normalize_optional_text(entity.assertion_level)
    return None


def _observation_confidence(observation: KnowledgeObservation) -> str | None:
    payload = observation.payload if isinstance(observation.payload, Mapping) else {}
    metadata = (
        observation.observation_metadata
        if isinstance(observation.observation_metadata, Mapping)
        else {}
    )
    return _normalize_optional_text(
        payload.get("confidence") or metadata.get("confidence")
    )


def _canonical_evidence_refs(*, entity_type: str, entity: Any) -> list[Any]:
    refs: list[Any] = []
    if entity_type == "finding":
        refs.extend(_extract_evidence_refs(entity.evidence_summary))
        refs.extend(_extract_evidence_refs(entity.finding_metadata))
    elif entity_type == "web_path":
        refs.extend(_extract_evidence_refs(entity.evidence_refs))
    return refs


def _extract_evidence_refs(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        refs: list[Any] = []
        evidence_id = value.get("evidence_archive_id")
        if evidence_id is not None:
            refs.append(evidence_id)
        for key in ("evidence_refs", "archive_refs"):
            refs.extend(_extract_evidence_refs(value.get(key)))
        return refs
    if isinstance(value, list | tuple):
        refs = []
        for item in value:
            refs.extend(_extract_evidence_refs(item))
        return refs
    return []


def _compact_mapping(value: Any, *, max_items: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if len(compact) >= max_items:
            break
        if key in {"evidence_refs", "archive_refs"}:
            continue
        if isinstance(item, str | int | float | bool) or item is None:
            compact[str(key)] = item
        elif isinstance(item, Mapping):
            nested = _compact_mapping(item, max_items=3)
            if nested:
                compact[str(key)] = nested
        elif isinstance(item, list | tuple):
            compact[str(key)] = [
                nested_item
                for nested_item in item[:3]
                if isinstance(nested_item, str | int | float | bool)
            ]
    return compact


def _format_summary(summary: Mapping[str, Any]) -> str:
    parts = []
    for key, value in summary.items():
        if value is None or value == {} or value == []:
            continue
        parts.append(f"{key}={value}")
    return "; ".join(parts)


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return tuple(unique)


def _compact_text(value: Any, max_characters: int) -> str:
    if value is None or max_characters <= 0:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= max_characters:
        return text
    if max_characters <= 3:
        return text[:max_characters]
    return text[: max_characters - 3].rstrip() + "..."


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _datetime_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _first_int(*values: Any) -> int:
    for value in values:
        if value is not None:
            return int(value)
    return 0


__all__ = [
    "KnowledgePacket",
    "KnowledgePacketBuilder",
    "KnowledgePacketItem",
    "KnowledgeRecordType",
]
