"""Tests for CPE 2.3 parsing helpers."""

from __future__ import annotations

from backend.services.cve_indexing.cpe import extract_cpe_identity_tokens, parse_cpe23


def test_parse_cpe23_parses_valid_uri_fields() -> None:
    parsed = parse_cpe23("cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*")

    assert parsed is not None
    assert parsed.part == "a"
    assert parsed.vendor == "apache"
    assert parsed.product == "log4j"
    assert parsed.version == "2.14.1"
    assert parsed.vendor_norm == "apache"
    assert parsed.product_norm == "log4j"


def test_parse_cpe23_normalizes_whitespace_and_case() -> None:
    parsed = parse_cpe23(" CPE:2.3:a:Apache:Log4j:*:*:*:*:*:*:*:* ")

    assert parsed is not None
    assert parsed.vendor_norm == "apache"
    assert parsed.product_norm == "log4j"


def test_parse_cpe23_rejects_malformed_values() -> None:
    assert parse_cpe23(None) is None
    assert parse_cpe23("") is None
    assert parse_cpe23("cpe:2.2:a:apache:log4j:2.14.1") is None
    assert parse_cpe23("cpe:2.3:a:apache:log4j") is None
    assert parse_cpe23("not-a-cpe") is None


def test_extract_cpe_identity_tokens_returns_deduped_ordered_tokens() -> None:
    tokens = extract_cpe_identity_tokens(
        [
            "cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*",
            "cpe:2.3:a:apache:log4j-core:2.14.1:*:*:*:*:*:*:*",
            "bad-value",
        ]
    )

    assert tokens == ("apache", "log4j", "core")


def test_extract_cpe_identity_tokens_handles_empty_or_wildcard_values() -> None:
    tokens = extract_cpe_identity_tokens(
        [
            "cpe:2.3:a:*:*:*:*:*:*:*:*:*:*",
            "cpe:2.3:a:apache:log4j:*:*:*:*:*:*:*:*",
        ]
    )

    assert "apache" in tokens
    assert "log4j" in tokens
