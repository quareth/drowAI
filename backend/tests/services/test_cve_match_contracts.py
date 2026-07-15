"""Tests for simplified product-version CVE lookup contracts."""

from __future__ import annotations

import pytest

from backend.services.cve_indexing.match_contracts import (
    CveLookupMatch,
    CveLookupRequest,
    CveLookupResponse,
)


def test_lookup_request_accepts_product_version_shape() -> None:
    request = CveLookupRequest(product="  OpenSSL  ", version=" 3.0.13 ", max_results=3)

    assert request.product == "openssl"
    assert request.version == "3.0.13"
    assert request.max_results == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"product": "", "version": "1.2.3"}, "product is required"),
        ({"product": "openssl", "version": ""}, "version is required"),
        ({"product": "openssl", "version": "3.0.13", "max_results": 0}, "max_results must be >= 1"),
    ],
)
def test_lookup_request_rejects_invalid_input(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CveLookupRequest(**kwargs)


def test_lookup_match_requires_rationale_for_explanation() -> None:
    with pytest.raises(ValueError, match="rationale is required"):
        CveLookupMatch(cve_id="CVE-2026-0001", rationale=" ")


def test_lookup_response_normalizes_matches_and_message() -> None:
    response = CveLookupResponse(
        matches=(
            CveLookupMatch(cve_id="CVE-2026-1111", rationale="product exact match; version exact match"),
        ),
        message=" ok ",
    )

    assert len(response.matches) == 1
    assert response.matches[0].cve_id == "CVE-2026-1111"
    assert response.message == "ok"
