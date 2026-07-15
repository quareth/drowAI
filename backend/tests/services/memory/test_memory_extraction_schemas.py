"""Validate memory extraction schema contracts without DB or LLM dependencies."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.services.memory.memory_extraction_schemas import (
    MEMORY_EXTRACTION_MAX_FACTS_PER_TURN,
    ExtractionFact,
    ExtractionResult,
    GateClassifierOutput,
)


def test_gate_classifier_output_true() -> None:
    parsed = GateClassifierOutput(extractable=True)
    assert parsed.extractable is True


def test_gate_classifier_output_false() -> None:
    parsed = GateClassifierOutput(extractable=False)
    assert parsed.extractable is False


def test_extraction_fact_valid_user_profile() -> None:
    fact = ExtractionFact(content="User prefers concise responses.", tier="user_profile")
    assert fact.tier == "user_profile"


def test_extraction_fact_valid_task_engagement() -> None:
    fact = ExtractionFact(
        content="Engagement focus is web application testing.",
        tier="task_engagement",
    )
    assert fact.tier == "task_engagement"


def test_extraction_fact_invalid_tier() -> None:
    with pytest.raises(ValidationError):
        ExtractionFact(content="Invalid tier should fail.", tier="invalid_tier")


def test_extraction_result_truncates_excess_facts() -> None:
    facts = [
        ExtractionFact(content=f"Fact sentence {idx}.", tier="user_profile")
        for idx in range(MEMORY_EXTRACTION_MAX_FACTS_PER_TURN + 3)
    ]

    parsed = ExtractionResult(facts=facts, skipped_reason=None)

    assert len(parsed.facts) == MEMORY_EXTRACTION_MAX_FACTS_PER_TURN
