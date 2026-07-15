"""Canonical JSON schema specs for wired structured-output LLM callsites.

This module is the single source of truth for JSON schemas consumed by
runtime logic on wired LangGraph/backend paths.
"""

from __future__ import annotations

from typing import Any, Dict

from agent.providers.llm.core.base import StructuredOutputSpec
from backend.services.reporting.report_section_schema import (
    engagement_report_section_json_schema,
)


def _spec(name: str, schema: Dict[str, Any]) -> StructuredOutputSpec:
    """Build immutable structured-output spec objects."""
    return StructuredOutputSpec(name=name, schema=schema, strict=True)


def _candidate_evidence_ref_schema(*, allow_source_artifact_ref: bool) -> Dict[str, Any]:
    """Build strict evidence reference schema for candidate observation rows."""
    properties: Dict[str, Any] = {
        "evidence_archive_id": {"type": "string", "minLength": 1},
        "excerpt": {"type": "string", "minLength": 1},
    }
    required = ["evidence_archive_id", "excerpt"]
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if allow_source_artifact_ref:
        properties["evidence_archive_id"] = {"type": ["string", "null"], "minLength": 1}
        properties["source_artifact_id"] = {"type": ["string", "null"], "minLength": 1}
        schema["required"] = ["evidence_archive_id", "source_artifact_id", "excerpt"]
        schema["anyOf"] = [
            {
                "type": "object",
                "properties": {
                    "evidence_archive_id": {"type": "string", "minLength": 1},
                    "source_artifact_id": {"type": ["string", "null"], "minLength": 1},
                    "excerpt": {"type": "string", "minLength": 1},
                },
                "required": ["evidence_archive_id", "source_artifact_id", "excerpt"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "evidence_archive_id": {"type": ["string", "null"], "minLength": 1},
                    "source_artifact_id": {"type": "string", "minLength": 1},
                    "excerpt": {"type": "string", "minLength": 1},
                },
                "required": ["evidence_archive_id", "source_artifact_id", "excerpt"],
                "additionalProperties": False,
            },
        ]
    return schema


def _candidate_observation_item_schema(*, allow_source_artifact_ref: bool) -> Dict[str, Any]:
    """Build strict candidate observation schema used across extraction flows."""
    return {
        "type": "object",
        "properties": {
            "observation_type": {"type": "string", "minLength": 3},
            "subject_type": {"type": "string", "minLength": 3},
            "subject_key_hint": {"type": "string", "minLength": 1},
            "assertion_level": {"type": "string", "enum": ["candidate"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "attributes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "minLength": 1},
                        "value": {"type": "string"},
                    },
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
            },
            "rationale": {"type": "string", "minLength": 1},
            "evidence_refs": {
                "type": "array",
                "items": _candidate_evidence_ref_schema(
                    allow_source_artifact_ref=allow_source_artifact_ref
                ),
                "minItems": 1,
            },
            "vulnerability": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "severity": {"type": "string", "minLength": 1},
                },
                "required": ["id", "title", "severity"],
                "additionalProperties": False,
            },
            "vulnerability_confidence": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "required": [
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
        ],
        "additionalProperties": False,
    }


def _prior_turn_reference_schema() -> Dict[str, Any]:
    """Build strict classifier hints for prior-turn reference resolution."""
    return {
        "type": "object",
        "properties": {
            "required": {"type": "boolean"},
            "operation": {
                "type": ["string", "null"],
                "enum": [
                    "reference_resolution",
                    "continuation",
                    "revision",
                    "comparison",
                    "quote_or_recall",
                    "none",
                    None,
                ],
            },
            "status": {
                "type": "string",
                "enum": ["resolved", "ambiguous", "unresolved", "none"],
            },
            "confidence": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "hints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "reference_kind": {
                            "type": "string",
                            "enum": [
                                "rendered_turn",
                                "relative_turn",
                                "anchor_text",
                                "unknown",
                            ],
                        },
                        "turn_number": {"type": ["integer", "null"], "minimum": 1},
                        "speaker": {
                            "type": ["string", "null"],
                            "enum": ["user", "assistant", "system", "tool", "unknown", None],
                        },
                        "anchor_text": {"type": ["string", "null"]},
                        "reason": {"type": ["string", "null"]},
                        "confidence": {
                            "type": ["number", "null"],
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": [
                        "reference_kind",
                        "turn_number",
                        "speaker",
                        "anchor_text",
                        "reason",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["required", "operation", "status", "confidence", "hints"],
        "additionalProperties": False,
    }


INTENT_CLASSIFIER_STRUCTURED_OUTPUT = _spec(
    "intent_classifier",
    {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": [
                    "simple_chat",
                    "direct_executor",
                    "plan_executor",
                ],
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "suggested_capabilities": {
                "type": "array",
                "items": {"type": "string"},
            },
            "requested_output_format": {
                "type": ["string", "null"],
                "enum": ["json", "csv", "markdown", None],
            },
            "question_type": {
                "type": ["string", "null"],
                "enum": ["binary_check", "multi_step", "open_ended", None],
            },
            "answer_style": {
                "type": ["string", "null"],
                "enum": ["short", "normal", None],
            },
            "terminal_when": {
                "type": ["string", "null"],
                "enum": ["determined", "all_steps_done", None],
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
            },
            "target_status": {
                "type": "string",
                "enum": ["resolved", "unresolved", "ambiguous"],
            },
            "resolved_target": {
                "type": ["string", "null"],
            },
            "target_source": {
                "type": "string",
                "enum": ["explicit_current_message", "referential_history", "environment", "none"],
            },
            "target_confidence": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "target_evidence": {
                "type": ["string", "null"],
            },
            "prior_target_reuse": {
                "type": "string",
                "enum": ["allow", "disallow", "ambiguous"],
            },
            "prior_target_reuse_evidence": {
                "type": ["string", "null"],
            },
            "turn_interpretation": {
                "type": "object",
                "properties": {
                    "resolved_user_intent": {"type": "string"},
                    "original_goal": {"type": ["string", "null"]},
                    "task_seed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                    },
                    "overall_goal": {"type": ["string", "null"]},
                    "continuation_mode": {
                        "type": "string",
                        "enum": [
                            "new_request",
                            "continue_prior_work",
                            "continue_prior_step",
                            "revise_approach",
                            "ambiguous",
                        ],
                    },
                    "step_reference_text": {"type": ["string", "null"]},
                    "step_reference_status": {
                        "type": "string",
                        "enum": ["none", "resolved", "unresolved", "ambiguous"],
                    },
                    "resolved_step_title": {"type": ["string", "null"]},
                    "resolved_step_detail": {"type": ["string", "null"]},
                    "next_operational_goal": {"type": ["string", "null"]},
                    "execution_readiness": {
                        "type": "string",
                        "enum": ["ready", "blocked", "ambiguous"],
                    },
                    "blocking_reason": {"type": ["string", "null"]},
                    "success_condition": {"type": ["string", "null"]},
                    "explicit_constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "relevant_memory_fragments": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "suggested_category_focus": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "retrieval_hints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "resolved_user_intent",
                    "original_goal",
                    "task_seed",
                    "overall_goal",
                    "continuation_mode",
                    "step_reference_text",
                    "step_reference_status",
                    "resolved_step_title",
                    "resolved_step_detail",
                    "next_operational_goal",
                    "execution_readiness",
                    "blocking_reason",
                    "success_condition",
                    "explicit_constraints",
                    "relevant_memory_fragments",
                    "suggested_category_focus",
                    "retrieval_hints",
                ],
                "additionalProperties": False,
            },
            "prior_turn_reference": _prior_turn_reference_schema(),
            "reasoning": {"type": "string"},
        },
        "required": [
            "label",
            "confidence",
            "suggested_capabilities",
            "requested_output_format",
            "question_type",
            "answer_style",
            "terminal_when",
            "risk_flags",
            "target_status",
            "resolved_target",
            "target_source",
            "target_confidence",
            "target_evidence",
            "prior_target_reuse",
            "prior_target_reuse_evidence",
            "turn_interpretation",
            "prior_turn_reference",
            "reasoning",
        ],
        "additionalProperties": False,
    },
)


TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT = _spec(
    "tool_category_selector",
    {
        "type": "object",
        "properties": {
            "selected_categories": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reasoning": {"type": "string"},
        },
        "required": ["selected_categories", "reasoning"],
        "additionalProperties": False,
    },
)


TOOL_SELECTOR_STRUCTURED_OUTPUT = _spec(
    "tool_selector",
    {
        "type": "object",
        "properties": {
            "selected_tools": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "execution_strategy": {
                "type": "string",
                "enum": ["sequential", "parallel"],
            },
            "reasoning": {"type": "string"},
        },
        "required": ["selected_tools", "execution_strategy", "reasoning"],
        "additionalProperties": False,
    },
)

PLANNER_CONTRACT_STRUCTURED_OUTPUT = _spec(
    "planner_contract",
    {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["plan_ready", "clarify_required"]},
            "plan": {"type": "array", "items": {"type": "string"}},
            "todo_list": {"type": "array", "items": {"type": "string"}},
            "first_goal": {"type": "string"},
            "clarify_request": {
                "type": ["object", "null"],
                "properties": {
                    "required_blockers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "slot": {"type": "string"},
                                "question": {"type": "string"},
                                "input_type": {"type": "string", "enum": ["select"]},
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                    "maxItems": 4,
                                },
                            },
                            "required": ["slot", "question", "input_type", "options"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["required_blockers"],
                "additionalProperties": False,
            },
        },
        "required": ["mode", "plan", "todo_list", "first_goal", "clarify_request"],
        "additionalProperties": False,
    },
)


DECISION_ROUTER_STRUCTURED_OUTPUT = _spec(
    "decision_router",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["call_tool", "reflect", "finalize", "think_more", "synthesis"],
            },
            "reasoning": {"type": "string"},
        },
        "required": ["action", "reasoning"],
        "additionalProperties": False,
    },
)


THINK_MORE_STRUCTURED_OUTPUT = _spec(
    "think_more",
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "updated_plan": {"type": "array", "items": {"type": "string"}},
            "next_goal": {"type": "string"},
            "key_observations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["reasoning", "updated_plan", "next_goal", "key_observations"],
        "additionalProperties": False,
    },
)


POST_TOOL_DECISION_STRUCTURED_OUTPUT = _spec(
    "post_tool_decision",
    {
        "type": "object",
        "properties": {
            "next_action": {
                "type": "string",
                "enum": ["call_tool", "think_more", "reflect", "finalize"],
            },
            "action_reasoning": {
                "type": "string",
                "minLength": 5,
            },
            "tool_intent": {
                "type": ["object", "null"],
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "target": {"type": ["string", "null"]},
                    "focus": {"type": ["string", "null"]},
                },
                "required": ["description", "target", "focus"],
                "additionalProperties": False,
            },
            "user_goal_achieved": {
                "type": "boolean",
            },
            "todo_progress": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "in_progress",
                                "completed",
                                "skipped",
                            ],
                        },
                        "completion_type": {
                            "type": ["string", "null"],
                            "enum": ["positive", "negative", None],
                        },
                        "completion_reason": {"type": ["string", "null"]},
                    },
                    "required": [
                        "index",
                        "status",
                        "completion_type",
                        "completion_reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "effective_next_goal": {
                "type": ["string", "null"],
            },
            "failure_detected": {
                "type": "boolean",
            },
            "failure_category": {
                "type": ["string", "null"],
                "enum": [
                    "network_error",
                    "permission_denied",
                    "timeout",
                    "invalid_params",
                    "tool_unavailable",
                    "empty_output",
                    "unknown",
                    None,
                ],
            },
            "retry_suggested": {
                "type": "boolean",
            },
            "candidate_observations": {
                "type": ["array", "null"],
                "items": _candidate_observation_item_schema(
                    allow_source_artifact_ref=True
                ),
            },
        },
        "required": [
            "next_action",
            "action_reasoning",
            "tool_intent",
            "user_goal_achieved",
            "todo_progress",
            "effective_next_goal",
            "failure_detected",
            "failure_category",
            "retry_suggested",
            "candidate_observations",
        ],
        "additionalProperties": False,
    },
)


REFLECT_STRUCTURED_OUTPUT = _spec(
    "reflection_analysis",
    {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string"},
            "alternative_approaches": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["root_cause", "alternative_approaches"],
        "additionalProperties": False,
    },
)


TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT = _spec(
    "tool_output_compressor",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "key_findings": {"type": "array", "items": {"type": "string"}},
            "structured_signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "service",
                                "header",
                                "redirect",
                                "path",
                                "ui_link",
                                "form",
                                "endpoint",
                                "error_context",
                                "kv_pair",
                            ],
                        },
                        "port": {"type": ["integer", "null"]},
                        "protocol": {"type": ["string", "null"]},
                        "state": {"type": ["string", "null"]},
                        "service": {"type": ["string", "null"]},
                        "version": {"type": ["string", "null"]},
                        "name": {"type": ["string", "null"]},
                        "key": {"type": ["string", "null"]},
                        "value": {"type": ["string", "null"]},
                        "status": {"type": ["integer", "null"]},
                        "size": {"type": ["string", "null"]},
                        "path": {"type": ["string", "null"]},
                        "label": {"type": ["string", "null"]},
                        "target": {"type": ["string", "null"]},
                        "method": {"type": ["string", "null"]},
                        "action": {"type": ["string", "null"]},
                        "fields": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                        },
                        "redirect_target": {"type": ["string", "null"]},
                        "message": {"type": ["string", "null"]},
                        "code": {"type": ["string", "null"]},
                        "parameter_conflict": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "type",
                        "port",
                        "protocol",
                        "state",
                        "service",
                        "version",
                        "name",
                        "key",
                        "value",
                        "status",
                        "size",
                        "path",
                        "label",
                        "target",
                        "method",
                        "action",
                        "fields",
                        "redirect_target",
                        "message",
                        "code",
                        "parameter_conflict",
                    ],
                    "additionalProperties": False,
                },
            },
            "decision_evidence": {"type": "array", "items": {"type": "string"}},
            "lossiness_risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
        },
        "required": [
            "summary",
            "key_findings",
            "structured_signals",
            "decision_evidence",
            "lossiness_risk",
        ],
        "additionalProperties": False,
    },
)

GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT = _spec(
    "generic_candidate_extractor",
    {
        "type": "object",
        "properties": {
            "candidate_observations": {
                "type": "array",
                "items": _candidate_observation_item_schema(
                    allow_source_artifact_ref=False
                ),
            },
            "analyst_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "minLength": 1},
                        "evidence_refs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "evidence_archive_id": {"type": "string", "minLength": 1},
                                    "excerpt": {"type": "string", "minLength": 1},
                                },
                                "required": ["evidence_archive_id", "excerpt"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["note", "evidence_refs"],
                    "additionalProperties": False,
                },
            },
            "no_signal": {"type": "boolean"},
        },
        "required": ["candidate_observations", "analyst_notes", "no_signal"],
        "additionalProperties": False,
    },
)


MEMORY_GATE_STRUCTURED_OUTPUT = _spec(
    "memory_gate",
    {
        "type": "object",
        "properties": {
            "extractable": {"type": "boolean"},
        },
        "required": ["extractable"],
        "additionalProperties": False,
    },
)

MEMORY_EXTRACTION_STRUCTURED_OUTPUT = _spec(
    "memory_extraction",
    {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "minLength": 5},
                        "tier": {
                            "type": "string",
                            "enum": ["user_profile", "task_engagement"],
                        },
                    },
                    "required": ["content", "tier"],
                    "additionalProperties": False,
                },
            },
            "skipped_reason": {"type": ["string", "null"]},
        },
        "required": ["facts", "skipped_reason"],
        "additionalProperties": False,
    },
)


def _task_closure_memo_source_refs_schema(*, max_items: int) -> Dict[str, Any]:
    """Build bounded source reference arrays for task closure memo output."""
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 256},
        "maxItems": max_items,
    }


def _task_closure_memo_source_backed_item_schema(
    properties: Dict[str, Any],
    required: list[str],
) -> Dict[str, Any]:
    """Build source-backed memo item schemas with closed object contracts."""
    item_properties = {
        **properties,
        "evidence_refs": _task_closure_memo_source_refs_schema(max_items=100),
        "knowledge_refs": _task_closure_memo_source_refs_schema(max_items=100),
    }
    required_fields = [*required, "evidence_refs", "knowledge_refs"]
    evidence_backed_properties = {
        **item_properties,
        "evidence_refs": {
            **_task_closure_memo_source_refs_schema(max_items=100),
            "minItems": 1,
        },
    }
    knowledge_backed_properties = {
        **item_properties,
        "knowledge_refs": {
            **_task_closure_memo_source_refs_schema(max_items=100),
            "minItems": 1,
        },
    }
    return {
        "type": "object",
        "properties": item_properties,
        "required": required_fields,
        "anyOf": [
            {
                "type": "object",
                "properties": evidence_backed_properties,
                "required": required_fields,
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": knowledge_backed_properties,
                "required": required_fields,
                "additionalProperties": False,
            },
        ],
        "additionalProperties": False,
    }


TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT = _spec(
    "task_closure_memo",
    {
        "type": "object",
        "properties": {
            "task_name": {"type": "string", "minLength": 1, "maxLength": 512},
            "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            "include_in_report_recommendation": {
                "type": "object",
                "properties": {
                    "include": {"type": "boolean"},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "required": ["include", "reason"],
                "additionalProperties": False,
            },
            "actions_performed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "source": {
                            "type": "string",
                            "enum": ["transcript", "evidence", "knowledge"],
                        },
                    },
                    "required": ["text", "source"],
                    "additionalProperties": False,
                },
                "maxItems": 100,
            },
            "reportable_observations": {
                "type": "array",
                "items": _task_closure_memo_source_backed_item_schema(
                    {
                        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    ["text", "confidence"],
                ),
                "maxItems": 100,
            },
            "possible_findings": {
                "type": "array",
                "items": _task_closure_memo_source_backed_item_schema(
                    {
                        "title": {"type": "string", "minLength": 1, "maxLength": 512},
                        "severity_hint": {
                            "type": ["string", "null"],
                            "enum": [
                                "informational",
                                "low",
                                "medium",
                                "high",
                                "critical",
                                None,
                            ],
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "description": {
                            "type": ["string", "null"],
                            "maxLength": 4000,
                        },
                    },
                    ["title", "severity_hint", "confidence", "description"],
                ),
                "maxItems": 100,
            },
            "limitations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "maxItems": 100,
            },
            "unsupported_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "maxItems": 100,
            },
            "evidence_refs": _task_closure_memo_source_refs_schema(max_items=500),
            "knowledge_refs": _task_closure_memo_source_refs_schema(max_items=500),
        },
        "required": [
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
        ],
        "additionalProperties": False,
    },
)

ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT = _spec(
    "engagement_report_section",
    engagement_report_section_json_schema(),
)


__all__ = [
    "DECISION_ROUTER_STRUCTURED_OUTPUT",
    "ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT",
    "GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT",
    "INTENT_CLASSIFIER_STRUCTURED_OUTPUT",
    "MEMORY_EXTRACTION_STRUCTURED_OUTPUT",
    "MEMORY_GATE_STRUCTURED_OUTPUT",
    "PLANNER_CONTRACT_STRUCTURED_OUTPUT",
    "REFLECT_STRUCTURED_OUTPUT",
    "THINK_MORE_STRUCTURED_OUTPUT",
    "TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT",
    "TOOL_SELECTOR_STRUCTURED_OUTPUT",
    "TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT",
    "POST_TOOL_DECISION_STRUCTURED_OUTPUT",
    "TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT",
]
