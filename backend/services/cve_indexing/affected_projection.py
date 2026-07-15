"""Focused helpers for extracting matchable CVE affected-product entries.

Scope:
- Parses `containers.cna.affected` from canonical CVE JSON payloads.
- Preserves raw values for persistence while adding conservative normalized keys.

Boundary:
- Contains no database writes, sync orchestration, or ranking logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_NORMALIZED_NAME_MAX_LEN = 255


@dataclass(slots=True, frozen=True)
class CveAffectedEntry:
    """One extracted CVE affected-product projection candidate."""

    cve_id: str
    vendor_raw: str | None
    vendor_norm: str | None
    product_raw: str | None
    product_norm: str | None
    default_status: str | None
    versions_json: list[dict[str, Any]] | None
    cpes_json: list[str] | None


def extract_cve_affected_entries(*, cve_id: str, cve_json: dict[str, Any]) -> tuple[CveAffectedEntry, ...]:
    """Extract deterministic affected-product entries from one CVE JSON payload.

    Fail-closed behavior:
    - malformed containers/entries are skipped
    - entries without usable product/cpe identity are skipped
    """

    affected_items = _extract_affected_items(cve_json)
    if not affected_items:
        return ()

    rows: list[CveAffectedEntry] = []
    for item in affected_items:
        row = _to_projection_entry(cve_id=cve_id, item=item)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _to_projection_entry(*, cve_id: str, item: dict[str, Any]) -> CveAffectedEntry | None:
    vendor_raw = _clean_text(item.get("vendor"))
    product_raw = _clean_text(item.get("product"))
    default_status = _clean_text(item.get("defaultStatus"))

    cpes_json = _extract_cpes(item.get("cpes"))
    versions_json = _extract_versions(item.get("versions"))
    if product_raw is None and not cpes_json:
        return None

    return CveAffectedEntry(
        cve_id=cve_id,
        vendor_raw=vendor_raw,
        vendor_norm=_normalize_name(vendor_raw),
        product_raw=product_raw,
        product_norm=_normalize_name(product_raw),
        default_status=default_status,
        versions_json=versions_json,
        cpes_json=cpes_json,
    )


def _extract_affected_items(cve_json: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    containers = cve_json.get("containers")
    if not isinstance(containers, dict):
        return ()
    cna = containers.get("cna")
    if not isinstance(cna, dict):
        return ()
    affected = cna.get("affected")
    if not isinstance(affected, list):
        return ()
    return tuple(item for item in affected if isinstance(item, dict))


def _extract_versions(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None

    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append(dict(item))
    return rows or None


def _extract_cpes(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None

    cpes: list[str] = []
    for item in value:
        cleaned = _clean_text(item)
        if cleaned is not None:
            cpes.append(cleaned)
    return cpes or None


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    normalized = " ".join(lowered.split())
    if len(normalized) > _NORMALIZED_NAME_MAX_LEN:
        normalized = normalized[:_NORMALIZED_NAME_MAX_LEN]
    return normalized or None


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


__all__ = ["CveAffectedEntry", "extract_cve_affected_entries"]
