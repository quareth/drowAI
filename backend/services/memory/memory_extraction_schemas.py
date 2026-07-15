"""Define Pydantic output contracts for memory extraction LLM responses.

This module owns schema-only models for gate classification and extraction
facts. It intentionally contains no prompt construction, LLM calls, or storage
logic.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, field_validator

from backend.services.memory.memory_models import MemoryTier

MEMORY_EXTRACTION_MAX_FACTS_PER_TURN = int(
    os.getenv("MEMORY_EXTRACTION_MAX_FACTS_PER_TURN", "5")
)
_ALLOWED_MEMORY_TIERS = {
    MemoryTier.USER_PROFILE.value,
    MemoryTier.TASK_ENGAGEMENT.value,
}


class GateClassifierOutput(BaseModel):
    """Structured output for the extraction gate classifier."""

    extractable: bool


class ExtractionFact(BaseModel):
    """One extracted memory fact with target memory tier."""

    content: str
    tier: str

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, value: str) -> str:
        if value not in _ALLOWED_MEMORY_TIERS:
            raise ValueError("tier must be 'user_profile' or 'task_engagement'")
        return value


class ExtractionResult(BaseModel):
    """Structured extraction result containing zero or more facts."""

    facts: list[ExtractionFact]
    skipped_reason: str | None = None

    @field_validator("facts")
    @classmethod
    def _truncate_facts(cls, value: list[ExtractionFact]) -> list[ExtractionFact]:
        if len(value) > MEMORY_EXTRACTION_MAX_FACTS_PER_TURN:
            return value[:MEMORY_EXTRACTION_MAX_FACTS_PER_TURN]
        return value
