"""Tests for multi-signal CVE product candidate resolution."""

from __future__ import annotations

from backend.models.cve import CveAffectedProduct
from backend.services.cve_indexing.product_resolver import ProductResolver


def _row(
    *,
    cve_id: str,
    product_norm: str,
    cpes_json: list[str] | None = None,
) -> CveAffectedProduct:
    return CveAffectedProduct(
        cve_record_id=1,
        cve_id=cve_id,
        vendor_raw="Apache",
        vendor_norm="apache",
        product_raw=product_norm,
        product_norm=product_norm,
        default_status="affected",
        versions_json=[{"version": "1.0.0", "status": "affected"}],
        cpes_json=cpes_json,
    )


def test_resolver_returns_exact_match_quality(monkeypatch) -> None:
    resolver = ProductResolver(db=object())
    exact_row = _row(cve_id="CVE-TEST-PRD-0001", product_norm="openssl")

    monkeypatch.setattr(resolver, "_query_exact", lambda **_: (exact_row,))
    monkeypatch.setattr(resolver, "_query_cpe_tokens", lambda **_: ())
    monkeypatch.setattr(resolver, "_query_product_tokens", lambda **_: ())

    rows = resolver.resolve(product="OpenSSL")

    assert rows
    assert rows[0].row.cve_id == "CVE-TEST-PRD-0001"
    assert rows[0].match_quality == "exact"


def test_resolver_finds_candidate_via_cpe_tokens(monkeypatch) -> None:
    resolver = ProductResolver(db=object())
    cpe_row = _row(
        cve_id="CVE-TEST-PRD-0002",
        product_norm="apache log4j2",
        cpes_json=["cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*"],
    )

    monkeypatch.setattr(resolver, "_query_exact", lambda **_: ())
    monkeypatch.setattr(resolver, "_query_cpe_tokens", lambda **_: (cpe_row,))
    monkeypatch.setattr(resolver, "_query_product_tokens", lambda **_: ())

    rows = resolver.resolve(product="Apache Log4j")

    assert rows
    assert rows[0].row.cve_id == "CVE-TEST-PRD-0002"
    assert rows[0].match_quality == "cpe"


def test_resolver_dedupes_rows_when_multiple_strategies_match(monkeypatch) -> None:
    resolver = ProductResolver(db=object())
    row = _row(
        cve_id="CVE-TEST-PRD-0003",
        product_norm="apache log4j",
        cpes_json=["cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*"],
    )

    monkeypatch.setattr(resolver, "_query_exact", lambda **_: (row,))
    monkeypatch.setattr(resolver, "_query_cpe_tokens", lambda **_: (row,))
    monkeypatch.setattr(resolver, "_query_product_tokens", lambda **_: (row,))

    rows = resolver.resolve(product="Apache Log4j")
    cve_ids = [item.row.cve_id for item in rows]

    assert cve_ids.count("CVE-TEST-PRD-0003") == 1
    assert rows[0].match_quality == "exact"


def test_resolver_handles_empty_product_input() -> None:
    assert ProductResolver(db=object()).resolve(product="   ") == ()
