"""Tests for focused CVE affected-entry extraction helpers."""

from __future__ import annotations

from backend.services.cve_indexing.affected_projection import extract_cve_affected_entries


def test_extract_affected_entries_preserves_raw_and_normalized_values() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": "  OpenSSL Foundation  ",
                        "product": "  OpenSSL  ",
                        "defaultStatus": "affected",
                        "versions": [{"version": "3.0.13", "status": "affected"}],
                        "cpes": [" cpe:2.3:a:openssl:openssl:3.0.13:*:*:*:*:*:*:* "],
                    }
                ]
            }
        }
    }

    rows = extract_cve_affected_entries(cve_id="CVE-2026-1111", cve_json=payload)

    assert len(rows) == 1
    row = rows[0]
    assert row.cve_id == "CVE-2026-1111"
    assert row.vendor_raw == "OpenSSL Foundation"
    assert row.vendor_norm == "openssl foundation"
    assert row.product_raw == "OpenSSL"
    assert row.product_norm == "openssl"
    assert row.default_status == "affected"
    assert row.versions_json == [{"version": "3.0.13", "status": "affected"}]
    assert row.cpes_json == ["cpe:2.3:a:openssl:openssl:3.0.13:*:*:*:*:*:*:*"]


def test_extract_affected_entries_skips_malformed_and_unsupported_entries() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    "bad-entry",
                    {"vendor": "acme"},  # no product/cpe identity => skip
                    {
                        "vendor": "Acme",
                        "product": "Widget",
                        "versions": "invalid",
                        "cpes": ["", 123, " cpe:2.3:a:acme:widget:*:*:*:*:*:*:*:* "],
                    },
                ]
            }
        }
    }

    rows = extract_cve_affected_entries(cve_id="CVE-2026-2222", cve_json=payload)

    assert len(rows) == 1
    row = rows[0]
    assert row.vendor_raw == "Acme"
    assert row.vendor_norm == "acme"
    assert row.product_raw == "Widget"
    assert row.product_norm == "widget"
    assert row.versions_json is None
    assert row.cpes_json == ["cpe:2.3:a:acme:widget:*:*:*:*:*:*:*:*"]


def test_extract_affected_entries_returns_empty_when_affected_path_missing() -> None:
    rows = extract_cve_affected_entries(
        cve_id="CVE-2026-3333",
        cve_json={"containers": {"cna": {"affected": None}}},
    )
    assert rows == ()


def test_extract_affected_entries_truncates_normalized_names_to_index_limit() -> None:
    vendor = "V" * 400
    product = "P" * 420
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": vendor,
                        "product": product,
                        "defaultStatus": "affected",
                    }
                ]
            }
        }
    }

    rows = extract_cve_affected_entries(cve_id="CVE-2026-4444", cve_json=payload)

    assert len(rows) == 1
    row = rows[0]
    assert row.vendor_raw == vendor
    assert row.product_raw == product
    assert row.vendor_norm == vendor.lower()[:255]
    assert row.product_norm == product.lower()[:255]
    assert len(row.vendor_norm or "") == 255
    assert len(row.product_norm or "") == 255
