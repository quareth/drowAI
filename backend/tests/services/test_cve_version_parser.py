"""Tests for lenient CVE version parsing and comparison helpers."""

from __future__ import annotations

from backend.services.cve_indexing.version_parser import compare_versions, parse_version


def test_compare_versions_matches_semver_behavior_for_strict_values() -> None:
    assert compare_versions("1.2.3", "1.2.4") == -1
    assert compare_versions("1.2.3", "1.2.3") == 0
    assert compare_versions("1.2.4", "1.2.3") == 1


def test_parse_version_supports_two_segment_prerelease() -> None:
    parsed = parse_version("2.0-beta9")

    assert parsed is not None
    assert parsed.core == (2, 0)
    assert parsed.prerelease == ("beta9",)


def test_compare_versions_handles_log4j_style_values() -> None:
    assert compare_versions("2.14.1", "2.15.0") == -1
    assert compare_versions("2.0-beta9", "2.14.1") == -1


def test_compare_versions_treats_two_segment_and_three_segment_as_equal() -> None:
    assert compare_versions("2.0", "2.0.0") == 0
    assert compare_versions("2.14", "2.14.0") == 0


def test_parse_version_returns_none_for_unparseable_markers() -> None:
    assert parse_version("log4j-core*") is None
    assert parse_version("*") is None
    assert parse_version("n/a") is None


def test_compare_versions_returns_none_when_one_side_is_unparseable() -> None:
    assert compare_versions("2.14.1", "log4j-core*") is None
    assert compare_versions("junk", "2.14.1") is None
