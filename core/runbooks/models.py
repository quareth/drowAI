"""Typed contracts for validated runbook metadata and loaded runbook content."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RunbookType(str, Enum):
    """Supported runbook ownership/use categories."""

    TOOL = "tool"
    PROCEDURE = "procedure"


class RunbookStage(str, Enum):
    """Prompt or planning stage where runbook content may be rendered."""

    INTENT = "intent"
    PLANNER = "planner"
    TOOL_SELECTION = "tool_selection"
    TOOL_PARAMETERS = "tool_parameters"
    POST_TOOL = "post_tool"
    FINAL_SUMMARY = "final_summary"


class RunbookMetadata(BaseModel):
    """Validated frontmatter fields shared by all bundled runbooks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    type: RunbookType
    version: int = Field(gt=0)
    description: str
    trigger_tool_ids: tuple[str, ...] = Field(default_factory=tuple)
    trigger_category_ids: tuple[str, ...] = Field(default_factory=tuple)
    stages: tuple[RunbookStage, ...] = Field(min_length=1)

    @field_validator("id", "name", "description")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("trigger_tool_ids", "trigger_category_ids")
    @classmethod
    def _normalize_trigger_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ids = tuple(item.strip() for item in value if item.strip())
        if len(ids) != len(value):
            raise ValueError("trigger ids must not contain empty values")
        return ids

    @model_validator(mode="after")
    def _validate_tool_runbook_triggers(self) -> "RunbookMetadata":
        if (
            self.type is RunbookType.TOOL
            and not self.trigger_tool_ids
            and not self.trigger_category_ids
        ):
            raise ValueError("tool runbooks require trigger_tool_ids or trigger_category_ids")
        return self


class LoadedRunbook(RunbookMetadata):
    """Validated runbook metadata plus markdown instructions."""

    body: str

    @field_validator("body")
    @classmethod
    def _non_empty_body(cls, value: str) -> str:
        body = value.strip()
        if not body:
            raise ValueError("body must not be empty")
        return body


__all__ = [
    "LoadedRunbook",
    "RunbookMetadata",
    "RunbookStage",
    "RunbookType",
]
