"""Tests for conservative CVE version applicability evaluation helpers."""

from __future__ import annotations

from backend.services.cve_indexing.version_eval import evaluate_version_applicability


def test_exact_affected_version_is_applicable() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="1.2.3",
        versions_json=[{"version": "1.2.3", "status": "affected"}],
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"


def test_missing_version_is_possible() -> None:
    result = evaluate_version_applicability(
        fingerprint_version=None,
        versions_json=[{"version": "1.2.3", "status": "affected"}],
        default_status="affected",
    )

    assert result.status == "possible"
    assert result.explanation == "product match; version missing"


def test_version_outside_supported_range_is_no_match() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.1.0",
        versions_json=[{"version": "1.0.0", "lessThan": "2.0.0", "status": "affected"}],
        default_status="affected",
    )

    assert result.status == "no_match"
    assert result.explanation == "product match; version outside affected rules"


def test_unsupported_expression_degrades_to_possible() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="1.2.3",
        versions_json=[{"version": "1.*", "status": "affected", "versionType": "rpm"}],
        default_status="affected",
    )

    assert result.status == "possible"
    assert result.explanation == "product match; version rule unsupported by MVP evaluator"


def test_matching_unaffected_rule_is_no_match() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="3.0.13",
        versions_json=[{"version": "3.0.13", "status": "unaffected"}],
        default_status="affected",
    )

    assert result.status == "no_match"
    assert result.explanation == "product match; version exact unaffected"


def test_prerelease_does_not_collapse_to_release_exact_match() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="1.2.3-beta.1",
        versions_json=[{"version": "1.2.3", "status": "affected", "versionType": "semver"}],
        default_status="affected",
    )

    assert result.status == "no_match"
    assert result.explanation == "product match; version outside affected rules"


def test_supported_semver_prerelease_range_is_applicable() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="1.2.3-beta.2",
        versions_json=[
            {
                "version": "1.2.3-beta.1",
                "lessThan": "1.2.3",
                "status": "affected",
                "versionType": "semver",
            }
        ],
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"


def test_custom_version_type_is_evaluated() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.14.1",
        versions_json=[
            {
                "version": "2.0",
                "lessThan": "2.15.0",
                "status": "affected",
                "versionType": "custom",
            }
        ],
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"


def test_changes_array_affected_version_is_applicable() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.14.1",
        versions_json=[
            {
                "version": "2.0-beta9",
                "lessThan": "log4j-core*",
                "status": "affected",
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
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"


def test_changes_array_unaffected_version_is_no_match() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.15.0",
        versions_json=[
            {
                "version": "2.0-beta9",
                "lessThan": "log4j-core*",
                "status": "affected",
                "versionType": "custom",
                "changes": [
                    {"at": "2.4", "status": "affected"},
                    {"at": "2.15.0", "status": "unaffected"},
                ],
            }
        ],
        default_status="affected",
    )

    assert result.status == "no_match"
    assert result.explanation == "product match; version outside affected rules"


def test_changes_array_re_affected_after_unaffected() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.13.0",
        versions_json=[
            {
                "version": "2.0-beta9",
                "lessThan": "log4j-core*",
                "status": "affected",
                "versionType": "custom",
                "changes": [
                    {"at": "2.12.2", "status": "unaffected"},
                    {"at": "2.13.0", "status": "affected"},
                ],
            }
        ],
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"


def test_unparseable_less_than_with_changes_uses_changes() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.14.1",
        versions_json=[
            {
                "version": "2.0",
                "lessThan": "log4j-core*",
                "status": "affected",
                "versionType": "custom",
                "changes": [
                    {"at": "2.15.0", "status": "unaffected"},
                ],
            }
        ],
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"


def test_unparseable_less_than_without_changes_is_possible() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.14.1",
        versions_json=[
            {
                "version": "2.0",
                "lessThan": "junk",
                "status": "affected",
                "versionType": "custom",
            }
        ],
        default_status="affected",
    )

    assert result.status == "possible"
    assert result.explanation == "product match; version rule unsupported by MVP evaluator"


def test_two_segment_version_in_range() -> None:
    result = evaluate_version_applicability(
        fingerprint_version="2.14",
        versions_json=[
            {
                "version": "2.0",
                "lessThan": "2.16.0",
                "status": "affected",
                "versionType": "custom",
            }
        ],
        default_status="affected",
    )

    assert result.status == "applicable"
    assert result.explanation == "product match; version affected"
