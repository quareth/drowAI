"""Tests for the task closure memo structured-output schema contract."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from agent.providers.llm.core.base import StructuredOutputSpec
from core.llm.structured_schemas import TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT


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


def test_task_closure_memo_spec_name_and_required_fields() -> None:
    spec = TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT

    assert isinstance(spec, StructuredOutputSpec)
    assert spec.name == "task_closure_memo"
    assert spec.strict is True
    assert set(spec.schema["required"]) == {
        "task_name",
        "summary",
        "include_in_report_recommendation",
        "actions_performed",
        "reportable_observations",
        "possible_findings",
        "limitations",
        "unsupported_notes",
        "evidence_refs",
        "knowledge_refs",
    }


def test_task_closure_memo_reportable_items_include_source_ref_arrays() -> None:
    properties = TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT.schema["properties"]

    observation_item = properties["reportable_observations"]["items"]
    finding_item = properties["possible_findings"]["items"]

    for item in (observation_item, finding_item):
        assert item["properties"]["evidence_refs"]["type"] == "array"
        assert item["properties"]["knowledge_refs"]["type"] == "array"
        assert "evidence_refs" in item["required"]
        assert "knowledge_refs" in item["required"]
        assert item["anyOf"][0]["properties"]["evidence_refs"]["minItems"] == 1
        assert item["anyOf"][1]["properties"]["knowledge_refs"]["minItems"] == 1


def test_task_closure_memo_schema_closes_all_object_levels() -> None:
    object_schemas = list(
        _iter_object_schemas(TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT.schema)
    )

    assert object_schemas
    assert all(schema.get("additionalProperties") is False for schema in object_schemas)
