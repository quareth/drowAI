"""DNS-discovery deterministic compression helpers for normalized Amass output."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS

from .common import (
    as_int,
    compact_evidence_line,
    dedupe_string_list,
    sanitize_artifact_refs,
)
from .contracts import CompressionInput, DeterministicCompressionResult

AMASS_TOOL_ID = "information_gathering.dns.amass"
AMASS_CAPTURE_FORMAT = "amass_v5_subs_text"

DNS_NAME_SAMPLE_LIMIT = 8
DNS_MAPPING_SAMPLE_LIMIT = 5
DNS_UNRESOLVED_SAMPLE_LIMIT = 8
DNS_DIAGNOSTIC_LIMIT = 3
DNS_ARTIFACT_REF_LIMIT = 3
DNS_STRUCTURED_SIGNAL_SAMPLE_LIMIT = 25

_REGISTERED_DNS_DISCOVERY_TOOL_IDS: tuple[str, ...] = (AMASS_TOOL_ID,)
_NORMALIZED_COUNT_KEYS = (
    "names_count",
    "resolved_names_count",
    "unresolved_names_count",
    "ip_count",
)
_AMASS_PARSE_STATUSES = frozenset(("success", "partial", "failed", "empty"))


@dataclass(frozen=True, slots=True)
class _DnsNameRecord:
    """Canonical DNS name plus any normalized addresses returned for it."""

    name: str
    addresses: tuple[str, ...]
    discovery_role: Optional[str] = None


def dns_discovery_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project parsed Amass DNS metadata into compact name/address summaries."""

    if input_data.tool_name != AMASS_TOOL_ID:
        return DeterministicCompressionResult.none(
            fallback_reason="unsupported_dns_discovery_tool",
        )

    metadata = _amass_metadata(input_data.raw_result)
    if not metadata:
        return DeterministicCompressionResult.none(
            fallback_reason="no_dns_discovery_metadata",
        )

    records = _dns_name_records(metadata)
    diagnostics = sorted(
        dedupe_string_list(
            _iterable_or_empty(metadata.get("diagnostics")),
            limit=DNS_DIAGNOSTIC_LIMIT,
        )
    )
    names_count = len(records)
    resolved_names_count = sum(1 for record in records if record.addresses)
    unresolved_names_count = sum(1 for record in records if not record.addresses)
    unique_ip_count = len(
        _sort_ip_addresses(
            address
            for record in records
            for address in record.addresses
        )
    )

    mappings = [record for record in records if record.addresses]
    unresolved = [record for record in records if not record.addresses]
    artifact_refs = _artifact_refs(input_data.raw_result, metadata=metadata)

    findings = (
        _mapping_findings(mappings)
        + _unresolved_findings(unresolved)
        + _diagnostic_findings(diagnostics)
        + [f"artifact: {ref['path']}" for ref in artifact_refs]
    )
    findings = dedupe_string_list(findings, limit=None)
    if not findings and names_count == 0:
        findings = ["Amass metadata contained no DNS names."]

    return DeterministicCompressionResult(
        summary=_summary(
            _summary_text(
                parse_status=_text_or_none(metadata.get("parse_status")),
                enumeration_status=_text_or_none(metadata.get("enumeration_status")),
                result_completeness=_text_or_none(metadata.get("result_completeness")),
                partial_results=bool(metadata.get("partial_results") is True),
                names_count=names_count,
                resolved_names_count=resolved_names_count,
                unresolved_names_count=unresolved_names_count,
                unique_ip_count=unique_ip_count,
                seed_names_count=as_int(metadata.get("seed_names_count")),
                prior_names_count=as_int(metadata.get("prior_names_count")),
                newly_discovered_names_count=as_int(
                    metadata.get("newly_discovered_names_count")
                ),
            )
        ),
        key_findings=tuple(findings),
        structured_signals=tuple(
            _structured_signals(
                records,
                diagnostics=diagnostics,
                artifact_refs=artifact_refs,
                names_count=names_count,
                resolved_names_count=resolved_names_count,
                unresolved_names_count=unresolved_names_count,
                unique_ip_count=unique_ip_count,
                metadata=metadata,
            )
        ),
        decision_evidence=tuple(
            _decision_evidence(
                mappings,
                unresolved=unresolved,
                diagnostics=diagnostics,
            )
        ),
        completeness="partial",
        lossiness_risk="low",
    )


def registered_dns_discovery_tool_ids() -> tuple[str, ...]:
    """Return DNS-discovery tool ids registered for deterministic coverage."""

    return _REGISTERED_DNS_DISCOVERY_TOOL_IDS


def register_dns_discovery_adapters() -> None:
    """Register visible DNS-discovery adapters."""

    from .registry import register_adapter

    register_adapter(AMASS_TOOL_ID, dns_discovery_adapter)


def _amass_metadata(raw_result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return normalized Amass metadata from a runtime tool result."""

    metadata = raw_result.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}

    nested_amass = metadata.get("amass")
    if isinstance(nested_amass, Mapping) and _looks_like_amass_metadata(nested_amass):
        return nested_amass

    if _looks_like_amass_metadata(metadata):
        return metadata
    return {}


def _looks_like_amass_metadata(metadata: Mapping[str, Any]) -> bool:
    """Return whether metadata carries normalized Amass DNS discovery fields."""

    if _text_or_none(metadata.get("capture_format")) != AMASS_CAPTURE_FORMAT:
        return False

    if _text_or_none(metadata.get("parse_status")) not in _AMASS_PARSE_STATUSES:
        return False

    subdomains = _normalized_dns_record_map(
        metadata.get("subdomains"),
        name_key="subdomain",
        require_amass_row_contract=True,
    )
    if subdomains is None:
        return False

    hosts = _normalized_dns_record_map(metadata.get("hosts"), name_key="hostname")
    if hosts != subdomains:
        return False

    derived_ips = tuple(
        _sort_ip_addresses(
            address
            for addresses in subdomains.values()
            for address in addresses
        )
    )
    metadata_ips = _canonical_ip_tuple(metadata.get("ips"))
    if metadata_ips != derived_ips:
        return False

    return _has_derived_count_fields(
        metadata,
        records=subdomains,
        unique_ips=derived_ips,
    )


def _has_derived_count_fields(
    metadata: Mapping[str, Any],
    *,
    records: Mapping[str, tuple[str, ...]],
    unique_ips: tuple[str, ...],
) -> bool:
    """Return whether flat count fields match the normalized rows exactly."""

    expected_counts = {
        "names_count": len(records),
        "resolved_names_count": sum(1 for addresses in records.values() if addresses),
        "unresolved_names_count": sum(
            1 for addresses in records.values() if not addresses
        ),
        "ip_count": len(unique_ips),
    }
    return all(
        as_int(metadata.get(key)) == expected_counts[key]
        for key in _NORMALIZED_COUNT_KEYS
    )


def _normalized_dns_record_map(
    value: Any,
    *,
    name_key: str,
    require_amass_row_contract: bool = False,
) -> dict[str, tuple[str, ...]] | None:
    """Return one normalized row per DNS name, or ``None`` for contract drift."""

    if not isinstance(value, (list, tuple)):
        return None

    records: dict[str, tuple[str, ...]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        name = _normalized_dns_name(item.get(name_key))
        addresses = _canonical_ip_tuple(item.get("ip"))
        if name is None or addresses is None or name in records:
            return None
        if require_amass_row_contract and (
            _text_or_none(item.get("source")) != "amass"
            or _record_type_tuple(item.get("record_types"))
            != _record_types_for_addresses(addresses)
        ):
            return None
        records[name] = addresses
    return records


def _dns_name_records(metadata: Mapping[str, Any]) -> list[_DnsNameRecord]:
    """Return canonical name/address rows from normalized Amass metadata."""

    records = _normalized_dns_record_map(
        metadata.get("subdomains"),
        name_key="subdomain",
    )
    if records is None:
        return []

    return [
        _DnsNameRecord(
            name=name,
            addresses=addresses,
            discovery_role=_discovery_role_by_name(metadata).get(name),
        )
        for name, addresses in sorted(records.items())
    ]


def _discovery_role_by_name(metadata: Mapping[str, Any]) -> dict[str, str]:
    """Return valid discovery roles keyed by normalized DNS name."""

    roles: dict[str, str] = {}
    for item in _iterable_or_empty(metadata.get("subdomains")):
        if not isinstance(item, Mapping):
            continue
        name = _normalized_dns_name(item.get("subdomain"))
        role = _text_or_none(item.get("discovery_role"))
        if name is None or role not in {
            "scope_seed",
            "prior_known",
            "newly_discovered",
        }:
            continue
        roles[name] = role
    return roles


def _record_type_tuple(value: Any) -> tuple[str, ...] | None:
    """Return normalized DNS record types, rejecting non-deterministic rows."""

    if not isinstance(value, (list, tuple)):
        return None

    record_types: list[str] = []
    seen: set[str] = set()
    for item in value:
        record_type = _text_or_none(item)
        if record_type not in {"A", "AAAA"} or record_type in seen:
            return None
        seen.add(record_type)
        record_types.append(record_type)
    return tuple(record_types)


def _record_types_for_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    """Return deterministic record types implied by canonical IP addresses."""

    return tuple(
        record_type
        for version, record_type in ((4, "A"), (6, "AAAA"))
        if any(
            ipaddress.ip_address(address).version == version
            for address in addresses
        )
    )


def _summary_text(
    *,
    parse_status: Optional[str],
    enumeration_status: Optional[str],
    result_completeness: Optional[str],
    partial_results: bool,
    names_count: int,
    resolved_names_count: int,
    unresolved_names_count: int,
    unique_ip_count: int,
    seed_names_count: int,
    prior_names_count: int,
    newly_discovered_names_count: int,
) -> str:
    """Build a truthful status-aware Amass DNS summary."""

    if enumeration_status or result_completeness or partial_results:
        execution = str(enumeration_status or "unknown").strip().lower()
        completeness = str(result_completeness or "unknown").strip().lower()
        status_text = f"Amass enumeration {execution.replace('_', ' ')}"
        if completeness == "partial" or partial_results:
            status_text += " with partial results"
        counts = (
            f"{names_count} DNS names: {seed_names_count} seed, "
            f"{newly_discovered_names_count} newly discovered, "
            f"{prior_names_count} prior, {resolved_names_count} resolved, "
            f"{unresolved_names_count} unresolved, {unique_ip_count} unique IPs."
        )
        parse = str(parse_status or "unknown").strip().lower()
        return f"{status_text}; parser status {parse}; {counts}"

    counts = (
        f"{names_count} DNS names: {resolved_names_count} resolved, "
        f"{unresolved_names_count} unresolved, {unique_ip_count} unique IPs."
    )
    status = str(parse_status or "").strip().lower()
    if status == "empty":
        return (
            "Amass returned no DNS names: "
            f"{resolved_names_count} resolved, {unresolved_names_count} unresolved, "
            f"{unique_ip_count} unique IPs."
        )
    if status == "partial":
        return f"Amass partially parsed DNS output; discovered {counts}"
    if status == "failed":
        return f"Amass parsing failed; discovered {counts}"
    return f"Amass discovered {counts}"


def _mapping_findings(records: list[_DnsNameRecord]) -> list[str]:
    """Return bounded name/address mapping findings."""

    return [
        compact_evidence_line(
            f"{record.name} resolves to {', '.join(record.addresses)}"
        )
        for record in records[:DNS_MAPPING_SAMPLE_LIMIT]
    ]


def _unresolved_findings(records: list[_DnsNameRecord]) -> list[str]:
    """Return bounded unresolved DNS name findings."""

    return [
        compact_evidence_line(f"{record.name}: no address returned")
        for record in records[:DNS_UNRESOLVED_SAMPLE_LIMIT]
    ]


def _diagnostic_findings(diagnostics: Iterable[str]) -> list[str]:
    """Return bounded Amass parse diagnostic findings."""

    return [
        compact_evidence_line(f"diagnostic: {diagnostic}")
        for diagnostic in diagnostics
    ]


def _decision_evidence(
    mappings: list[_DnsNameRecord],
    *,
    unresolved: list[_DnsNameRecord],
    diagnostics: list[str],
) -> list[str]:
    """Return bounded evidence lines from normalized Amass metadata."""

    evidence: list[str] = []
    for record in mappings[:DNS_MAPPING_SAMPLE_LIMIT]:
        evidence.append(
            compact_evidence_line(
                f"dns mapping: {record.name} -> {', '.join(record.addresses)}"
            )
        )
    for record in unresolved[:DNS_UNRESOLVED_SAMPLE_LIMIT]:
        evidence.append(compact_evidence_line(f"unresolved dns name: {record.name}"))
    for diagnostic in diagnostics[:DNS_DIAGNOSTIC_LIMIT]:
        evidence.append(compact_evidence_line(f"amass diagnostic: {diagnostic}"))
    return dedupe_string_list(evidence, limit=None)


def _structured_signals(
    records: list[_DnsNameRecord],
    *,
    diagnostics: list[str],
    artifact_refs: list[dict[str, str]],
    names_count: int,
    resolved_names_count: int,
    unresolved_names_count: int,
    unique_ip_count: int,
    metadata: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return bounded key-value structured signals for normalized Amass facts."""

    signals: list[Mapping[str, Any]] = [
        {"type": "kv_pair", "key": "amass_dns_names_count", "value": names_count},
        {
            "type": "kv_pair",
            "key": "amass_resolved_names_count",
            "value": resolved_names_count,
        },
        {
            "type": "kv_pair",
            "key": "amass_unresolved_names_count",
            "value": unresolved_names_count,
        },
        {
            "type": "kv_pair",
            "key": "amass_unique_ip_count",
            "value": unique_ip_count,
        },
    ]
    for key, value in _execution_structured_signals(metadata):
        signals.append({"type": "kv_pair", "key": key, "value": value})
    sample_signals: list[Mapping[str, Any]] = []

    for record in records[:DNS_NAME_SAMPLE_LIMIT]:
        sample_signals.append(
            {
                "type": "kv_pair",
                "key": "amass_dns_name_sample",
                "value": _compact_signal_text(record.name),
            }
        )
        if record.discovery_role == "scope_seed":
            sample_signals.append(
                {
                    "type": "kv_pair",
                    "key": "amass_scope_seed",
                    "value": _compact_signal_text(record.name),
                }
            )
    mappings = [record for record in records if record.addresses]
    unresolved = [record for record in records if not record.addresses]

    for record in mappings[:DNS_MAPPING_SAMPLE_LIMIT]:
        sample_signals.append(
            {
                "type": "kv_pair",
                "key": f"amass_mapping:{record.name}",
                "value": _compact_signal_text(", ".join(record.addresses)),
            }
        )
    for record in unresolved[:DNS_UNRESOLVED_SAMPLE_LIMIT]:
        sample_signals.append(
            {
                "type": "kv_pair",
                "key": "amass_unresolved_name",
                "value": _compact_signal_text(record.name),
            }
        )
    for diagnostic in diagnostics[:DNS_DIAGNOSTIC_LIMIT]:
        sample_signals.append(
            {
                "type": "kv_pair",
                "key": "amass_diagnostic",
                "value": _compact_signal_text(diagnostic),
            }
        )
    for ref in artifact_refs:
        sample_signals.append(
            {
                "type": "kv_pair",
                "key": "amass_artifact_ref",
                "value": _compact_signal_text(ref["path"]),
            }
        )

    signals.extend(sample_signals[:DNS_STRUCTURED_SIGNAL_SAMPLE_LIMIT])
    return signals


def _execution_structured_signals(metadata: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Return normalized execution/role count signals when present."""

    fields = (
        ("enumeration_status", "amass_enumeration_status"),
        ("result_completeness", "amass_result_completeness"),
        ("partial_results", "amass_partial_results"),
        ("seed_names_count", "amass_seed_names_count"),
        ("prior_names_count", "amass_prior_names_count"),
        ("newly_discovered_names_count", "amass_newly_discovered_names_count"),
        ("discovered_names_count", "amass_discovered_names_count"),
    )
    signals: list[tuple[str, Any]] = []
    for metadata_key, signal_key in fields:
        if metadata_key not in metadata:
            continue
        value = metadata.get(metadata_key)
        if isinstance(value, str):
            value = _compact_signal_text(value)
        signals.append((signal_key, value))
    return signals


def _artifact_refs(
    raw_result: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Return sanitized artifact refs carried by raw result or metadata."""

    candidates: list[Mapping[str, Any]] = []
    for artifact in _iterable_or_empty(raw_result.get("artifacts")):
        if isinstance(artifact, Mapping):
            candidates.append(artifact)
        elif isinstance(artifact, str):
            candidates.append({"path": artifact})

    for artifact in _iterable_or_empty(metadata.get("artifacts")):
        if isinstance(artifact, Mapping):
            candidates.append(artifact)
        elif isinstance(artifact, str):
            candidates.append({"path": artifact})

    return sanitize_artifact_refs(candidates)[:DNS_ARTIFACT_REF_LIMIT]


def _sort_ip_addresses(values: Iterable[Any]) -> list[str]:
    """Return unique IP strings ordered by address family and numeric value."""

    addresses = []
    seen: set[str] = set()
    for value in values:
        text = _text_or_none(value)
        if not text:
            continue
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        normalized = str(address)
        if normalized in seen:
            continue
        seen.add(normalized)
        addresses.append(address)
    return [
        str(address)
        for address in sorted(addresses, key=lambda item: (item.version, int(item)))
    ]


def _canonical_ip_tuple(value: Any) -> tuple[str, ...] | None:
    """Return canonical sorted IP strings, rejecting invalid or drifted values."""

    if not isinstance(value, (list, tuple)):
        return None

    addresses = []
    seen: set[str] = set()
    for item in value:
        text = _text_or_none(item)
        if not text:
            return None
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return None
        normalized = str(address)
        if normalized != text or normalized in seen:
            return None
        seen.add(normalized)
        addresses.append(address)

    return tuple(
        str(address)
        for address in sorted(addresses, key=lambda item: (item.version, int(item)))
    )


def _iterable_or_empty(value: Any) -> Iterable[Any]:
    """Return iterable list/tuple/set values without treating text as iterable."""

    if isinstance(value, (list, tuple, set)):
        return value
    return ()


def _normalized_dns_name(value: Any) -> Optional[str]:
    """Return a DNS name only when it is already normalized by the parser."""

    text = _text_or_none(value)
    if not text or text.endswith(".") or text != text.lower() or len(text) > 253:
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        return None
    try:
        encoded = text.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if encoded != text:
        return None

    labels = text.split(".")
    if len(labels) < 2:
        return None
    for label in labels:
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                not (
                    character.isascii()
                    and (character.isalnum() or character == "-")
                )
                for character in label
            )
        ):
            return None
    return text


def _text_or_none(value: Any) -> Optional[str]:
    """Return stripped text or None."""

    text = str(value or "").strip()
    return text or None


def _summary(value: str) -> str:
    """Bound summaries to the existing compact summary size."""

    return value[:COMPACT_SUMMARY_MAX_CHARS]


def _compact_signal_text(value: str) -> str:
    """Bound structured-signal text through the shared compact helper."""

    return compact_evidence_line(value)


register_dns_discovery_adapters()
