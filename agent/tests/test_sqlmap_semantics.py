"""Regression tests for producer-owned SQLMap finding semantics.

These tests lock SQLMap semantic emission over parsed vulnerability metadata
only. They intentionally avoid command execution, raw text parsing, artifact
reads, and backend adapter behavior.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import (
    SQLMAP_CAPABILITY_FAMILY,
    SQLMAP_SEMANTIC_SCHEMA_VERSION,
    SqlmapArgs,
    SqlmapTool,
    build_sqlmap_semantic_observations,
    parse_sqlmap_text,
)
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope, compile_facts
from tests.tools.fixtures.output_fixtures import load_output_fixture


def _confirmed_row(parameter: str, injection_type: str) -> dict[str, object]:
    return {
        "type": injection_type,
        "parameter": parameter,
        "payload": "1 AND 1=1",
        "raw": {
            "type": injection_type,
            "parameter": parameter,
            "payload": "1 AND 1=1",
            "value": "true",
        },
    }


def test_sqlmap_explicit_injection_metadata_emits_canonical_confirmed_finding() -> None:
    args = SqlmapArgs(target="HTTPS://Example.com:443/item?id=1")

    observations = build_sqlmap_semantic_observations(
        {"vulnerabilities": [_confirmed_row("id", "boolean-based blind")]},
        args,
    )

    assert observations == [
        {
            "observation_type": "finding.vulnerability_confirmed",
            "subject_type": "finding.instance",
            "subject_key": (
                "finding.instance:sqlmap:https://example.com/item:"
                "param-id:variant-boolean-based-blind"
            ),
            "payload": {
                "source": "sqlmap",
                "detector_id": "sqlmap",
                "target_url": "https://example.com/item",
                "parameter": "id",
                "injection_type": "boolean-based-blind",
                "confidence": "confirmed",
            },
        }
    ]


def test_sqlmap_tool_emits_from_parsed_metadata_without_raw_output() -> None:
    tool = SqlmapTool()
    args = SqlmapArgs(target="https://example.com/item?id=1")

    observations = tool.emit_semantic_observations(
        stdout=(
            "sqlmap identified the following injection point:\n"
            "Parameter: ignored (GET)\n"
            "  Type: boolean-based blind"
        ),
        stderr="",
        exit_code=0,
        args=args,
        metadata={"vulnerabilities": [_confirmed_row("metadata", "error-based")]},
    )

    assert observations[0]["subject_key"] == (
        "finding.instance:sqlmap:https://example.com/item:"
        "param-metadata:variant-error-based"
    )


def test_sqlmap_locked_fixture_parse_output_emits_confirmed_injection_blocks() -> None:
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com/vuln.php?id=1")
    metadata = tool.parse_output(
        load_output_fixture("web_applications.web_vulnerability_scanners.sqlmap"),
        "",
        0,
        args,
    )

    observations = tool.emit_semantic_observations("", "", 0, args, metadata)
    subject_keys = [str(item["subject_key"]) for item in observations]

    assert len(subject_keys) >= 3
    assert all(":param-id:" in subject_key for subject_key in subject_keys)
    assert any("boolean-based-blind" in subject_key for subject_key in subject_keys)
    assert metadata["semantic_schema_version"] == SQLMAP_SEMANTIC_SCHEMA_VERSION
    assert metadata["capability_family"] == SQLMAP_CAPABILITY_FAMILY


def test_sqlmap_scan_completion_warnings_heuristics_and_partial_rows_emit_no_findings() -> None:
    args = SqlmapArgs(target="https://example.com/item?id=1")
    text_metadata = parse_sqlmap_text(
        "[INFO] testing connection to the target URL\n"
        "[WARNING] heuristic test shows that GET parameter 'id' might be injectable\n"
        "sqlmap identified possible SQL injection vulnerable behavior\n"
    )
    partial_rows = [
        {"parameter": "id", "type": "boolean-based blind"},
        {"parameter": "id", "type": "boolean-based blind", "raw": {"value": False}},
        {"parameter": "", "type": "boolean-based blind", "raw": {"value": True}},
        {"parameter": "id", "raw": {"value": True}},
        "bad-row",
    ]

    assert build_sqlmap_semantic_observations(text_metadata, args) == []
    assert build_sqlmap_semantic_observations({"vulnerabilities": partial_rows}, args) == []
    assert (
        build_sqlmap_semantic_observations(
            {"vulnerabilities": [_confirmed_row("id", "boolean")]},
            SimpleNamespace(target=""),
        )
        == []
    )
    assert build_sqlmap_semantic_observations({"vulnerabilities": []}, args) == []


def test_sqlmap_multiple_parameters_and_techniques_keep_order_and_dedupe() -> None:
    args = SqlmapArgs(target="https://example.com/item?id=1&name=a")
    first = _confirmed_row("id", "boolean-based blind")
    duplicate = _confirmed_row("id", "boolean-based blind")
    second = _confirmed_row("id", "time-based blind")
    third = _confirmed_row("name", "error-based")

    observations = build_sqlmap_semantic_observations(
        {"vulnerabilities": [first, duplicate, second, third]},
        args,
    )

    assert [item["subject_key"] for item in observations] == [
        "finding.instance:sqlmap:https://example.com/item:param-id:variant-boolean-based-blind",
        "finding.instance:sqlmap:https://example.com/item:param-id:variant-time-based-blind",
        "finding.instance:sqlmap:https://example.com/item:param-name:variant-error-based",
    ]


def test_sqlmap_secret_bearing_inputs_are_not_leaked_in_semantic_rows() -> None:
    args = SqlmapArgs(
        target="https://example.com/item?id=1",
        data="username=alice&password=super-secret",
        cookies="sessionid=super-secret-cookie",
        headers="Authorization: Bearer super-secret-token",
        auth_cred="alice:super-secret-basic",
    )
    row = _confirmed_row("id", "boolean-based blind")
    row["payload"] = "password=super-secret"
    row["raw"]["payload"] = "password=super-secret"

    observations = build_sqlmap_semantic_observations({"vulnerabilities": [row]}, args)
    encoded = json.dumps(observations, sort_keys=True)

    assert "super-secret" not in encoded
    assert "super-secret-cookie" not in encoded
    assert "super-secret-token" not in encoded
    assert "super-secret-basic" not in encoded


def test_sqlmap_semantic_observations_compile_directly_as_confirmed_findings() -> None:
    args = SqlmapArgs(target="https://example.com/item?id=1")
    observations = build_sqlmap_semantic_observations(
        {"vulnerabilities": [_confirmed_row("id", "boolean-based blind")]},
        args,
    )

    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version=SQLMAP_SEMANTIC_SCHEMA_VERSION,
            capability_family=SQLMAP_CAPABILITY_FAMILY,
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert compiled.accepted_count == 1
    assert compiled.rejected_count == 0
    assert compiled.diagnostics == ()
