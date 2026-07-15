"""Tests for canonical durable evidence reference normalization."""

from __future__ import annotations

import pytest

from backend.services.knowledge.evidence_refs import (
    normalize_canonical_evidence_refs,
    normalize_refs_for_archive_scope,
    resolve_refs_from_artifact_summaries,
)


def test_resolve_refs_from_artifacts_maps_unique_archive_rows() -> None:
    refs = resolve_refs_from_artifact_summaries(
        artifact_summaries=[
            {"artifact_id": "artifact-a", "artifact_kind": "stdout"},
            {"artifact_id": "artifact-missing", "artifact_kind": "stdout"},
        ],
        archives=[
            {
                "id": "archive-a",
                "source_artifact_id": "artifact-a",
                "lineage": {"artifact_id": "artifact-a"},
            }
        ],
    )

    assert refs == [{"evidence_archive_id": "archive-a"}]


def test_normalize_refs_for_archive_scope_drops_unmapped_and_ambiguous_refs() -> None:
    refs = normalize_refs_for_archive_scope(
        [
            {"evidence_archive_id": "archive-a", "artifact_id": "legacy-noise"},
            {"evidence_id": "archive-b", "excerpt": "proof"},
            {"artifact_id": "artifact-c"},
            {"source_artifact_id": "artifact-ambiguous"},
            {"artifact_id": "artifact-missing"},
        ],
        [
            {"id": "archive-a", "source_artifact_id": "artifact-a", "lineage": {}},
            {"id": "archive-b", "source_artifact_id": "artifact-b", "lineage": {}},
            {"id": "archive-c", "source_artifact_id": "artifact-c", "lineage": {}},
            {"id": "archive-d1", "source_artifact_id": "artifact-ambiguous", "lineage": {}},
            {"id": "archive-d2", "source_artifact_id": "artifact-ambiguous", "lineage": {}},
        ],
    )

    assert refs == [
        {"evidence_archive_id": "archive-a"},
        {"evidence_archive_id": "archive-b", "excerpt": "proof"},
        {"evidence_archive_id": "archive-c"},
    ]


def test_normalize_canonical_evidence_refs_rejects_noncanonical_persisted_refs() -> None:
    with pytest.raises(ValueError, match="evidence_archive_id"):
        normalize_canonical_evidence_refs([{"artifact_id": "artifact-a"}], strict=True)

    assert normalize_canonical_evidence_refs(
        [{"evidence_archive_id": "archive-a", "artifact_id": "ignored"}],
        strict=True,
    ) == [{"evidence_archive_id": "archive-a"}]
