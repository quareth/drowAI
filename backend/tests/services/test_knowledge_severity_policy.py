"""Tests for centralized durable knowledge severity policy."""

from __future__ import annotations

from backend.services.knowledge.severity_policy import (
    ALLOWED_FINDING_SEVERITIES,
    normalize_severity,
    resolve_finding_severity,
    severity_bucket_template,
    severity_from_cvss,
    severity_sort_score,
)


def test_explicit_valid_severity_normalizes_and_wins() -> None:
    resolution = resolve_finding_severity(
        observation_type="finding.vulnerability_detected",
        payload={
            "severity": " HIGH ",
            "cvss_score": 2.0,
            "finding_subtype": "secret_exposure_detected",
        },
    )

    assert resolution is not None
    assert resolution.severity == "high"
    assert resolution.source == "explicit"
    assert resolution.signal == "payload.severity"


def test_invalid_explicit_severity_is_ignored_for_other_structured_signals() -> None:
    resolution = resolve_finding_severity(
        observation_type="finding.vulnerability_detected",
        payload={"severity": "important", "cvss_score": "9.8"},
    )

    assert resolution is not None
    assert resolution.severity == "critical"
    assert resolution.source == "numeric_score"
    assert resolution.signal == "payload.cvss_score"


def test_cvss_thresholds_map_to_canonical_severities() -> None:
    assert severity_from_cvss(9.0) == "critical"
    assert severity_from_cvss("7.0") == "high"
    assert severity_from_cvss(4.0) == "medium"
    assert severity_from_cvss(0.1) == "low"
    assert severity_from_cvss(0) == "info"
    assert severity_from_cvss("not-a-score") is None


def test_structured_policy_signals_resolve_without_titles() -> None:
    exploit = resolve_finding_severity(
        observation_type="finding.exploit_succeeded",
        payload={"detector_id": "exploit/unix/webapp/drupal_drupalgeddon2"},
    )
    credential = resolve_finding_severity(
        observation_type="finding.vulnerability_confirmed",
        payload={"finding_subtype": "credential_compromise_confirmed"},
    )
    secret = resolve_finding_severity(
        observation_type="finding.vulnerability_detected",
        payload={"finding_subtype": "secret_exposure_detected"},
    )

    assert exploit is not None
    assert exploit.severity == "high"
    assert exploit.signal == "observation_type:finding.exploit_succeeded"
    assert credential is not None
    assert credential.severity == "high"
    assert secret is not None
    assert secret.severity == "medium"


def test_unknown_or_no_signal_findings_do_not_resolve_severity() -> None:
    assert normalize_severity("unknown") is None
    assert resolve_finding_severity(
        observation_type="finding.vulnerability_detected",
        payload={"detector_id": "custom/no-severity"},
    ) is None


def test_rank_and_bucket_helpers_match_query_contract() -> None:
    assert ALLOWED_FINDING_SEVERITIES == frozenset({"critical", "high", "medium", "low", "info"})
    assert severity_bucket_template() == {
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "info": 0,
    }
    assert severity_sort_score("critical") > severity_sort_score("high")
    assert severity_sort_score("info") > severity_sort_score(None)
