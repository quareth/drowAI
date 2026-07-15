"""Tests for idempotent intent-brief seed backfill helper."""

from __future__ import annotations

from typing import Any, Dict

from backend.services.langgraph_chat.intent.briefs import (
    METADATA_KEY_INTENT_BRIEF_SEED,
    METADATA_KEY_TURN_INTERPRETATION,
    ensure_intent_brief_seed_present,
)


def test_ensure_intent_brief_seed_present_sets_defaults_on_empty_metadata() -> None:
    metadata: Dict[str, Any] = {}

    ensure_intent_brief_seed_present(metadata)

    assert metadata[METADATA_KEY_TURN_INTERPRETATION] == {}
    assert isinstance(metadata[METADATA_KEY_INTENT_BRIEF_SEED], dict)


def test_ensure_intent_brief_seed_present_keeps_existing_seed_object() -> None:
    existing_seed = {"resolved_user_intent": "pre-existing"}
    metadata: Dict[str, Any] = {
        METADATA_KEY_INTENT_BRIEF_SEED: existing_seed,
    }

    ensure_intent_brief_seed_present(metadata)

    assert metadata[METADATA_KEY_INTENT_BRIEF_SEED] is existing_seed


def test_ensure_intent_brief_seed_present_keeps_existing_turn_interpretation() -> None:
    existing_interpretation = {
        "resolved_user_intent": "pre-existing intent",
        "next_operational_goal": "pre-existing goal",
    }
    metadata: Dict[str, Any] = {
        METADATA_KEY_TURN_INTERPRETATION: existing_interpretation,
    }

    ensure_intent_brief_seed_present(metadata)

    assert metadata[METADATA_KEY_TURN_INTERPRETATION] is existing_interpretation
    assert isinstance(metadata[METADATA_KEY_INTENT_BRIEF_SEED], dict)
