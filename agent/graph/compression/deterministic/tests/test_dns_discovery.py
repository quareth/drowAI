"""Unit tests for DNS-discovery deterministic compression helpers."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import asdict
from typing import Any, Mapping

from core.prompts.constants import (
    COMPACT_DECISION_EVIDENCE_MAX_CHARS,
    COMPACT_KEY_FINDINGS_MAX_ITEMS,
    COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS,
)

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.dns_discovery import (
    AMASS_TOOL_ID,
    DNS_ARTIFACT_REF_LIMIT,
    DNS_DIAGNOSTIC_LIMIT,
    DNS_MAPPING_SAMPLE_LIMIT,
    DNS_NAME_SAMPLE_LIMIT,
    DNS_STRUCTURED_SIGNAL_SAMPLE_LIMIT,
    DNS_UNRESOLVED_SAMPLE_LIMIT,
    dns_discovery_adapter,
    registered_dns_discovery_tool_ids,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)
from agent.tools.catalog_visibility import visible_available_tools


def test_dns_discovery_adapter_registers_exact_visible_amass_tool_id() -> None:
    """Amass has deterministic coverage after visible-catalog promotion."""

    assert get_adapter(AMASS_TOOL_ID) is dns_discovery_adapter
    assert registered_dns_discovery_tool_ids() == (AMASS_TOOL_ID,)
    assert AMASS_TOOL_ID in visible_available_tools()


def test_amass_metadata_compacts_names_mappings_diagnostics_and_artifacts() -> None:
    """Amass metadata is compacted without reparsing stdout or leaking artifacts."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "stdout": "ignored.example.com 203.0.113.200",
                "metadata": {
                    "subdomains": [
                        {
                            "subdomain": "api.example.com",
                            "ip": ["192.0.2.20", "2001:db8::5"],
                            "record_types": ["A", "AAAA"],
                            "source": "amass",
                        },
                        {
                            "subdomain": "unresolved.example.com",
                            "ip": [],
                            "record_types": [],
                            "source": "amass",
                        },
                        {
                            "subdomain": "www.example.com",
                            "ip": ["192.0.2.10"],
                            "record_types": ["A"],
                            "source": "amass",
                        },
                    ],
                    "hosts": [
                        {
                            "hostname": "api.example.com",
                            "ip": ["192.0.2.20", "2001:db8::5"],
                        },
                        {"hostname": "unresolved.example.com", "ip": []},
                        {"hostname": "www.example.com", "ip": ["192.0.2.10"]},
                    ],
                    "ips": ["192.0.2.10", "192.0.2.20", "2001:db8::5"],
                    "names_count": 3,
                    "resolved_names_count": 2,
                    "unresolved_names_count": 1,
                    "ip_count": 3,
                    "parse_status": "partial",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": ["incomplete_capture_sections"],
                },
                "artifacts": [
                    "artifacts/amass/amass.json",
                    {
                        "path": "https://objects.local/private/amass.json?X-Amz-Signature=secret",
                        "artifact_id": "artifact-1",
                        "artifact_kind": "object_store",
                    },
                ],
            },
        )
    )

    assert result.summary == (
        "Amass partially parsed DNS output; discovered 3 DNS names: 2 resolved, "
        "1 unresolved, 3 unique IPs."
    )
    assert result.key_findings == (
        "api.example.com resolves to 192.0.2.20",
        "api.example.com resolves to 2001:db8::5",
        "www.example.com resolves to 192.0.2.10",
        "unresolved.example.com: no address returned",
        "diagnostic: incomplete_capture_sections",
    )
    assert result.decision_evidence == (
        "dns mapping: api.example.com -> 192.0.2.20, 2001:db8::5",
        "dns mapping: www.example.com -> 192.0.2.10",
        "unresolved dns name: unresolved.example.com",
        "amass diagnostic: incomplete_capture_sections",
    )
    assert result.structured_signals[:4] == (
        {"type": "kv_pair", "key": "amass_dns_names_count", "value": 3},
        {"type": "kv_pair", "key": "amass_resolved_names_count", "value": 2},
        {"type": "kv_pair", "key": "amass_unresolved_names_count", "value": 1},
        {"type": "kv_pair", "key": "amass_unique_ip_count", "value": 3},
    )
    assert {
        "type": "kv_pair",
        "key": "amass_mapping:api.example.com",
        "value": "192.0.2.20, 2001:db8::5",
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_unresolved_name",
        "value": "unresolved.example.com",
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_artifact_ref",
        "value": "artifact://artifact-1",
    } in result.structured_signals
    assert "ignored.example.com" not in str(result)
    assert result.completeness == "partial"
    assert result.lossiness_risk == "low"


def test_amass_metadata_preserves_fitting_details_and_complete_counts() -> None:
    """Fitting DNS details are complete while count signals keep complete totals."""

    resolved = [
        _subdomain_row(
            f"resolved-{index}.example.com",
            [f"192.0.2.{index}"],
        )
        for index in range(1, DNS_MAPPING_SAMPLE_LIMIT + 3)
    ]
    unresolved = [
        _subdomain_row(f"unresolved-{index}.example.com", [])
        for index in range(1, DNS_UNRESOLVED_SAMPLE_LIMIT + 3)
    ]
    diagnostics = [
        f"diagnostic_{index}" for index in range(1, DNS_DIAGNOSTIC_LIMIT + 3)
    ]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": resolved + unresolved,
                    "hosts": _hosts_from_subdomains(resolved + unresolved),
                    "ips": [item["ip"][0] for item in resolved],
                    "names_count": len(resolved) + len(unresolved),
                    "resolved_names_count": len(resolved),
                    "unresolved_names_count": len(unresolved),
                    "ip_count": len(resolved),
                    "parse_status": "partial",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": diagnostics,
                }
            },
        )
    )

    mapping_findings = [
        finding for finding in result.key_findings if " resolves to " in finding
    ]
    unresolved_findings = [
        finding for finding in result.key_findings if "no address returned" in finding
    ]
    diagnostic_findings = [
        finding for finding in result.key_findings if finding.startswith("diagnostic:")
    ]

    assert result.summary == (
        "Amass partially parsed DNS output; discovered 17 DNS names: 7 resolved, "
        "10 unresolved, 7 unique IPs."
    )
    assert len(mapping_findings) == len(resolved)
    assert len(unresolved_findings) == len(unresolved)
    assert len(diagnostic_findings) == DNS_DIAGNOSTIC_LIMIT
    assert {
        "type": "kv_pair",
        "key": "amass_dns_names_count",
        "value": len(resolved) + len(unresolved),
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_omitted",
        "value": 0,
    } in result.structured_signals


def test_seven_small_amass_dns_mappings_all_survive_key_findings() -> None:
    """Small complete mapping sets are not capped by the old five-row sample."""

    resolved = [
        _subdomain_row(f"resolved-{index}.example.com", [f"192.0.2.{index}"])
        for index in range(1, 8)
    ]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": resolved,
                    "hosts": _hosts_from_subdomains(resolved),
                    "ips": [item["ip"][0] for item in resolved],
                    "names_count": len(resolved),
                    "resolved_names_count": len(resolved),
                    "unresolved_names_count": 0,
                    "ip_count": len(resolved),
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                }
            },
        )
    )

    expected_mappings = tuple(
        f"resolved-{index}.example.com resolves to 192.0.2.{index}"
        for index in range(1, 8)
    )

    assert result.key_findings == expected_mappings
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_total",
        "value": 7,
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_shown",
        "value": 7,
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_omitted",
        "value": 0,
    } in result.structured_signals
    assert result.lossiness_risk == "low"


def test_identical_amass_metadata_produces_byte_equivalent_compact_fields() -> None:
    """Identical normalized metadata emits byte-for-byte equivalent fields."""

    first = dns_discovery_adapter(
        CompressionInput(tool_name=AMASS_TOOL_ID, raw_result=_sample_raw_result())
    )
    second = dns_discovery_adapter(
        CompressionInput(tool_name=AMASS_TOOL_ID, raw_result=_sample_raw_result())
    )

    assert _canonical_bytes(first) == _canonical_bytes(second)


def test_reordered_amass_metadata_produces_same_normalized_compact_result() -> None:
    """Row order does not change the compact DNS result."""

    ordered = _sample_raw_result()
    reordered = {
        "metadata": {
            **ordered["metadata"],
            "subdomains": list(reversed(ordered["metadata"]["subdomains"])),
            "hosts": list(reversed(ordered["metadata"]["hosts"])),
            "ips": list(reversed(ordered["metadata"]["ips"])),
        }
    }

    first = dns_discovery_adapter(
        CompressionInput(tool_name=AMASS_TOOL_ID, raw_result=ordered)
    )
    second = dns_discovery_adapter(
        CompressionInput(tool_name=AMASS_TOOL_ID, raw_result=reordered)
    )

    assert _canonical_bytes(first) == _canonical_bytes(second)


def test_large_amass_result_respects_non_detail_sample_bounds() -> None:
    """Non-detail presentation samples remain bounded by named DNS constants."""

    resolved = [
        _subdomain_row(
            f"resolved-{index:02d}.example.com",
            [f"192.0.2.{index}"],
        )
        for index in range(1, DNS_MAPPING_SAMPLE_LIMIT + DNS_NAME_SAMPLE_LIMIT + 8)
    ]
    unresolved = [
        _subdomain_row(f"unresolved-{index:02d}.example.com", [])
        for index in range(1, DNS_UNRESOLVED_SAMPLE_LIMIT + DNS_NAME_SAMPLE_LIMIT + 8)
    ]
    diagnostics = [
        f"diagnostic_{index}" for index in range(1, DNS_DIAGNOSTIC_LIMIT + 8)
    ]
    artifacts = [
        f"artifacts/amass/result-{index}.json"
        for index in range(1, DNS_ARTIFACT_REF_LIMIT + 8)
    ]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": resolved + unresolved,
                    "hosts": _hosts_from_subdomains(resolved + unresolved),
                    "ips": [item["ip"][0] for item in resolved],
                    "names_count": len(resolved) + len(unresolved),
                    "resolved_names_count": len(resolved),
                    "unresolved_names_count": len(unresolved),
                    "ip_count": len(resolved),
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": diagnostics,
                    "artifacts": artifacts,
                }
            },
        )
    )

    assert len(
        [finding for finding in result.key_findings if " resolves to " in finding]
    ) == len(resolved)
    assert len(
        [finding for finding in result.key_findings if "no address returned" in finding]
    ) == len(unresolved)
    assert len(
        [finding for finding in result.key_findings if finding.startswith("diagnostic:")]
    ) == DNS_DIAGNOSTIC_LIMIT
    assert len(result.key_findings) <= COMPACT_KEY_FINDINGS_MAX_ITEMS
    assert len("\n".join(result.key_findings)) <= COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS
    assert len(result.decision_evidence) == (
        DNS_MAPPING_SAMPLE_LIMIT + DNS_UNRESOLVED_SAMPLE_LIMIT + DNS_DIAGNOSTIC_LIMIT
    )
    assert len(result.structured_signals) <= 7 + DNS_STRUCTURED_SIGNAL_SAMPLE_LIMIT
    assert _signal_count(result.structured_signals, "amass_dns_name_sample") <= (
        DNS_NAME_SAMPLE_LIMIT
    )
    assert _signal_prefix_count(result.structured_signals, "amass_mapping:") <= (
        DNS_MAPPING_SAMPLE_LIMIT
    )
    assert _signal_count(result.structured_signals, "amass_unresolved_name") <= (
        DNS_UNRESOLVED_SAMPLE_LIMIT
    )
    assert _signal_count(result.structured_signals, "amass_diagnostic") <= (
        DNS_DIAGNOSTIC_LIMIT
    )
    assert _signal_count(result.structured_signals, "amass_artifact_ref") <= (
        DNS_ARTIFACT_REF_LIMIT
    )
    assert {
        "type": "kv_pair",
        "key": "amass_dns_names_count",
        "value": len(resolved) + len(unresolved),
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_omitted",
        "value": 0,
    } in result.structured_signals


def test_large_amass_dns_details_report_exact_omission_accounting() -> None:
    """Large DNS detail sets are bounded without losing summary/artifact truth."""

    resolved = [
        _subdomain_row(f"resolved-{index:02d}.example.com", [f"192.0.2.{index}"])
        for index in range(1, COMPACT_KEY_FINDINGS_MAX_ITEMS + 5)
    ]
    artifacts = [
        {
            "artifact_id": "large-amass",
            "artifact_kind": "object_store",
            "path": "s3://tenant-private/large-amass.json",
        }
    ]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": resolved,
                    "hosts": _hosts_from_subdomains(resolved),
                    "ips": [item["ip"][0] for item in resolved],
                    "names_count": len(resolved),
                    "resolved_names_count": len(resolved),
                    "unresolved_names_count": 0,
                    "ip_count": len(resolved),
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                    "artifacts": artifacts,
                }
            },
        )
    )

    mapping_findings = [
        finding for finding in result.key_findings if " resolves to " in finding
    ]
    omission_findings = [
        finding
        for finding in result.key_findings
        if finding.startswith("key findings omitted:")
    ]

    expected_shown = COMPACT_KEY_FINDINGS_MAX_ITEMS - 1
    expected_omitted = len(resolved) - expected_shown

    assert len(result.key_findings) <= COMPACT_KEY_FINDINGS_MAX_ITEMS
    assert len("\n".join(result.key_findings)) <= COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS
    assert len(mapping_findings) == expected_shown
    assert omission_findings == [
        (
            "key findings omitted: showing "
            f"{expected_shown} of {len(resolved)}; omitted {expected_omitted}."
        )
    ]
    assert result.summary == (
        f"Amass discovered {len(resolved)} DNS names: {len(resolved)} resolved, "
        f"0 unresolved, {len(resolved)} unique IPs."
    )
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_total",
        "value": len(resolved),
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_shown",
        "value": expected_shown,
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_omitted",
        "value": expected_omitted,
    } in result.structured_signals
    assert result.lossiness_risk == "medium"


def test_oversized_sanitized_artifact_handle_cannot_reduce_dns_budget() -> None:
    """A huge artifact handle stays out of key-finding budget reservation."""

    oversized_artifact_id = "artifact-" + ("x" * COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS)
    resolved = [_subdomain_row("api.example.com", ["192.0.2.20"])]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": resolved,
                    "hosts": _hosts_from_subdomains(resolved),
                    "ips": ["192.0.2.20"],
                    "names_count": 1,
                    "resolved_names_count": 1,
                    "unresolved_names_count": 0,
                    "ip_count": 1,
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                    "artifacts": [
                        {
                            "artifact_id": oversized_artifact_id,
                            "artifact_kind": "object_store",
                            "path": "s3://tenant-private/amass/raw.json",
                        }
                    ],
                },
            },
        )
    )

    artifact_signals = [
        signal
        for signal in result.structured_signals
        if signal.get("key") == "amass_artifact_ref"
    ]

    assert result.key_findings == ("api.example.com resolves to 192.0.2.20",)
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_shown",
        "value": 1,
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_dns_detail_omitted",
        "value": 0,
    } in result.structured_signals
    assert artifact_signals
    assert str(artifact_signals[0]["value"]).startswith("artifact://artifact-")
    assert len(str(artifact_signals[0]["value"])) <= COMPACT_DECISION_EVIDENCE_MAX_CHARS
    assert result.lossiness_risk == "low"


def test_long_amass_names_and_diagnostics_are_compacted_in_all_text_outputs() -> None:
    """Long samples and diagnostics are truncated through compact helpers."""

    long_diagnostic = "diagnostic_" + ("detail-" * 180)
    many_addresses = [
        f"2001:db8::{index:x}" for index in range(1, 90)
    ]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "hosts": [
                        {
                            "hostname": "very-long-name.example.com",
                            "ip": many_addresses,
                        }
                    ],
                    "subdomains": [
                        _subdomain_row("very-long-name.example.com", many_addresses)
                    ],
                    "ips": many_addresses,
                    "names_count": 1,
                    "resolved_names_count": 1,
                    "unresolved_names_count": 0,
                    "ip_count": len(many_addresses),
                    "parse_status": "partial",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": [long_diagnostic],
                }
            },
        )
    )

    flattened_strings = _flatten_strings(asdict(result))

    assert any(value.endswith("...") for value in flattened_strings)
    assert all(
        len(value) <= COMPACT_DECISION_EVIDENCE_MAX_CHARS
        for value in flattened_strings
    )


def test_amass_ipv4_and_ipv6_mappings_remain_distinguishable() -> None:
    """A and AAAA relationships stay visible in compact mappings."""

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": [
                        _subdomain_row(
                            "dualstack.example.com",
                            ["2001:db8::5", "192.0.2.5"],
                        )
                    ],
                    "hosts": [
                        {
                            "hostname": "dualstack.example.com",
                            "ip": ["2001:db8::5", "192.0.2.5"],
                        }
                    ],
                    "ips": ["2001:db8::5", "192.0.2.5"],
                    "names_count": 1,
                    "resolved_names_count": 1,
                    "unresolved_names_count": 0,
                    "ip_count": 2,
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                }
            },
        )
    )

    assert result.summary == (
        "Amass discovered 1 DNS names: 1 resolved, 0 unresolved, 2 unique IPs."
    )
    assert result.key_findings == (
        "dualstack.example.com resolves to 192.0.2.5",
        "dualstack.example.com resolves to 2001:db8::5",
    )
    assert {
        "type": "kv_pair",
        "key": "amass_mapping:dualstack.example.com",
        "value": "192.0.2.5, 2001:db8::5",
    } in result.structured_signals


def test_unresolved_amass_names_are_not_described_as_nonexistent() -> None:
    """Unresolved Amass names mean no address was returned in this result."""

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": [_subdomain_row("later.example.com", [])],
                    "hosts": [{"hostname": "later.example.com", "ip": []}],
                    "ips": [],
                    "names_count": 1,
                    "resolved_names_count": 0,
                    "unresolved_names_count": 1,
                    "ip_count": 0,
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                }
            },
        )
    )

    rendered = str(result).lower()

    assert "later.example.com: no address returned" in result.key_findings
    assert "nonexistent" not in rendered
    assert "not found" not in rendered
    assert "nxdomain" not in rendered


def test_amass_parser_statuses_produce_truthful_summaries() -> None:
    """Empty, partial, and failed parser statuses remain explicit."""

    empty = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": [],
                    "hosts": [],
                    "ips": [],
                    "names_count": 0,
                    "resolved_names_count": 0,
                    "unresolved_names_count": 0,
                    "ip_count": 0,
                    "parse_status": "empty",
                    "capture_format": "amass_v5_subs_text",
                }
            },
        )
    )
    partial = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": [_subdomain_row("partial.example.com", [])],
                    "hosts": [{"hostname": "partial.example.com", "ip": []}],
                    "ips": [],
                    "names_count": 1,
                    "resolved_names_count": 0,
                    "unresolved_names_count": 1,
                    "ip_count": 0,
                    "parse_status": "partial",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": ["incomplete_capture_sections"],
                }
            },
        )
    )
    failed = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": [],
                    "hosts": [],
                    "ips": [],
                    "names_count": 0,
                    "resolved_names_count": 0,
                    "unresolved_names_count": 0,
                    "ip_count": 0,
                    "parse_status": "failed",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": ["amass_exit_nonzero"],
                }
            },
        )
    )

    assert empty.summary == (
        "Amass returned no DNS names: 0 resolved, 0 unresolved, 0 unique IPs."
    )
    assert partial.summary == (
        "Amass partially parsed DNS output; discovered 1 DNS names: 0 resolved, "
        "1 unresolved, 0 unique IPs."
    )
    assert failed.summary == (
        "Amass parsing failed; discovered 0 DNS names: 0 resolved, 0 unresolved, "
        "0 unique IPs."
    )


def test_amass_execution_status_and_roles_are_compacted_from_metadata() -> None:
    """Completion and discovery roles come from normalized metadata, not stdout."""

    metadata = {
        "subdomains": [
            {
                **_subdomain_row("example.com", []),
                "discovery_role": "scope_seed",
                "result_scope": "task_cumulative",
            },
            {
                **_subdomain_row("old.example.com", []),
                "discovery_role": "prior_known",
                "result_scope": "task_cumulative",
            },
            {
                **_subdomain_row("new.example.com", ["192.0.2.44"]),
                "discovery_role": "newly_discovered",
                "result_scope": "task_cumulative",
            },
        ],
        "hosts": [
            {"hostname": "example.com", "ip": []},
            {"hostname": "old.example.com", "ip": []},
            {"hostname": "new.example.com", "ip": ["192.0.2.44"]},
        ],
        "ips": ["192.0.2.44"],
        "names_count": 3,
        "resolved_names_count": 1,
        "unresolved_names_count": 2,
        "ip_count": 1,
        "parse_status": "success",
        "enumeration_status": "timed_out",
        "result_completeness": "partial",
        "partial_results": True,
        "enumeration_exit_code": 124,
        "seed_names_count": 1,
        "prior_names_count": 1,
        "newly_discovered_names_count": 1,
        "discovered_names_count": 1,
        "capture_format": "amass_v5_subs_text",
        "diagnostics": [],
    }

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "stdout": "ignored raw collector text",
                "metadata": metadata,
            },
        )
    )

    assert result.summary == (
        "Amass enumeration timed out with partial results; parser status success; "
        "3 DNS names: 1 seed, 1 newly discovered, 1 prior, 1 resolved, "
        "2 unresolved, 1 unique IPs."
    )
    assert {
        "type": "kv_pair",
        "key": "amass_enumeration_status",
        "value": "timed_out",
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_newly_discovered_names_count",
        "value": 1,
    } in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "amass_scope_seed",
        "value": "example.com",
    } in result.structured_signals
    assert "ignored raw collector text" not in str(result)


def test_amass_adapter_falls_back_for_incompatible_metadata_and_unsupported_tools() -> None:
    """Incompatible metadata and other tool ids use explicit fallback contracts."""

    incompatible_metadata = (
        {"diagnostics": ["generic_tool_warning"]},
        {"hosts": []},
        {"ips": ["192.0.2.1"]},
        {"parse_status": "success"},
        {
            "capture_format": "nmap_xml",
            "hosts": [{"hostname": "example.com", "ip": ["192.0.2.1"]}],
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": ["NOT_A_NORMALIZED_ROW.EXAMPLE.COM"],
            "hosts": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [{"subdomain": "api.example.com", "ip": []}],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [{"name": "api.example.com", "ip": []}],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "subdomains": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [_subdomain_row("api.example.com", [])],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "ips": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "mystery",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [{"subdomain": "api.example.com", "ip": []}],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "ips": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [
                {
                    "subdomain": "api.example.com",
                    "ip": [],
                    "record_types": [],
                }
            ],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "ips": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [
                {
                    "subdomain": "api.example.com",
                    "ip": [],
                    "record_types": [],
                    "source": "other",
                }
            ],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "ips": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [
                {
                    "subdomain": "api.example.com",
                    "ip": ["192.0.2.5"],
                    "record_types": ["AAAA"],
                    "source": "amass",
                }
            ],
            "hosts": [{"hostname": "api.example.com", "ip": ["192.0.2.5"]}],
            "ips": ["192.0.2.5"],
            "names_count": 1,
            "resolved_names_count": 1,
            "unresolved_names_count": 0,
            "ip_count": 1,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [
                {
                    "subdomain": "api.example.com",
                    "ip": ["2001:db8::5"],
                    "record_types": ["A"],
                    "source": "amass",
                }
            ],
            "hosts": [{"hostname": "api.example.com", "ip": ["2001:db8::5"]}],
            "ips": ["2001:db8::5"],
            "names_count": 1,
            "resolved_names_count": 1,
            "unresolved_names_count": 0,
            "ip_count": 1,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [
                {
                    "subdomain": "api.example.com",
                    "ip": [],
                    "record_types": ["A"],
                    "source": "amass",
                }
            ],
            "hosts": [{"hostname": "api.example.com", "ip": []}],
            "ips": [],
            "names_count": 1,
            "resolved_names_count": 0,
            "unresolved_names_count": 1,
            "ip_count": 0,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [_subdomain_row("api.example.com", ["192.0.2.5"])],
            "hosts": [{"hostname": "other.example.com", "ip": ["192.0.2.5"]}],
            "ips": ["192.0.2.5"],
            "names_count": 1,
            "resolved_names_count": 1,
            "unresolved_names_count": 0,
            "ip_count": 1,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [_subdomain_row("api.example.com", ["192.0.2.5"])],
            "hosts": [{"hostname": "api.example.com", "ip": ["192.0.2.5"]}],
            "ips": ["198.51.100.99"],
            "names_count": 1,
            "resolved_names_count": 1,
            "unresolved_names_count": 0,
            "ip_count": 1,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [_subdomain_row("api.example.com", ["192.0.2.5"])],
            "hosts": [{"hostname": "api.example.com", "ip": ["192.0.2.5"]}],
            "ips": ["192.0.2.5"],
            "names_count": 99,
            "resolved_names_count": 50,
            "unresolved_names_count": 49,
            "ip_count": 77,
            "parse_status": "success",
        },
        {
            "capture_format": "amass_v5_subs_text",
            "subdomains": [
                {
                    "subdomain": "api.example.com",
                    "ip": ["2001:0db8::5"],
                    "record_types": ["AAAA"],
                    "source": "amass",
                }
            ],
            "hosts": [{"hostname": "api.example.com", "ip": ["2001:0db8::5"]}],
            "ips": ["2001:db8::5"],
            "names_count": 1,
            "resolved_names_count": 1,
            "unresolved_names_count": 0,
            "ip_count": 1,
            "parse_status": "success",
        },
    )

    incompatible_results = tuple(
        compress_deterministically(
            CompressionInput(
                tool_name=AMASS_TOOL_ID,
                raw_result={"metadata": metadata},
            )
        )
        for metadata in incompatible_metadata
    )
    unsupported = dns_discovery_adapter(
        CompressionInput(
            tool_name="information_gathering.dns.other",
            raw_result={"metadata": _sample_raw_result()["metadata"]},
        )
    )

    assert all(result.completeness == "none" for result in incompatible_results)
    assert all(
        result.fallback_reason == "no_dns_discovery_metadata"
        for result in incompatible_results
    )
    assert unsupported.completeness == "none"
    assert unsupported.fallback_reason == "unsupported_dns_discovery_tool"


def test_amass_artifact_references_survive_adapter_output() -> None:
    """Metadata-only artifacts remain available for omitted DNS facts."""

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    **_sample_raw_result()["metadata"],
                    "artifacts": [
                        {
                            "artifact_id": "rich-amass",
                            "artifact_kind": "object_store",
                            "path": "s3://tenant-private/rich-amass.json",
                        }
                    ],
                }
            },
        )
    )

    assert all(not finding.startswith("artifact:") for finding in result.key_findings)
    assert {
        "type": "kv_pair",
        "key": "amass_artifact_ref",
        "value": "artifact://rich-amass",
    } in result.structured_signals


def test_large_scope_seed_samples_preserve_amass_artifact_ref() -> None:
    """Artifact refs survive even when optional structured samples fill the cap."""

    resolved = []
    for index in range(1, DNS_NAME_SAMPLE_LIMIT + 1):
        row = _subdomain_row(f"seed-{index:02d}.example.com", [f"192.0.2.{index}"])
        row["discovery_role"] = "scope_seed"
        resolved.append(row)
    unresolved = [
        _subdomain_row(f"unresolved-{index:02d}.example.com", [])
        for index in range(1, DNS_UNRESOLVED_SAMPLE_LIMIT + 1)
    ]
    diagnostics = [
        f"diagnostic_{index}" for index in range(1, DNS_DIAGNOSTIC_LIMIT + 1)
    ]

    result = dns_discovery_adapter(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "metadata": {
                    "subdomains": resolved + unresolved,
                    "hosts": _hosts_from_subdomains(resolved + unresolved),
                    "ips": [item["ip"][0] for item in resolved],
                    "names_count": len(resolved) + len(unresolved),
                    "resolved_names_count": len(resolved),
                    "unresolved_names_count": len(unresolved),
                    "ip_count": len(resolved),
                    "parse_status": "success",
                    "capture_format": "amass_v5_subs_text",
                    "diagnostics": diagnostics,
                    "artifacts": [
                        {
                            "artifact_id": "pressure-amass",
                            "artifact_kind": "object_store",
                            "path": "s3://tenant-private/pressure-amass.json",
                        }
                    ],
                }
            },
        )
    )

    sample_signal_count = len(result.structured_signals) - 7

    assert sample_signal_count <= DNS_STRUCTURED_SIGNAL_SAMPLE_LIMIT
    assert {
        "type": "kv_pair",
        "key": "amass_artifact_ref",
        "value": "artifact://pressure-amass",
    } in result.structured_signals


def test_amass_adapter_falls_back_without_normalized_metadata_or_raw_parsing() -> None:
    """Raw stdout alone is not parsed by the deterministic compression adapter."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=AMASS_TOOL_ID,
            raw_result={
                "stdout": "\n".join(
                    [
                        "__DROWAI_AMASS_V5_NAMES_BEGIN__",
                        "api.example.com",
                        "__DROWAI_AMASS_V5_NAMES_END__",
                        "__DROWAI_AMASS_V5_RESOLVED_BEGIN__",
                        "api.example.com 192.0.2.20",
                        "__DROWAI_AMASS_V5_RESOLVED_END__",
                    ]
                )
            },
        )
    )

    assert result.completeness == "none"
    assert result.fallback_reason == "no_dns_discovery_metadata"


def _sample_raw_result() -> Mapping[str, Any]:
    """Return representative normalized Amass metadata for determinism tests."""

    return {
        "metadata": {
            "subdomains": [
                _subdomain_row(
                    "api.example.com",
                    ["2001:db8::5", "192.0.2.20"],
                ),
                _subdomain_row("unresolved.example.com", []),
                _subdomain_row("www.example.com", ["192.0.2.10"]),
            ],
            "hosts": [
                {"hostname": "api.example.com", "ip": ["192.0.2.20", "2001:db8::5"]},
                {"hostname": "unresolved.example.com", "ip": []},
                {"hostname": "www.example.com", "ip": ["192.0.2.10"]},
            ],
            "ips": ["2001:db8::5", "192.0.2.10", "192.0.2.20"],
            "names_count": 3,
            "resolved_names_count": 2,
            "unresolved_names_count": 1,
            "ip_count": 3,
            "parse_status": "success",
            "capture_format": "amass_v5_subs_text",
            "diagnostics": ["incomplete_capture_sections"],
        }
    }


def _hosts_from_subdomains(subdomains: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return Amass compatibility host rows for normalized subdomain rows."""

    return [
        {"hostname": item["subdomain"], "ip": list(item.get("ip") or [])}
        for item in subdomains
    ]


def _subdomain_row(name: str, ips: list[str]) -> dict[str, Any]:
    """Return one normalized Amass subdomain row."""

    return {
        "subdomain": name,
        "ip": list(ips),
        "record_types": _record_types_for_ips(ips),
        "source": "amass",
    }


def _record_types_for_ips(ips: list[str]) -> list[str]:
    """Return deterministic Amass record types for canonical IP strings."""

    return [
        record_type
        for version, record_type in ((4, "A"), (6, "AAAA"))
        if any(ipaddress.ip_address(address).version == version for address in ips)
    ]


def _canonical_bytes(value: Any) -> bytes:
    """Return stable JSON bytes for deterministic result comparisons."""

    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":")).encode()


def _signal_count(signals: tuple[Mapping[str, Any], ...], key: str) -> int:
    """Return how many kv signals use an exact key."""

    return sum(1 for signal in signals if signal.get("key") == key)


def _signal_prefix_count(signals: tuple[Mapping[str, Any], ...], prefix: str) -> int:
    """Return how many kv signals use a key prefix."""

    return sum(
        1
        for signal in signals
        if str(signal.get("key") or "").startswith(prefix)
    )


def _flatten_strings(value: Any) -> list[str]:
    """Return all strings in a nested result structure."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_flatten_strings(item))
        return strings
    if isinstance(value, (list, tuple)):
        strings = []
        for item in value:
            strings.extend(_flatten_strings(item))
        return strings
    return []
