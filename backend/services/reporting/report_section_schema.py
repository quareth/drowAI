"""Own the pure JSON schema for generated engagement report sections."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.services.reporting.contracts import (
    REPORT_SECTION_BLOCK_TYPES,
    REPORT_SECTION_SCHEMA_VERSION,
    REPORT_SECTION_STATUSES,
    REPORT_SECTION_TYPES,
)


def engagement_report_section_json_schema() -> dict[str, Any]:
    """Return a fresh JSON schema dict for one generated report section."""

    return deepcopy(ENGAGEMENT_REPORT_SECTION_JSON_SCHEMA)


def _ref_array_schema(*, max_items: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 256},
        "maxItems": max_items,
    }


def _source_refs_schema(*, require_reportable_ref: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "task_memo_ids": _ref_array_schema(max_items=100),
        "knowledge_refs": _ref_array_schema(max_items=500),
        "evidence_refs": _ref_array_schema(max_items=500),
    }
    required = ["task_memo_ids", "knowledge_refs", "evidence_refs"]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if require_reportable_ref:
        schema["anyOf"] = [
            {
                "type": "object",
                "properties": {
                    **properties,
                    "knowledge_refs": {**_ref_array_schema(max_items=500), "minItems": 1},
                },
                "required": required,
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    **properties,
                    "evidence_refs": {**_ref_array_schema(max_items=500), "minItems": 1},
                },
                "required": required,
                "additionalProperties": False,
            },
        ]
    return schema


def _block_schema() -> dict[str, Any]:
    block_properties: dict[str, Any] = {
        "block_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "block_type": {"type": "string", "enum": list(REPORT_SECTION_BLOCK_TYPES)},
        "title": {"type": "string", "minLength": 1, "maxLength": 512},
        "severity": {
            "type": ["string", "null"],
            "enum": ["informational", "low", "medium", "high", "critical", None],
        },
        "confidence": {
            "type": ["string", "null"],
            "enum": ["low", "medium", "high", None],
        },
        "affected_assets": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 512},
            "maxItems": 100,
        },
        "content_markdown": {"type": "string", "minLength": 1, "maxLength": 20000},
        "impact_markdown": {"type": "string", "minLength": 1, "maxLength": 20000},
        "remediation_markdown": {
            "type": "string",
            "minLength": 1,
            "maxLength": 20000,
        },
        "source_refs": _source_refs_schema(),
    }
    required = [
        "block_id",
        "block_type",
        "title",
        "severity",
        "confidence",
        "affected_assets",
        "content_markdown",
        "impact_markdown",
        "remediation_markdown",
        "source_refs",
    ]
    return {
        "type": "object",
        "properties": block_properties,
        "required": required,
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    **block_properties,
                    "block_type": {"type": "string", "enum": ["finding"]},
                    "source_refs": _source_refs_schema(require_reportable_ref=True),
                },
                "required": required,
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    **block_properties,
                    "block_type": {
                        "type": "string",
                        "enum": ["evidence_note", "asset_note", "appendix_note"],
                    },
                },
                "required": required,
                "additionalProperties": False,
            },
        ],
        "additionalProperties": False,
    }


ENGAGEMENT_REPORT_SECTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "enum": [REPORT_SECTION_SCHEMA_VERSION]},
        "section_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "section_type": {"type": "string", "enum": list(REPORT_SECTION_TYPES)},
        "title": {"type": "string", "minLength": 1, "maxLength": 512},
        "status": {"type": "string", "enum": list(REPORT_SECTION_STATUSES)},
        "content_markdown": {"type": "string", "maxLength": 50000},
        "blocks": {"type": "array", "items": _block_schema(), "maxItems": 100},
        "source_refs": _source_refs_schema(),
        "unsupported_notes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            "maxItems": 100,
        },
        "generation_notes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            "maxItems": 100,
        },
    },
    "required": [
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
    ],
    "additionalProperties": False,
}


__all__ = [
    "ENGAGEMENT_REPORT_SECTION_JSON_SCHEMA",
    "engagement_report_section_json_schema",
]
