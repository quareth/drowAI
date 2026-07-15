"""Canonical durable evidence reference normalization for knowledge persistence.

This module owns conversion from transient task-artifact evidence references to
durable knowledge evidence archive references. Persisted knowledge rows may
identify evidence only with `evidence_archive_id` plus an optional excerpt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


EVIDENCE_ARCHIVE_ID_KEY = "evidence_archive_id"
_LEGACY_EVIDENCE_ID_KEY = "evidence_id"
_ARTIFACT_ID_KEYS = ("artifact_id", "source_artifact_id")


@dataclass(frozen=True)
class EvidenceArchiveRefIndex:
    """Lookup table for durable archive ids and transient artifact aliases."""

    archive_ids: frozenset[str]
    artifact_to_archive_id: Mapping[str, str]
    ambiguous_artifact_ids: frozenset[str]

    def resolve_archive_id(self, value: Any) -> str | None:
        """Return a durable archive id when `value` already identifies an archive."""
        archive_id = _clean_text(value)
        if archive_id and archive_id in self.archive_ids:
            return archive_id
        return None

    def resolve_artifact_id(self, value: Any) -> str | None:
        """Return the unique archive id mapped from one transient artifact id."""
        artifact_id = _clean_text(value)
        if not artifact_id or artifact_id in self.ambiguous_artifact_ids:
            return None
        return self.artifact_to_archive_id.get(artifact_id)


def build_archive_ref_index(archives: Sequence[Any] | None) -> EvidenceArchiveRefIndex:
    """Build archive and artifact lookup maps from archived evidence rows."""
    archive_ids: set[str] = set()
    artifact_candidates: dict[str, set[str]] = {}
    for archive in archives or ():
        archive_id = _clean_text(_archive_value(archive, "id"))
        if not archive_id:
            continue
        archive_ids.add(archive_id)
        for artifact_id in _archive_artifact_ids(archive):
            artifact_candidates.setdefault(artifact_id, set()).add(archive_id)

    artifact_to_archive_id: dict[str, str] = {}
    ambiguous_artifact_ids: set[str] = set()
    for artifact_id, mapped_archive_ids in artifact_candidates.items():
        if len(mapped_archive_ids) == 1:
            artifact_to_archive_id[artifact_id] = next(iter(mapped_archive_ids))
        else:
            ambiguous_artifact_ids.add(artifact_id)

    return EvidenceArchiveRefIndex(
        archive_ids=frozenset(archive_ids),
        artifact_to_archive_id=artifact_to_archive_id,
        ambiguous_artifact_ids=frozenset(ambiguous_artifact_ids),
    )


def resolve_refs_from_artifact_summaries(
    artifact_summaries: Sequence[Mapping[str, Any]] | None,
    archives: Sequence[Any] | None,
) -> list[dict[str, str]]:
    """Return canonical refs for artifact summaries that have unique archive rows."""
    index = build_archive_ref_index(archives)
    refs: list[dict[str, str]] = []
    for artifact in artifact_summaries or ():
        if not isinstance(artifact, Mapping):
            continue
        archive_id = index.resolve_artifact_id(artifact.get("artifact_id"))
        if archive_id:
            refs.append({EVIDENCE_ARCHIVE_ID_KEY: archive_id})
    return normalize_canonical_evidence_refs(refs)


def normalize_refs_for_archive_scope(
    value: Any,
    archives: Sequence[Any] | None,
) -> list[dict[str, str]]:
    """Map legacy/transient refs to canonical refs within one archive scope."""
    index = build_archive_ref_index(archives)
    if not _is_ref_sequence(value):
        return []

    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        archive_id = (
            index.resolve_archive_id(item.get(EVIDENCE_ARCHIVE_ID_KEY))
            or index.resolve_archive_id(item.get(_LEGACY_EVIDENCE_ID_KEY))
            or _resolve_artifact_ref(item, index)
        )
        if not archive_id:
            continue
        ref = {EVIDENCE_ARCHIVE_ID_KEY: archive_id}
        excerpt = _clean_text(item.get("excerpt"))
        if excerpt:
            ref["excerpt"] = excerpt
        refs.append(ref)
    return normalize_canonical_evidence_refs(refs)


def normalize_canonical_evidence_refs(value: Any, *, strict: bool = False) -> list[dict[str, str]]:
    """Return persisted canonical refs or raise when strict input is invalid."""
    if value is None:
        return []
    if not _is_ref_sequence(value):
        if strict:
            raise ValueError("payload.evidence_refs must be a list")
        return []

    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str | None]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            if strict:
                raise ValueError("payload.evidence_refs entries must be mappings")
            continue
        archive_id = _clean_text(item.get(EVIDENCE_ARCHIVE_ID_KEY))
        if not archive_id:
            if strict:
                raise ValueError(
                    "persisted evidence_refs entries require non-empty evidence_archive_id"
                )
            continue
        ref = {EVIDENCE_ARCHIVE_ID_KEY: archive_id}
        excerpt = _clean_text(item.get("excerpt"))
        marker = (archive_id, excerpt)
        if marker in seen:
            continue
        seen.add(marker)
        if excerpt:
            ref["excerpt"] = excerpt
        refs.append(ref)
    return refs


def _resolve_artifact_ref(item: Mapping[str, Any], index: EvidenceArchiveRefIndex) -> str | None:
    for key in _ARTIFACT_ID_KEYS:
        archive_id = index.resolve_artifact_id(item.get(key))
        if archive_id:
            return archive_id
    return None


def _archive_artifact_ids(archive: Any) -> set[str]:
    artifact_ids: set[str] = set()
    source_artifact_id = _clean_text(_archive_value(archive, "source_artifact_id"))
    if source_artifact_id:
        artifact_ids.add(source_artifact_id)

    lineage = _archive_value(archive, "lineage_snapshot")
    if not isinstance(lineage, Mapping):
        lineage = _archive_value(archive, "lineage")
    if isinstance(lineage, Mapping):
        lineage_artifact_id = _clean_text(lineage.get("artifact_id"))
        if lineage_artifact_id:
            artifact_ids.add(lineage_artifact_id)
    return artifact_ids


def _archive_value(archive: Any, key: str) -> Any:
    if isinstance(archive, Mapping):
        return archive.get(key)
    return getattr(archive, key, None)


def _is_ref_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
