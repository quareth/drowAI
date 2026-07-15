"""Unit tests for CVE ZIP parsing and deterministic record extraction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZipFile

import pytest

from backend.services.cve_indexing.parser import CveZipParser, CveZipParserError


def _build_zip(entries: dict[str, dict | str]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            if isinstance(payload, str):
                archive.writestr(name, payload)
            elif isinstance(payload, bytes):
                archive.writestr(name, payload)
            else:
                archive.writestr(name, json.dumps(payload))
    return buffer.getvalue()


def _record(
    *,
    cve_id: str = "CVE-2026-1000",
    description: str = "Buffer overflow in parser component",
    score: float = 8.1,
    severity: str = "HIGH",
    published: str = "2026-03-15T10:00:00Z",
    updated: str = "2026-03-15T11:00:00Z",
) -> dict:
    return {
        "cveMetadata": {
            "cveId": cve_id,
            "state": "PUBLISHED",
            "datePublished": published,
            "dateUpdated": updated,
        },
        "containers": {
            "cna": {
                "title": "Heap corruption vulnerability",
                "descriptions": [{"lang": "en", "value": description}],
                "metrics": [
                    {
                        "cvssV3_1": {
                            "baseSeverity": severity,
                            "baseScore": score,
                        }
                    }
                ],
            }
        },
    }


def test_parser_processes_baseline_and_delta_zip_contents() -> None:
    parser = CveZipParser()
    zip_payload = _build_zip(
        {
            "baseline/CVE-2026-1000.json": _record(cve_id="CVE-2026-1000"),
            "delta/CVE-2026-1001.json": _record(cve_id="CVE-2026-1001"),
            "delta/readme.txt": "not a json member",
        }
    )

    parsed = list(parser.iter_records(zip_payload))

    assert [item.cve_id for item in parsed] == ["CVE-2026-1000", "CVE-2026-1001"]
    assert parsed[0].published_at == datetime(2026, 3, 15, 10, tzinfo=UTC)
    assert parsed[0].source_updated_at == datetime(2026, 3, 15, 11, tzinfo=UTC)


def test_parser_processes_nested_zip_members() -> None:
    parser = CveZipParser()
    nested_zip = _build_zip({"records/CVE-2026-4242.json": _record(cve_id="CVE-2026-4242")})
    outer_zip = _build_zip({"cves.zip": nested_zip})

    parsed = list(parser.iter_records(outer_zip))

    assert len(parsed) == 1
    assert parsed[0].cve_id == "CVE-2026-4242"


def test_parser_skips_malformed_entries_below_safety_threshold() -> None:
    parser = CveZipParser(max_malformed_records=3, min_records_for_ratio_check=99)
    zip_payload = _build_zip(
        {
            "records/CVE-2026-1000.json": _record(cve_id="CVE-2026-1000"),
            "records/bad.json": "{not-json",
        }
    )

    parsed = list(parser.iter_records(zip_payload))

    assert len(parsed) == 1
    assert parsed[0].cve_id == "CVE-2026-1000"


def test_parser_aborts_when_malformed_threshold_crosses_limit() -> None:
    parser = CveZipParser(max_malformed_records=1, min_records_for_ratio_check=1, max_malformed_ratio=0.5)
    zip_payload = _build_zip(
        {
            "records/bad-1.json": "{not-json",
            "records/bad-2.json": "{also-not-json",
        }
    )

    with pytest.raises(CveZipParserError):
        list(parser.iter_records(zip_payload))


def test_record_extraction_is_deterministic_for_content_hash() -> None:
    parser = CveZipParser()
    first = _record(cve_id="CVE-2026-9000")
    second = {
        "containers": first["containers"],
        "cveMetadata": first["cveMetadata"],
    }

    first_zip = _build_zip({"first.json": first})
    second_zip = _build_zip({"second.json": second})
    first_record = list(parser.iter_records(first_zip))[0]
    second_record = list(parser.iter_records(second_zip))[0]

    assert first_record.content_hash == second_record.content_hash
    assert first_record.description == "Buffer overflow in parser component"
    assert first_record.severity == "HIGH"
    assert first_record.cvss_version == "3.1"
    assert first_record.cvss_score == 8.1
