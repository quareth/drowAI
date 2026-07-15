"""Tests for deterministic product-version CVE match-service behavior."""

from __future__ import annotations

from backend.models.cve import CveAffectedProduct, CveRecord
from backend.services.cve_indexing.match_contracts import CveLookupRequest
from backend.services.cve_indexing.match_service import CveMatchService
from backend.services.cve_indexing.product_resolver import ResolvedProjectionCandidate


def _projection_row(
    *,
    cve_id: str,
    product_norm: str,
    versions_json: list[dict] | None = None,
    default_status: str | None = "affected",
    cpes_json: list[str] | None = None,
) -> CveAffectedProduct:
    return CveAffectedProduct(
        cve_record_id=1,
        cve_id=cve_id,
        vendor_raw=None,
        vendor_norm=None,
        product_raw=product_norm,
        product_norm=product_norm,
        default_status=default_status,
        versions_json=versions_json,
        cpes_json=cpes_json,
    )


def _candidate(row: CveAffectedProduct, quality: str) -> ResolvedProjectionCandidate:
    return ResolvedProjectionCandidate(row=row, match_quality=quality)  # type: ignore[arg-type]


def _record(cve_id: str, *, title: str, severity: str, metrics: dict | None = None) -> CveRecord:
    return CveRecord(
        cve_id=cve_id,
        source="cvelist_v5",
        record_state="published",
        title=title,
        description=title,
        severity=severity,
        metrics=metrics,
        cve_json={},
    )


def test_lookup_returns_ranked_explainable_match() -> None:
    rows = (
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0001",
                product_norm="openssl",
                versions_json=[{"version": "3.0.13", "status": "affected"}],
            ),
            "exact",
        ),
    )
    service = CveMatchService(
        projection_candidate_loader=lambda **_: rows,
        record_loader=lambda **_: {"CVE-2026-0001": _record("CVE-2026-0001", title="OpenSSL issue", severity="high")},
    )

    response = service.lookup(CveLookupRequest(product="OpenSSL", version="3.0.13", max_results=5))

    assert len(response.matches) == 1
    assert response.matches[0].cve_id == "CVE-2026-0001"
    assert response.matches[0].rationale == "product exact match; version exact match"
    assert response.matches[0].summary == "OpenSSL issue"
    assert response.matches[0].version_applicable is True
    assert "product_exact_match" in response.matches[0].matched_fields
    assert response.message == "ok"


def test_lookup_ranks_applicable_before_possible_with_deterministic_tie_break() -> None:
    rows = (
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0010",
                product_norm="openssl",
                versions_json=[{"version": "3.0.13", "status": "affected"}],
            ),
            "exact",
        ),
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0011",
                product_norm="openssl",
                versions_json=[{"version": "1.*", "status": "affected", "versionType": "rpm"}],
            ),
            "exact",
        ),
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0012",
                product_norm="openssl",
                versions_json=[{"version": "3.0.13", "status": "affected"}],
            ),
            "token",
        ),
    )
    records = {
        "CVE-2026-0010": _record("CVE-2026-0010", title="first", severity="high"),
        "CVE-2026-0011": _record("CVE-2026-0011", title="possible", severity="critical"),
        "CVE-2026-0012": _record("CVE-2026-0012", title="second", severity="low"),
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: rows,
        record_loader=lambda **_: records,
    )
    request = CveLookupRequest(product="openssl", version="3.0.13", max_results=10)

    first = service.lookup(request)
    second = service.lookup(request)

    assert [item.cve_id for item in first.matches] == [item.cve_id for item in second.matches]
    assert [item.cve_id for item in first.matches] == ["CVE-2026-0010", "CVE-2026-0012", "CVE-2026-0011"]
    assert first.matches[0].score > first.matches[-1].score
    assert first.matches[-1].rationale == "product exact match; version rule unsupported by MVP evaluator"


def test_lookup_returns_no_matches_when_all_candidates_outside_version_rules() -> None:
    service = CveMatchService(
        projection_candidate_loader=lambda **_: (
            _candidate(
                _projection_row(
                    cve_id="CVE-2026-0099",
                    product_norm="apache",
                    versions_json=[{"version": "2.0.0", "status": "affected", "lessThan": "2.1.0"}],
                ),
                "exact",
            ),
        ),
        record_loader=lambda **_: {"CVE-2026-0099": _record("CVE-2026-0099", title="apache", severity="high")},
    )

    response = service.lookup(CveLookupRequest(product="apache", version="3.0.13"))

    assert response.matches == ()
    assert response.message == "no_cve_matches_after_applicability"


def test_lookup_uses_metrics_entries_shape_for_cvss_tie_break() -> None:
    rows = (
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0350",
                product_norm="openssl",
                versions_json=[{"version": "3.0.13", "status": "affected"}],
            ),
            "exact",
        ),
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0351",
                product_norm="openssl",
                versions_json=[{"version": "3.0.13", "status": "affected"}],
            ),
            "exact",
        ),
    )
    records = {
        "CVE-2026-0350": _record(
            "CVE-2026-0350",
            title="higher cvss",
            severity="high",
            metrics={"entries": [{"cvssV3_1": {"baseScore": 9.8}}]},
        ),
        "CVE-2026-0351": _record(
            "CVE-2026-0351",
            title="lower cvss",
            severity="high",
            metrics={"entries": [{"cvssV3_1": {"baseScore": 7.1}}]},
        ),
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: rows,
        record_loader=lambda **_: records,
    )

    response = service.lookup(CveLookupRequest(product="openssl", version="3.0.13"))

    assert [item.cve_id for item in response.matches] == ["CVE-2026-0350", "CVE-2026-0351"]


def test_lookup_emits_metrics_for_match_outcomes(monkeypatch) -> None:
    metric_calls: list[tuple[str, int]] = []

    def _fake_inc(name: str, value: int = 1) -> None:
        metric_calls.append((name, int(value)))

    monkeypatch.setattr("backend.services.cve_indexing.match_service.safe_inc", _fake_inc)
    rows = (
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0400",
                product_norm="openssl",
                versions_json=[{"version": "3.0.13", "status": "affected"}],
            ),
            "exact",
        ),
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0401",
                product_norm="openssl",
                versions_json=[{"version": "1.*", "versionType": "rpm", "status": "affected"}],
            ),
            "token",
        ),
    )
    records = {
        "CVE-2026-0400": _record("CVE-2026-0400", title="exact", severity="high"),
        "CVE-2026-0401": _record("CVE-2026-0401", title="possible", severity="low"),
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: rows,
        record_loader=lambda **_: records,
    )

    response = service.lookup(CveLookupRequest(product="openssl", version="3.0.13", max_results=5))

    assert len(response.matches) == 2
    assert ("cve.lookup.requests_total", 1) in metric_calls
    assert ("cve.lookup.matches_returned_total", 2) in metric_calls
    assert ("cve.lookup.applicable_total", 1) in metric_calls
    assert ("cve.lookup.possible_total", 1) in metric_calls


def test_scores_are_differentiated_by_match_quality() -> None:
    rows = (
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0600",
                product_norm="apache log4j",
                versions_json=[{"version": "2.14.1", "status": "affected"}],
            ),
            "exact",
        ),
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0601",
                product_norm="apache log4j2",
                versions_json=[{"version": "2.14.1", "status": "affected"}],
                cpes_json=["cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*"],
            ),
            "cpe",
        ),
        _candidate(
            _projection_row(
                cve_id="CVE-2026-0602",
                product_norm="apache logging framework",
                versions_json=[{"version": "2.14.1", "status": "affected"}],
            ),
            "token",
        ),
    )
    records = {
        "CVE-2026-0600": _record("CVE-2026-0600", title="exact", severity="high"),
        "CVE-2026-0601": _record("CVE-2026-0601", title="cpe", severity="high"),
        "CVE-2026-0602": _record("CVE-2026-0602", title="token", severity="high"),
    }
    service = CveMatchService(
        projection_candidate_loader=lambda **_: rows,
        record_loader=lambda **_: records,
    )

    response = service.lookup(CveLookupRequest(product="Apache Log4j", version="2.14.1", max_results=10))
    by_id = {item.cve_id: item.score for item in response.matches}

    assert by_id["CVE-2026-0600"] > by_id["CVE-2026-0601"] > by_id["CVE-2026-0602"]
