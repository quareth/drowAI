"""Unit tests for unified intent brief derivation and seed helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from backend.services.langgraph_chat.intent.briefs import (
    METADATA_KEY_CLASSIFIER_RAW_RESPONSE,
    METADATA_KEY_INTENT_BRIEF_SEED,
    METADATA_KEY_INTENT_TARGET_CONTINUITY,
    METADATA_KEY_INTENT_TARGET_RESOLUTION,
    METADATA_KEY_REQUEST_CONTRACT,
    METADATA_KEY_TURN_INTERPRETATION,
    build_working_memory_intent_brief,
    ensure_intent_brief_seed_present,
    write_intent_brief_seed,
)


_FIXTURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "core"
    / "prompts"
    / "tests"
    / "fixtures"
    / "intent_brief_equivalence"
)
_INTERPRETATION_FIELDS = (
    "resolved_user_intent",
    "original_goal",
    "task_seed",
    "overall_goal",
    "continuation_mode",
    "resolved_step_title",
    "resolved_step_detail",
    "next_operational_goal",
    "success_condition",
    "execution_readiness",
    "blocking_reason",
    "explicit_constraints",
    "suggested_category_focus",
    "retrieval_hints",
    "relevant_memory_fragments",
)


def _full_turn_interpretation() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 10.0.0.5",
        "original_goal": (
            "Scan 10.0.0.5 for open ports, then identify exposed services"
        ),
        "task_seed": [
            "Scan 10.0.0.5 for open ports",
            "Identify exposed services",
        ],
        "overall_goal": "Map exposed services",
        "continuation_mode": "new_request",
        "resolved_step_title": "Initial scan",
        "resolved_step_detail": "Enumerate service surface",
        "next_operational_goal": "Run TCP port scan",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "success_condition": "Open ports and service banners",
        "explicit_constraints": ["No UDP scan"],
        "relevant_memory_fragments": ["prior host discovery found target alive"],
        "suggested_category_focus": ["network_recon"],
        "retrieval_hints": ["tcp scan", "banner"],
    }


def _full_metadata(*, explicit_interpretation_key: bool = True) -> Dict[str, Any]:
    interpretation = _full_turn_interpretation()
    metadata: Dict[str, Any] = {
        METADATA_KEY_CLASSIFIER_RAW_RESPONSE: {
            "label": "direct_executor",
            "turn_interpretation": interpretation,
        },
        METADATA_KEY_REQUEST_CONTRACT: {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        METADATA_KEY_INTENT_TARGET_RESOLUTION: {
            "target_status": "resolved",
            "resolved_target": "10.0.0.5",
            "target_source": "explicit_current_message",
        },
        METADATA_KEY_INTENT_TARGET_CONTINUITY: {"status": "disallow"},
    }
    if explicit_interpretation_key:
        metadata[METADATA_KEY_TURN_INTERPRETATION] = interpretation
    return metadata


def test_build_working_memory_intent_brief_happy_path() -> None:
    brief = build_working_memory_intent_brief(_full_metadata())

    assert brief["resolved_user_intent"] == "Scan open ports on 10.0.0.5"
    assert (
        brief["original_goal"]
        == "Scan 10.0.0.5 for open ports, then identify exposed services"
    )
    assert brief["task_seed"] == [
        "Scan 10.0.0.5 for open ports",
        "Identify exposed services",
    ]
    assert brief["overall_goal"] == "Map exposed services"
    assert brief["continuation_mode"] == "new_request"
    assert brief["resolved_target"] == "10.0.0.5"
    assert brief["target_status"] == "resolved"
    assert brief["target_source"] == "explicit_current_message"
    assert brief["explicit_constraints"] == ["No UDP scan"]
    assert brief["suggested_category_focus"] == ["network_recon"]
    assert brief["request_contract"] == {
        "question_type": "multi_step",
        "answer_style": "normal",
        "terminal_when": "all_steps_done",
    }


def test_build_working_memory_intent_brief_reads_nested_turn_interpretation() -> None:
    metadata = _full_metadata(explicit_interpretation_key=False)
    assert METADATA_KEY_TURN_INTERPRETATION not in metadata

    brief = build_working_memory_intent_brief(metadata)
    assert brief["resolved_user_intent"] == "Scan open ports on 10.0.0.5"
    assert (
        brief["original_goal"]
        == "Scan 10.0.0.5 for open ports, then identify exposed services"
    )
    assert brief["task_seed"] == [
        "Scan 10.0.0.5 for open ports",
        "Identify exposed services",
    ]
    assert brief["next_operational_goal"] == "Run TCP port scan"


def test_build_working_memory_intent_brief_defaults_on_empty_metadata() -> None:
    brief = build_working_memory_intent_brief({})

    assert brief["resolved_user_intent"] is None
    assert brief["original_goal"] is None
    assert brief["task_seed"] == []
    assert brief["overall_goal"] is None
    assert brief["continuation_mode"] == "ambiguous"
    assert brief["execution_readiness"] == "ambiguous"
    assert brief["resolved_target"] is None
    assert brief["target_status"] == "unresolved"
    assert brief["target_source"] == "none"
    assert brief["explicit_constraints"] == []
    assert brief["suggested_category_focus"] == []
    assert brief["retrieval_hints"] == []
    assert brief["relevant_memory_fragments"] == []


def test_write_intent_brief_seed_populates_seed_and_turn_interpretation() -> None:
    metadata = _full_metadata(explicit_interpretation_key=False)
    write_intent_brief_seed(metadata)

    assert METADATA_KEY_TURN_INTERPRETATION in metadata
    assert isinstance(metadata[METADATA_KEY_TURN_INTERPRETATION], dict)
    seed = metadata[METADATA_KEY_INTENT_BRIEF_SEED]
    assert isinstance(seed, dict)
    assert seed["resolved_user_intent"] == "Scan open ports on 10.0.0.5"
    assert (
        seed["original_goal"]
        == "Scan 10.0.0.5 for open ports, then identify exposed services"
    )
    assert seed["task_seed"] == [
        "Scan 10.0.0.5 for open ports",
        "Identify exposed services",
    ]


def test_build_working_memory_intent_brief_normalizes_invalid_original_goal() -> None:
    metadata = _full_metadata()
    metadata[METADATA_KEY_TURN_INTERPRETATION]["original_goal"] = ["not", "a", "goal"]

    brief = build_working_memory_intent_brief(metadata)

    assert brief["original_goal"] is None


def test_build_working_memory_intent_brief_normalizes_task_seed() -> None:
    metadata = _full_metadata()
    metadata[METADATA_KEY_TURN_INTERPRETATION]["task_seed"] = [
        " Discover live hosts ",
        "",
        123,
        "Choose one online host",
        "Scan PostgreSQL",
        "Ignored fourth",
    ]

    brief = build_working_memory_intent_brief(metadata)

    assert brief["task_seed"] == [
        "Discover live hosts",
        "Choose one online host",
        "Scan PostgreSQL",
    ]


def test_ensure_intent_brief_seed_present_is_idempotent() -> None:
    metadata = _full_metadata()
    metadata[METADATA_KEY_INTENT_BRIEF_SEED] = {"resolved_user_intent": "existing"}

    ensure_intent_brief_seed_present(metadata)

    assert metadata[METADATA_KEY_INTENT_BRIEF_SEED] == {
        "resolved_user_intent": "existing"
    }


def test_unified_brief_matches_frozen_legacy_union() -> None:
    """Unified builder output must equal union of frozen legacy surfaces."""
    fixture_paths = sorted(_FIXTURE_DIR.glob("*.json"))
    assert len(fixture_paths) == 12

    for fixture_path in fixture_paths:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        expected = dict(payload["ptr_surface"])
        expected.update(payload["planner_surface"])
        expected.update(payload["tool_surface"])
        expected["original_goal"] = None
        expected["task_seed"] = []

        interpretation = {
            field: expected.get(field)
            for field in _INTERPRETATION_FIELDS
        }
        metadata = {
            METADATA_KEY_TURN_INTERPRETATION: interpretation,
            METADATA_KEY_REQUEST_CONTRACT: dict(expected.get("request_contract") or {}),
            METADATA_KEY_INTENT_TARGET_RESOLUTION: {
                "resolved_target": expected.get("resolved_target"),
                "target_status": expected.get("target_status"),
                "target_source": expected.get("target_source"),
            },
            METADATA_KEY_INTENT_TARGET_CONTINUITY: {"status": "disallow"},
        }

        actual = build_working_memory_intent_brief(metadata)
        assert actual == expected, fixture_path.name
