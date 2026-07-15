"""Integration-style tests for projected CVE matching behavior."""

from __future__ import annotations

from backend.models.cve import CveRecord
from backend.services.cve_indexing.match_contracts import CveLookupRequest
from backend.services.cve_indexing.match_service import CveMatchService
from backend.services.cve_indexing.product_resolver import ResolvedProjectionCandidate
from backend.services.cve_indexing.sync_record_projection import build_affected_product_projection


def _record(cve_id: str, *, severity: str, cvss: float) -> CveRecord:
    return CveRecord(
        cve_id=cve_id,
        source="cvelist_v5",
        record_state="published",
        title=cve_id,
        description=cve_id,
        severity=severity,
        metrics={"entries": [{"cvssV3_1": {"baseScore": cvss}}]},
        cve_json={},
    )


def _candidates(cve_id: str, payload: dict, *, quality: str) -> tuple[ResolvedProjectionCandidate, ...]:
    rows = build_affected_product_projection(cve_id=cve_id, cve_json=payload)
    return tuple(ResolvedProjectionCandidate(row=row, match_quality=quality) for row in rows)  # type: ignore[arg-type]


def test_log4shell_2141_is_applicable_with_changes_rule() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": "Apache Software Foundation",
                        "product": "Apache Log4j2",
                        "cpes": ["cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*"],
                        "versions": [
                            {
                                "version": "2.0-beta9",
                                "status": "affected",
                                "lessThan": "log4j-core*",
                                "versionType": "custom",
                                "changes": [
                                    {"at": "2.3.1", "status": "unaffected"},
                                    {"at": "2.4", "status": "affected"},
                                    {"at": "2.12.2", "status": "unaffected"},
                                    {"at": "2.13.0", "status": "affected"},
                                    {"at": "2.15.0", "status": "unaffected"},
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: _candidates("CVE-2021-44228", payload, quality="cpe"),
        record_loader=lambda **_: {"CVE-2021-44228": _record("CVE-2021-44228", severity="critical", cvss=10.0)},
    )

    response = service.lookup(CveLookupRequest(product="Apache Log4j", version="2.14.1", max_results=5))

    assert response.matches
    assert response.matches[0].cve_id == "CVE-2021-44228"
    assert response.matches[0].version_applicable is True


def test_log4shell_2150_is_excluded_as_unaffected() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": "Apache Software Foundation",
                        "product": "Apache Log4j2",
                        "cpes": ["cpe:2.3:a:apache:log4j:2.15.0:*:*:*:*:*:*:*"],
                        "versions": [
                            {
                                "version": "2.0-beta9",
                                "status": "affected",
                                "lessThan": "log4j-core*",
                                "versionType": "custom",
                                "changes": [
                                    {"at": "2.13.0", "status": "affected"},
                                    {"at": "2.15.0", "status": "unaffected"},
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: _candidates("CVE-2021-44228", payload, quality="cpe"),
        record_loader=lambda **_: {"CVE-2021-44228": _record("CVE-2021-44228", severity="critical", cvss=10.0)},
    )

    response = service.lookup(CveLookupRequest(product="Apache Log4j", version="2.15.0", max_results=5))

    assert response.matches == ()
    assert response.message == "no_cve_matches_after_applicability"


def test_log4shell_2122_is_excluded_as_unaffected_backport() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": "Apache Software Foundation",
                        "product": "Apache Log4j2",
                        "cpes": ["cpe:2.3:a:apache:log4j:2.12.2:*:*:*:*:*:*:*"],
                        "versions": [
                            {
                                "version": "2.0-beta9",
                                "status": "affected",
                                "lessThan": "log4j-core*",
                                "versionType": "custom",
                                "changes": [
                                    {"at": "2.12.2", "status": "unaffected"},
                                    {"at": "2.13.0", "status": "affected"},
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: _candidates("CVE-2021-44228", payload, quality="cpe"),
        record_loader=lambda **_: {"CVE-2021-44228": _record("CVE-2021-44228", severity="critical", cvss=10.0)},
    )

    response = service.lookup(CveLookupRequest(product="Apache Log4j", version="2.12.2", max_results=5))

    assert response.matches == ()


def test_custom_range_rule_is_applicable() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": "Apache Software Foundation",
                        "product": "Apache Log4j",
                        "versions": [
                            {
                                "version": "2.0",
                                "lessThan": "2.16.0",
                                "status": "affected",
                                "versionType": "custom",
                            }
                        ],
                    }
                ]
            }
        }
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: _candidates("CVE-2021-45046", payload, quality="exact"),
        record_loader=lambda **_: {"CVE-2021-45046": _record("CVE-2021-45046", severity="high", cvss=9.0)},
    )

    response = service.lookup(CveLookupRequest(product="Apache Log4j", version="2.15.0", max_results=5))

    assert response.matches
    assert response.matches[0].cve_id == "CVE-2021-45046"
    assert response.matches[0].version_applicable is True


def test_strict_semver_path_still_works() -> None:
    payload = {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": "Acme",
                        "product": "Widget",
                        "versions": [
                            {
                                "version": "1.2.3-beta.1",
                                "lessThan": "1.2.3",
                                "status": "affected",
                                "versionType": "semver",
                            }
                        ],
                    }
                ]
            }
        }
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: _candidates("CVE-2026-7777", payload, quality="exact"),
        record_loader=lambda **_: {"CVE-2026-7777": _record("CVE-2026-7777", severity="medium", cvss=6.4)},
    )

    response = service.lookup(CveLookupRequest(product="Widget", version="1.2.3-beta.2", max_results=5))

    assert response.matches
    assert response.matches[0].cve_id == "CVE-2026-7777"
