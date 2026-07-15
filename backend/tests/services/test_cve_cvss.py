"""Tests for shared CVSS extraction and sync projection score preservation."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.services.cve_indexing.cvss import extract_cvss_score
from backend.services.cve_indexing.parser import CveParsedRecord
from backend.services.cve_indexing.sync_record_projection import new_cve_record


def test_extract_cvss_score_reads_top_level_canonical_value() -> None:
    score = extract_cvss_score({"cvss_score": 8.7})
    assert score == 8.7


def test_extract_cvss_score_reads_metrics_entries_value() -> None:
    score = extract_cvss_score({"entries": [{"cvssV3_1": {"baseScore": 9.4}}]})
    assert score == 9.4


def test_extract_cvss_score_prefers_highest_score_when_multiple_exist() -> None:
    score = extract_cvss_score(
        {"entries": [{"cvssV3_1": {"baseScore": 6.2}}, {"cvssV3_1": {"baseScore": 8.9}}]}
    )
    assert score == 8.9


def test_new_cve_record_keeps_cvss_score_available_for_rank_tie_break() -> None:
    parsed = CveParsedRecord(
        cve_id="CVE-2026-7777",
        record_state="published",
        title="score test",
        description="score test",
        published_at=datetime(2026, 3, 15, 10, tzinfo=UTC),
        source_updated_at=datetime(2026, 3, 15, 11, tzinfo=UTC),
        severity="high",
        cvss_version="3.1",
        cvss_score=9.8,
        raw_json={
            "containers": {
                "cna": {
                    "metrics": [{"cvssV3_1": {"baseScore": 9.8, "baseSeverity": "HIGH"}}],
                    "affected": [],
                }
            }
        },
        content_hash="x" * 64,
    )

    row = new_cve_record(parsed)

    assert isinstance(row.metrics, dict)
    assert row.metrics.get("cvss_score") == 9.8
