"""Shared exception and validation models for tool execution."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolExecutionError(Exception):
    """Raised when a tool execution path fails before producing a ToolResult."""


class ToolValidationError(BaseModel):
    """Represents a single validation error for tool input."""

    field: str = Field(..., description="The field name that failed validation")
    error: str = Field(..., description="Human-readable validation error message")
    suggested_fix: str = Field(
        ..., description="Suggested correction for the LLM to retry"
    )
