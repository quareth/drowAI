"""Streaming parser for CVE release ZIP assets and normalized record extraction.

Scope:
- Iterates ZIP members one-by-one to avoid loading full baseline datasets at once.
- Extracts a deterministic minimal read model from CVE JSON records.
- Preserves raw JSON payload and computes stable content hashes for no-op detection.

Boundary:
- Does not fetch source assets or write any database rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Iterator
from zipfile import ZipFile


class CveZipParserError(RuntimeError):
    """Raised when ZIP parsing cannot continue safely."""


@dataclass(slots=True, frozen=True)
class CveParsedRecord:
    """Normalized CVE payload extracted from one JSON entry in a release ZIP."""

    cve_id: str
    record_state: str
    title: str | None
    description: str | None
    published_at: datetime | None
    source_updated_at: datetime | None
    severity: str | None
    cvss_version: str | None
    cvss_score: float | None
    raw_json: dict
    content_hash: str


class CveZipParser:
    """Parser for baseline/delta CVE ZIP payloads with malformed-record safety limits."""

    def __init__(
        self,
        *,
        max_malformed_records: int = 500,
        max_malformed_ratio: float = 0.2,
        min_records_for_ratio_check: int = 25,
        max_nested_zip_depth: int = 2,
    ) -> None:
        self._max_malformed_records = max_malformed_records
        self._max_malformed_ratio = max_malformed_ratio
        self._min_records_for_ratio_check = min_records_for_ratio_check
        self._max_nested_zip_depth = max(0, int(max_nested_zip_depth))

    def iter_records(self, zip_payload: bytes) -> Iterator[CveParsedRecord]:
        """Parse one ZIP payload and return extracted records.

        Parsing is performed entry-by-entry so only one JSON document is held in memory at
        a time. Malformed entries are skipped until a configured safety threshold is crossed.
        """

        malformed_count = 0
        seen_count = 0

        for raw_bytes in self._iter_json_member_bytes(zip_payload=zip_payload, depth=0):
            seen_count += 1
            try:
                raw_payload = json.loads(raw_bytes)
                if not isinstance(raw_payload, dict):
                    raise ValueError("CVE payload must be a JSON object.")
                yield _extract_record(raw_payload)
            except Exception:
                malformed_count += 1
                self._raise_if_threshold_crossed(
                    seen_count=seen_count,
                    malformed_count=malformed_count,
                )

    def _iter_json_member_bytes(self, *, zip_payload: bytes, depth: int) -> Iterator[bytes]:
        try:
            with ZipFile(BytesIO(zip_payload)) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue

                    with archive.open(member) as stream:
                        member_bytes = stream.read()

                    lowered_name = member.filename.lower()
                    if lowered_name.endswith(".json"):
                        yield member_bytes
                        continue

                    if lowered_name.endswith(".zip") and depth < self._max_nested_zip_depth:
                        yield from self._iter_json_member_bytes(
                            zip_payload=member_bytes,
                            depth=depth + 1,
                        )
        except Exception as exc:
            raise CveZipParserError(f"Failed to parse ZIP payload at depth={depth}.") from exc

    def _raise_if_threshold_crossed(self, *, seen_count: int, malformed_count: int) -> None:
        ratio_crossed = (
            seen_count >= self._min_records_for_ratio_check
            and (malformed_count / max(seen_count, 1)) > self._max_malformed_ratio
        )
        absolute_crossed = malformed_count > self._max_malformed_records

        if ratio_crossed or absolute_crossed:
            raise CveZipParserError(
                "CVE ZIP parsing aborted: malformed-record safety threshold crossed "
                f"(seen={seen_count}, malformed={malformed_count})."
            )


def _extract_record(raw_payload: dict) -> CveParsedRecord:
    cve_metadata = _dict(raw_payload.get("cveMetadata"))
    cve_id = str(cve_metadata.get("cveId", "")).strip()
    if not cve_id:
        raise ValueError("Missing cveMetadata.cveId")

    record_state = str(cve_metadata.get("state", "published")).strip().lower() or "published"

    containers = _dict(raw_payload.get("containers"))
    cna = _dict(containers.get("cna"))
    descriptions = cna.get("descriptions")
    metrics = cna.get("metrics")

    title = _normalize_text(cna.get("title"))
    description = _pick_description(descriptions)
    severity, cvss_version, cvss_score = _extract_cvss(metrics)

    published_at = _parse_timestamp(cve_metadata.get("datePublished"))
    source_updated_at = _parse_timestamp(cve_metadata.get("dateUpdated"))

    canonical_json = _canonicalize_payload(raw_payload)
    content_hash = hashlib.sha256(canonical_json).hexdigest()

    return CveParsedRecord(
        cve_id=cve_id,
        record_state=record_state,
        title=title,
        description=description,
        published_at=published_at,
        source_updated_at=source_updated_at,
        severity=severity,
        cvss_version=cvss_version,
        cvss_score=cvss_score,
        raw_json=raw_payload,
        content_hash=content_hash,
    )


def _canonicalize_payload(raw_payload: dict) -> bytes:
    return json.dumps(
        raw_payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _extract_cvss(metrics: object) -> tuple[str | None, str | None, float | None]:
    if not isinstance(metrics, list):
        return None, None, None

    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        for field_name, version in (("cvssV4_0", "4.0"), ("cvssV3_1", "3.1"), ("cvssV3_0", "3.0")):
            cvss = metric.get(field_name)
            if isinstance(cvss, dict):
                score = _to_float(cvss.get("baseScore"))
                severity = _normalize_text(cvss.get("baseSeverity"))
                return severity, version, score
    return None, None, None


def _pick_description(descriptions: object) -> str | None:
    if not isinstance(descriptions, list):
        return None

    english_fallback: str | None = None
    first_text: str | None = None
    for item in descriptions:
        if not isinstance(item, dict):
            continue
        value = _normalize_text(item.get("value"))
        if not value:
            continue
        if first_text is None:
            first_text = value
        lang = str(item.get("lang", "")).strip().lower()
        if lang == "en":
            english_fallback = value
            break
    return english_fallback or first_text


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    timestamp = value.strip()
    if not timestamp:
        return None
    timestamp = timestamp.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    return {}


__all__ = [
    "CveParsedRecord",
    "CveZipParser",
    "CveZipParserError",
]
