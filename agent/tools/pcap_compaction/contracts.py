"""Shared constants for deterministic PCAP compact envelopes."""

from __future__ import annotations

PCAP_COMPACT_SCHEMA_VERSION = "pcap.compact.v1"

PCAP_COMPACT_SECTION_LIMIT = 10
PCAP_COMPACT_FINDING_LIMIT = 20
PCAP_COMPACT_EVIDENCE_LIMIT = 10
PCAP_COMPACT_PIVOT_LIMIT = 10
PCAP_COMPACT_TEXT_LIMIT = 240

PCAP_PROTOCOL_EVIDENCE_KEYS = (
    "dns",
    "http",
    "tls",
    "ftp",
    "auth_indicators",
    "field_extract",
)

PCAP_COMPACT_LIST_KEYS = (
    "conversations",
    "dns",
    "http",
    "tls",
    "ftp",
    "auth_indicators",
    "secret_exposure",
    "credential_events",
    "auth_sequences",
    "field_extract",
    "warnings",
    "errors",
)
