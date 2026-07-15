"""PCAP-specific deterministic compression helpers.

This module projects already-parsed tshark/PCAP metadata into compact evidence.
It does not execute packet tools, read capture files, import backend knowledge
adapters, or inspect raw packet bytes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional

from agent.tools.pcap_compaction import build_pcap_compaction

from .common import (
    _metadata_compact_decision_evidence,
    _metadata_compact_key_findings,
    _metadata_compact_summary,
)
from .contracts import CompressionInput, DeterministicCompressionResult

TSHARK_TOOL_ID = "sniffing_spoofing.network_sniffers.tshark"

_PCAP_METADATA_KEYS = frozenset(
    {
        "analysis_mode",
        "auth_indicators",
        "auth_sequences",
        "compact_decision_evidence",
        "compact_key_findings",
        "compact_summary",
        "conversations",
        "credential_events",
        "dns",
        "errors",
        "field_extract",
        "ftp",
        "http",
        "limits",
        "output_format",
        "pcap",
        "pcap_compact",
        "secret_exposure",
        "tls",
        "warnings",
    }
)


def pcap_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project tshark-authored PCAP compact metadata into deterministic facts."""

    metadata = _runtime_metadata(input_data.raw_result)
    if not metadata or not _has_pcap_metadata(metadata):
        return DeterministicCompressionResult.none(
            fallback_reason="no_pcap_metadata",
        )

    generated = _generated_compact_payload(metadata, source_tool=input_data.tool_name)
    summary = _metadata_compact_summary(input_data.raw_result) or _text_or_none(
        generated.get("compact_summary")
    )
    key_findings = tuple(_metadata_compact_key_findings(input_data.raw_result))
    if not key_findings:
        key_findings = _string_tuple(generated.get("compact_key_findings"))

    decision_evidence = tuple(
        _metadata_compact_decision_evidence(input_data.raw_result)
    )
    if not decision_evidence:
        decision_evidence = _string_tuple(generated.get("compact_decision_evidence"))

    pcap_compact = metadata.get("pcap_compact")
    if not isinstance(pcap_compact, Mapping):
        pcap_compact = generated.get("pcap_compact")
    structured_signals = (
        ({"kind": "pcap_compact", "pcap_compact": dict(pcap_compact)},)
        if isinstance(pcap_compact, Mapping)
        else ()
    )

    if summary is None and not key_findings and not decision_evidence:
        return DeterministicCompressionResult.none(
            fallback_reason="no_pcap_compact_fields",
        )

    return DeterministicCompressionResult(
        summary=summary,
        key_findings=key_findings,
        structured_signals=structured_signals,
        decision_evidence=decision_evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def register_pcap_adapters() -> None:
    """Register deterministic PCAP adapters for tshark compact metadata."""

    from .registry import register_adapter

    register_adapter(TSHARK_TOOL_ID, pcap_adapter)


def _runtime_metadata(raw_result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return runtime metadata when present."""

    metadata = raw_result.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _has_pcap_metadata(metadata: Mapping[str, Any]) -> bool:
    """Return whether metadata carries tshark/PCAP compact source fields."""

    return any(key in metadata for key in _PCAP_METADATA_KEYS)


def _generated_compact_payload(
    metadata: Mapping[str, Any],
    *,
    source_tool: str,
) -> Dict[str, Any]:
    """Build missing compact fields from normalized tshark metadata."""

    return build_pcap_compaction(
        metadata,
        source_tool=_text_or_none(source_tool) or TSHARK_TOOL_ID,
    )


def _text_or_none(value: Any) -> Optional[str]:
    """Return stripped text or None."""

    text = str(value or "").strip()
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Return non-empty string tuple values in first-seen order."""

    if not isinstance(value, list):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


register_pcap_adapters()
