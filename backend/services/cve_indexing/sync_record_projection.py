"""CVE record + affected-product projection helpers for sync ingestion/upsert paths.

Scope:
- Maps parsed CVE payloads into `CveRecord` inserts/updates.
- Encapsulates deterministic payload hashing for no-op detection.
- Builds deterministic affected-product projection rows from canonical CVE JSON.

Boundary:
- Contains no network I/O, planning, scheduler logic, or DB session lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from backend.models.cve import CveAffectedProduct, CveRecord
from backend.services.cve_indexing.affected_projection import extract_cve_affected_entries
from backend.services.cve_indexing.contracts import CVE_SOURCE_KIND
from backend.services.cve_indexing.cvss import extract_cvss_score
from backend.services.cve_indexing.parser import CveParsedRecord
from backend.services.cve_indexing.primitives import utc_now
from backend.services.cve_indexing.projection_readiness import (
    PROJECTION_STATUS_ERROR,
    PROJECTION_STATUS_NON_PROJECTABLE,
    PROJECTION_STATUS_PROJECTED,
    ProjectionStatus,
)


@dataclass(slots=True, frozen=True)
class CveProjectionResult:
    """Computed projection rows and durable state for one CVE payload."""

    affected_rows: list[CveAffectedProduct]
    projection_status: ProjectionStatus
    affected_count: int
    projection_error_code: str | None


def new_cve_record(record: CveParsedRecord) -> CveRecord:
    """Create a new persisted CVE record from normalized parsed data."""
    created = CveRecord(
        cve_id=record.cve_id,
        source=CVE_SOURCE_KIND,
        record_state=record.record_state,
        published_at=record.published_at,
        source_updated_at=record.source_updated_at,
        title=record.title,
        description=record.description,
        severity=record.severity,
        metrics=_build_metrics(record),
        weaknesses=_extract_weaknesses(record.raw_json),
        references=_extract_references(record.raw_json),
        cve_json=record.raw_json,
    )
    apply_projection_state(
        target=created,
        cve_id=record.cve_id,
        cve_json=record.raw_json,
    )
    return created


def apply_record_update(target: CveRecord, source: CveParsedRecord) -> None:
    """Mutate existing CVE record with parsed source values."""
    target.record_state = source.record_state
    target.published_at = source.published_at
    target.source_updated_at = source.source_updated_at
    target.title = source.title
    target.description = source.description
    target.severity = source.severity
    target.metrics = _build_metrics(source)
    target.weaknesses = _extract_weaknesses(source.raw_json)
    target.references = _extract_references(source.raw_json)
    target.cve_json = source.raw_json
    apply_projection_state(
        target=target,
        cve_id=source.cve_id,
        cve_json=source.raw_json,
    )


def apply_projection_state(*, target: CveRecord, cve_id: str, cve_json: dict) -> CveProjectionResult:
    """Apply deterministic projection rows and durable status metadata to one record."""
    computed = compute_projection_state(cve_id=cve_id, cve_json=cve_json)
    target.affected_products = computed.affected_rows
    target.projection_status = computed.projection_status
    target.projection_affected_count = int(computed.affected_count)
    target.projection_error_code = computed.projection_error_code
    target.projection_last_projected_at = utc_now()
    return computed


def compute_projection_state(*, cve_id: str, cve_json: dict) -> CveProjectionResult:
    """Compute affected rows and durable projection status for one CVE JSON payload."""
    try:
        rows = build_affected_product_projection(cve_id=cve_id, cve_json=cve_json)
    except Exception:
        return CveProjectionResult(
            affected_rows=[],
            projection_status=PROJECTION_STATUS_ERROR,
            affected_count=0,
            projection_error_code="projection_exception",
        )

    if rows:
        return CveProjectionResult(
            affected_rows=rows,
            projection_status=PROJECTION_STATUS_PROJECTED,
            affected_count=len(rows),
            projection_error_code=None,
        )
    return CveProjectionResult(
        affected_rows=[],
        projection_status=PROJECTION_STATUS_NON_PROJECTABLE,
        affected_count=0,
        projection_error_code=None,
    )


def build_affected_product_projection(*, cve_id: str, cve_json: dict) -> list[CveAffectedProduct]:
    """Build deterministic affected-product rows from one CVE JSON payload."""
    extracted = extract_cve_affected_entries(cve_id=cve_id, cve_json=cve_json)
    seen_keys: set[str] = set()
    rows: list[CveAffectedProduct] = []

    for item in extracted:
        dedupe_key = json.dumps(
            {
                "vendor_raw": item.vendor_raw,
                "vendor_norm": item.vendor_norm,
                "product_raw": item.product_raw,
                "product_norm": item.product_norm,
                "default_status": item.default_status,
                "versions_json": item.versions_json,
                "cpes_json": item.cpes_json,
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        rows.append(
            CveAffectedProduct(
                cve_id=item.cve_id,
                vendor_raw=item.vendor_raw,
                vendor_norm=item.vendor_norm,
                product_raw=item.product_raw,
                product_norm=item.product_norm,
                default_status=item.default_status,
                versions_json=item.versions_json,
                cpes_json=item.cpes_json,
            )
        )

    return rows


def hash_record_payload(payload: object) -> str:
    """Return stable hash for normalized record payload comparison."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_metrics(record: CveParsedRecord) -> dict | None:
    container = _extract_cna_container(record.raw_json)
    metrics = container.get("metrics")
    score = float(record.cvss_score) if record.cvss_score is not None else None
    version = record.cvss_version

    if isinstance(metrics, list):
        payload: dict[str, object] = {"entries": metrics}
        extracted = extract_cvss_score(payload)
        if extracted is not None:
            payload["cvss_score"] = float(extracted)
            score = float(extracted)
        if version is not None:
            payload["cvss_version"] = version
        return payload

    if score is None and version is None:
        return None
    payload = {}
    if version is not None:
        payload["cvss_version"] = version
    if score is not None:
        payload["cvss_score"] = float(score)
    return payload or None


def _extract_weaknesses(payload: dict) -> list | None:
    problem_types = _extract_cna_container(payload).get("problemTypes")
    if isinstance(problem_types, list):
        return problem_types
    return None


def _extract_references(payload: dict) -> list | None:
    references = _extract_cna_container(payload).get("references")
    if isinstance(references, list):
        return references
    return None


def _extract_cna_container(payload: dict) -> dict:
    containers = payload.get("containers")
    if not isinstance(containers, dict):
        return {}
    cna = containers.get("cna")
    if not isinstance(cna, dict):
        return {}
    return cna


__all__ = [
    "apply_record_update",
    "apply_projection_state",
    "build_affected_product_projection",
    "compute_projection_state",
    "hash_record_payload",
    "new_cve_record",
]
