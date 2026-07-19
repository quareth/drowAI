"""Regression tests for strict structured-output schema compatibility."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from jsonschema import ValidationError, validate

from agent.providers.llm.contracts.structured_output import validate_openai_strict_schema
from core.llm.structured_schemas import (
    DECISION_ROUTER_STRUCTURED_OUTPUT,
    ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT,
    GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT,
    INTENT_CLASSIFIER_STRUCTURED_OUTPUT,
    MEMORY_EXTRACTION_STRUCTURED_OUTPUT,
    MEMORY_GATE_STRUCTURED_OUTPUT,
    PLANNER_CONTRACT_STRUCTURED_OUTPUT,
    POST_TOOL_DECISION_STRUCTURED_OUTPUT,
    REFLECT_STRUCTURED_OUTPUT,
    TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT,
    THINK_MORE_STRUCTURED_OUTPUT,
    TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT,
    TOOL_SELECTOR_STRUCTURED_OUTPUT,
    TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT,
)


def _collect_required_coverage_errors(schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Return validation errors where object.properties are not fully listed in required."""
    errors: List[str] = []

    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            required = schema.get("required")
            if not isinstance(required, list):
                errors.append(f"{path}: required missing or not a list")
            else:
                missing = sorted(set(properties.keys()) - set(required))
                if missing:
                    errors.append(f"{path}: required missing keys {missing}")
            for key, child in properties.items():
                errors.extend(_collect_required_coverage_errors(child, f"{path}.properties.{key}"))

        items = schema.get("items")
        if isinstance(items, dict):
            errors.extend(_collect_required_coverage_errors(items, f"{path}.items"))

        for key in ("anyOf", "allOf", "oneOf"):
            variants = schema.get(key)
            if isinstance(variants, list):
                for index, variant in enumerate(variants):
                    if isinstance(variant, dict):
                        errors.extend(
                            _collect_required_coverage_errors(variant, f"{path}.{key}[{index}]")
                        )

    return errors


def test_structured_schemas_have_required_for_all_properties() -> None:
    specs = [
        INTENT_CLASSIFIER_STRUCTURED_OUTPUT,
        TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT,
        TOOL_SELECTOR_STRUCTURED_OUTPUT,
        PLANNER_CONTRACT_STRUCTURED_OUTPUT,
        DECISION_ROUTER_STRUCTURED_OUTPUT,
        THINK_MORE_STRUCTURED_OUTPUT,
        POST_TOOL_DECISION_STRUCTURED_OUTPUT,
        REFLECT_STRUCTURED_OUTPUT,
        TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT,
        GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT,
        MEMORY_GATE_STRUCTURED_OUTPUT,
        MEMORY_EXTRACTION_STRUCTURED_OUTPUT,
        TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT,
        ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT,
    ]
    errors: List[str] = []
    for spec in specs:
        errors.extend(_collect_required_coverage_errors(spec.schema, path=spec.name))
        validate_openai_strict_schema(spec)
    assert not errors, "\n".join(errors)


def test_intent_classifier_turn_interpretation_requires_goal_and_task_seed() -> None:
    schema = INTENT_CLASSIFIER_STRUCTURED_OUTPUT.schema
    turn_interpretation = schema["properties"]["turn_interpretation"]

    assert turn_interpretation["properties"]["original_goal"] == {
        "type": ["string", "null"],
    }
    assert "original_goal" in turn_interpretation["required"]
    assert turn_interpretation["properties"]["task_seed"] == {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 3,
    }
    assert "task_seed" in turn_interpretation["required"]

    base_payload = {
        "label": "direct_executor",
        "confidence": 0.95,
        "suggested_capabilities": ["network_scan"],
        "requested_output_format": None,
        "question_type": "multi_step",
        "answer_style": "normal",
        "terminal_when": "all_steps_done",
        "risk_flags": [],
        "target_status": "resolved",
        "resolved_target": "10.0.0.5",
        "target_source": "explicit_current_message",
        "target_confidence": 0.9,
        "target_evidence": "User supplied 10.0.0.5.",
        "prior_target_reuse": "disallow",
        "prior_target_reuse_evidence": None,
        "turn_interpretation": {
            "resolved_user_intent": "Scan 10.0.0.5 for open services.",
            "original_goal": "Scan 10.0.0.5 for open services.",
            "task_seed": ["Scan 10.0.0.5 for open services."],
            "overall_goal": None,
            "continuation_mode": "new_request",
            "step_reference_text": None,
            "step_reference_status": "none",
            "resolved_step_title": None,
            "resolved_step_detail": None,
            "next_operational_goal": "Run a port scan against 10.0.0.5.",
            "execution_readiness": "ready",
            "blocking_reason": None,
            "success_condition": "Open services are identified.",
            "explicit_constraints": [],
            "relevant_memory_fragments": [],
            "suggested_category_focus": ["network_scan"],
            "retrieval_hints": [],
        },
        "prior_turn_reference": {
            "required": False,
            "operation": "none",
            "status": "none",
            "confidence": None,
            "hints": [],
        },
        "reasoning": "The request is grounded and actionable.",
    }

    validate(base_payload, schema)
    base_payload["turn_interpretation"]["original_goal"] = None
    base_payload["turn_interpretation"]["task_seed"] = []
    validate(base_payload, schema)
    base_payload["turn_interpretation"]["task_seed"] = [
        "Discover live hosts",
        "Choose one online host",
        "Scan PostgreSQL exposure",
    ]
    validate(base_payload, schema)
    base_payload["turn_interpretation"]["task_seed"] = [
        "one",
        "two",
        "three",
        "four",
    ]
    with pytest.raises(ValidationError):
        validate(base_payload, schema)


def test_tool_selector_schema_requires_candidate_tools_strategy_and_reasoning() -> None:
    schema = TOOL_SELECTOR_STRUCTURED_OUTPUT.schema

    assert schema["properties"]["selected_tools"]["minItems"] == 1
    assert schema["properties"]["execution_strategy"]["enum"] == [
        "sequential",
        "parallel",
    ]
    assert schema["properties"]["reasoning"] == {"type": "string"}
    assert schema["required"] == ["selected_tools", "execution_strategy", "reasoning"]

    validate(
        {
            "selected_tools": ["shell.exec"],
            "execution_strategy": "parallel",
            "reasoning": "shell.exec is the only listed tool that can run commands.",
        },
        schema,
    )


def test_tool_output_compressor_schema_validates_success_payload() -> None:
    payload = {
        "summary": "HTTP 200 response returned a Gunicorn-served HTML Security Dashboard page.",
        "key_findings": [
            "HTTP/1.1 200 OK",
            "Server: gunicorn",
        ],
        "structured_signals": [
            {
                "type": "header",
                "port": None,
                "protocol": None,
                "state": None,
                "service": None,
                "version": None,
                "name": "Server",
                "key": None,
                "value": "gunicorn",
                "status": None,
                "size": None,
                "path": None,
                "label": None,
                "target": None,
                "method": None,
                "action": None,
                "fields": None,
                "redirect_target": None,
                "message": None,
                "code": None,
                "parameter_conflict": None,
            },
            {
                "type": "ui_link",
                "port": None,
                "protocol": None,
                "state": None,
                "service": None,
                "version": None,
                "name": None,
                "key": None,
                "value": None,
                "status": None,
                "size": None,
                "path": None,
                "label": "Dashboard",
                "target": "/",
                "method": None,
                "action": None,
                "fields": None,
                "redirect_target": None,
                "message": None,
                "code": None,
                "parameter_conflict": None,
            },
        ],
        "decision_evidence": ['<a href=\"/\">Dashboard</a>'],
        "lossiness_risk": "medium",
    }

    validate(payload, TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT.schema)


def test_tool_output_compressor_schema_validates_failure_payload() -> None:
    payload = {
        "summary": "Nmap failed due to conflicting parameters.",
        "key_findings": [
            "ERROR: You cannot use -p when not doing a port scan.",
            "Conflicting parameters: -sn and ports=1-10000.",
        ],
        "structured_signals": [
            {
                "type": "error_context",
                "port": None,
                "protocol": None,
                "state": None,
                "service": None,
                "version": None,
                "name": None,
                "key": None,
                "value": None,
                "status": None,
                "size": None,
                "path": None,
                "label": None,
                "target": None,
                "method": None,
                "action": None,
                "fields": None,
                "redirect_target": None,
                "message": "Cannot use -p with -sn",
                "code": None,
                "parameter_conflict": ["-sn", "-p"],
            }
        ],
        "decision_evidence": [
            "ERROR: You cannot use -F (fast scan) or -p (explicit port selection) when not doing a port scan."
        ],
        "lossiness_risk": "low",
    }

    validate(payload, TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT.schema)


def test_tool_output_compressor_schema_is_openai_strict_compatible() -> None:
    validate_openai_strict_schema(TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT)


def test_candidate_extractor_schema_supports_optional_vulnerability_fields() -> None:
    schema = GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema
    observation_item = schema["properties"]["candidate_observations"]["items"]
    observation_properties = observation_item["properties"]

    assert "vulnerability" in observation_properties
    assert "vulnerability_confidence" in observation_properties

    required_fields = set(observation_item["required"])
    assert "vulnerability" in required_fields
    assert "vulnerability_confidence" in required_fields

    vulnerability_schema = observation_properties["vulnerability"]
    assert vulnerability_schema["type"] == ["object", "null"]
    assert vulnerability_schema["additionalProperties"] is False
    assert vulnerability_schema["required"] == ["id", "title", "severity"]

    confidence_schema = observation_properties["vulnerability_confidence"]
    assert confidence_schema["type"] == ["number", "null"]
    assert confidence_schema["minimum"] == 0.0
    assert confidence_schema["maximum"] == 1.0


def test_candidate_extractor_schema_keeps_legacy_observation_shape_required() -> None:
    observation_item = GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema["properties"][
        "candidate_observations"
    ]["items"]
    required_fields = set(observation_item["required"])
    assert required_fields == {
        "observation_type",
        "subject_type",
        "subject_key_hint",
        "assertion_level",
        "confidence",
        "attributes",
        "rationale",
        "evidence_refs",
        "vulnerability",
        "vulnerability_confidence",
    }


def test_candidate_extractor_schema_restricts_vulnerability_fields_to_vuln_types() -> None:
    observation_item = GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema["properties"][
        "candidate_observations"
    ]["items"]
    assert "allOf" not in observation_item


def test_candidate_extractor_schema_validates_legacy_payload_shape() -> None:
    payload = {
        "candidate_observations": [
            {
                "observation_type": "service.version",
                "subject_type": "host.service",
                "subject_key_hint": "10.0.0.5:5432",
                "assertion_level": "candidate",
                "confidence": 0.72,
                "attributes": [{"key": "version", "value": "PostgreSQL 11.5"}],
                "rationale": "Banner includes PostgreSQL 11.5",
                "evidence_refs": [
                    {
                        "evidence_archive_id": "ev-1",
                        "excerpt": "PostgreSQL 11.5 on x86_64-pc-linux-gnu",
                    }
                ],
                "vulnerability": None,
                "vulnerability_confidence": None,
            }
        ],
        "analyst_notes": [],
        "no_signal": False,
    }

    validate(payload, GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema)


def test_candidate_extractor_schema_validates_vulnerability_payload_shape() -> None:
    payload = {
        "candidate_observations": [
            {
                "observation_type": "finding.vulnerability.version_eol",
                "subject_type": "host.service",
                "subject_key_hint": "10.0.0.5:5432",
                "assertion_level": "candidate",
                "confidence": 0.83,
                "attributes": [{"key": "version", "value": "PostgreSQL 11.5"}],
                "rationale": "Version is no longer supported",
                "evidence_refs": [
                    {
                        "evidence_archive_id": "ev-2",
                        "excerpt": "Detected PostgreSQL 11.5",
                    }
                ],
                "vulnerability": {
                    "id": "CVE-2023-12345",
                    "title": "Out-of-support PostgreSQL version",
                    "severity": "high",
                },
                "vulnerability_confidence": 0.86,
            }
        ],
        "analyst_notes": [],
        "no_signal": False,
    }

    validate(payload, GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema)


def test_candidate_extractor_schema_allows_nullable_vuln_fields_for_non_vuln_observation() -> None:
    payload = {
        "candidate_observations": [
            {
                "observation_type": "service.version",
                "subject_type": "host.service",
                "subject_key_hint": "10.0.0.5:5432",
                "assertion_level": "candidate",
                "confidence": 0.61,
                "attributes": [{"key": "version", "value": "PostgreSQL 11.5"}],
                "rationale": "Version evidence captured",
                "evidence_refs": [
                    {"evidence_archive_id": "ev-3", "excerpt": "PostgreSQL 11.5"}
                ],
                "vulnerability": None,
                "vulnerability_confidence": 0.5,
            }
        ],
        "analyst_notes": [],
        "no_signal": False,
    }

    validate(payload, GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema)


def test_post_tool_decision_schema_supports_optional_candidate_observations() -> None:
    schema = POST_TOOL_DECISION_STRUCTURED_OUTPUT.schema
    properties = schema["properties"]
    assert "candidate_observations" in properties
    assert "candidate_observations" in set(schema.get("required", []))
    assert properties["candidate_observations"]["type"] == ["array", "null"]


def test_post_tool_decision_candidate_refs_allow_source_artifact_id() -> None:
    payload = {
        "next_action": "call_tool",
        "action_reasoning": "Need one additional validation command.",
        "tool_intent": {
            "description": "Validate service response with focused probe",
            "target": "10.0.0.8:5432",
            "focus": "postgresql version verification",
        },
        "user_goal_achieved": False,
        "todo_progress": [],
        "effective_next_goal": None,
        "failure_detected": False,
        "failure_category": None,
        "retry_suggested": False,
        "candidate_observations": [
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.instance",
                "subject_key_hint": "cve-2024-0001:service.socket:10.0.0.8/tcp/5432",
                "assertion_level": "candidate",
                "confidence": 0.84,
                "attributes": [{"key": "version", "value": "11.5"}],
                "rationale": "Version appears vulnerable by advisory matrix.",
                "evidence_refs": [
                    {
                        "evidence_archive_id": None,
                        "source_artifact_id": "artifact-1",
                        "excerpt": "PostgreSQL 11.5",
                    }
                ],
                "vulnerability": {
                    "id": "CVE-2024-0001",
                    "title": "PostgreSQL version likely vulnerable",
                    "severity": "high",
                },
                "vulnerability_confidence": 0.9,
            }
        ],
    }

    validate(payload, POST_TOOL_DECISION_STRUCTURED_OUTPUT.schema)
