"""Tests for the engagement report section structured-output schema."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agent.providers.llm.core.base import StructuredOutputSpec
from backend.services.reporting import contracts
from core.llm.structured_schemas import ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT


def _iter_object_schemas(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if schema.get("type") == "object" or (
        isinstance(schema.get("type"), list) and "object" in schema["type"]
    ):
        yield schema

    for value in schema.get("properties", {}).values():
        if isinstance(value, dict):
            yield from _iter_object_schemas(value)

    items = schema.get("items")
    if isinstance(items, dict):
        yield from _iter_object_schemas(items)

    for option in schema.get("anyOf", []):
        if isinstance(option, dict):
            yield from _iter_object_schemas(option)


def _iter_property_names(schema: dict[str, Any]) -> Iterator[str]:
    for name, value in schema.get("properties", {}).items():
        yield name
        if isinstance(value, dict):
            yield from _iter_property_names(value)

    items = schema.get("items")
    if isinstance(items, dict):
        yield from _iter_property_names(items)

    for option in schema.get("anyOf", []):
        if isinstance(option, dict):
            yield from _iter_property_names(option)


def _validator() -> Draft202012Validator:
    return Draft202012Validator(ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT.schema)


def _valid_section_payload() -> dict[str, Any]:
    return {
        "schema_version": contracts.REPORT_SECTION_SCHEMA_VERSION,
        "section_id": "findings",
        "section_type": "findings",
        "title": "Findings",
        "status": "ready",
        "content_markdown": "The validated findings are listed below.",
        "blocks": [
            {
                "block_id": "finding-1",
                "block_type": "finding",
                "title": "Weak TLS configuration",
                "severity": "medium",
                "confidence": "high",
                "affected_assets": ["app.example.test"],
                "content_markdown": "TLS configuration allows weak ciphers.",
                "impact_markdown": "An attacker may downgrade transport security.",
                "remediation_markdown": "Disable weak ciphers and redeploy.",
                "source_refs": {
                    "task_memo_ids": ["memo-1"],
                    "knowledge_refs": [],
                    "evidence_refs": ["evidence-1"],
                },
            }
        ],
        "source_refs": {
            "task_memo_ids": ["memo-1"],
            "knowledge_refs": [],
            "evidence_refs": ["evidence-1"],
        },
        "unsupported_notes": [],
        "generation_notes": [],
    }


def test_engagement_report_section_spec_name_and_required_envelope() -> None:
    spec = ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT

    assert isinstance(spec, StructuredOutputSpec)
    assert spec.name == "engagement_report_section"
    assert spec.strict is True
    assert set(spec.schema["required"]) == {
        "schema_version",
        "section_id",
        "section_type",
        "title",
        "status",
        "content_markdown",
        "blocks",
        "source_refs",
        "unsupported_notes",
        "generation_notes",
    }


def test_engagement_report_section_schema_enums_match_reporting_contracts() -> None:
    properties = ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT.schema["properties"]
    block_properties = properties["blocks"]["items"]["properties"]

    assert properties["schema_version"]["enum"] == [
        contracts.REPORT_SECTION_SCHEMA_VERSION
    ]
    assert tuple(properties["section_type"]["enum"]) == contracts.REPORT_SECTION_TYPES
    assert tuple(properties["status"]["enum"]) == contracts.REPORT_SECTION_STATUSES
    assert tuple(block_properties["block_type"]["enum"]) == (
        contracts.REPORT_SECTION_BLOCK_TYPES
    )
    assert "needs_review" in properties["status"]["enum"]
    assert contracts.CURRENT_REPORT_SECTION_STATUSES == ("ready",)


def test_engagement_report_section_schema_closes_all_object_levels() -> None:
    object_schemas = list(
        _iter_object_schemas(ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT.schema)
    )

    assert object_schemas
    assert all(schema.get("additionalProperties") is False for schema in object_schemas)


def test_engagement_report_section_schema_accepts_valid_section() -> None:
    _validator().validate(_valid_section_payload())


def test_engagement_report_section_schema_rejects_missing_source_refs() -> None:
    payload = _valid_section_payload()
    payload.pop("source_refs")

    with pytest.raises(ValidationError):
        _validator().validate(payload)


def test_engagement_report_section_schema_rejects_unknown_block_type() -> None:
    payload = _valid_section_payload()
    payload["blocks"][0]["block_type"] = "unsupported"

    with pytest.raises(ValidationError):
        _validator().validate(payload)


def test_engagement_report_section_schema_requires_finding_reportable_refs() -> None:
    payload = _valid_section_payload()
    payload["blocks"][0]["source_refs"]["evidence_refs"] = []

    with pytest.raises(ValidationError):
        _validator().validate(payload)


def test_engagement_report_section_schema_accepts_parser_review_status() -> None:
    payload = _valid_section_payload()
    payload["status"] = "needs_review"

    _validator().validate(payload)
    assert "needs_review" not in contracts.CURRENT_REPORT_SECTION_STATUSES


def test_engagement_report_section_schema_limits_markdown_fields() -> None:
    markdown_fields = {
        name
        for name in _iter_property_names(ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT.schema)
        if "markdown" in name
    }

    assert markdown_fields == {
        "content_markdown",
        "impact_markdown",
        "remediation_markdown",
    }


def test_engagement_report_section_schema_rejects_unknown_properties() -> None:
    payload = deepcopy(_valid_section_payload())
    payload["extra"] = "unexpected"

    with pytest.raises(ValidationError):
        _validator().validate(payload)
