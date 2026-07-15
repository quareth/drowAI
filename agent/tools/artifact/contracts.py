"""Pydantic contracts for artifact memory tools.

This module owns artifact tool argument schemas and artifact-tool-local type
aliases only. It does not open database sessions, call backend artifact
services, format stdout, or resolve active task context.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ArtifactReadMode = Literal["auto", "head", "tail", "match", "full"]
"""Read mode options for artifact memory retrieval."""


class ArtifactSearchArgs(BaseModel):
    """Arguments for `artifact.search` task-scoped discovery."""

    query: Optional[str] = Field(
        None,
        description="Optional free-text query used to narrow artifact discovery.",
    )
    tool_name: Optional[str] = Field(
        None,
        description="Optional exact tool name filter (for example: shell.exec).",
    )
    artifact_kind: Optional[str] = Field(
        None,
        description="Optional artifact kind filter (for example: stdout, stderr, tool_file).",
    )
    execution_id: Optional[str] = Field(
        None,
        description="Optional execution UUID filter when one execution is already known.",
    )
    turn_id: Optional[str] = Field(
        None,
        description="Optional turn identifier filter for conversation-scoped narrowing.",
    )
    conversation_id: Optional[str] = Field(
        None,
        description="Optional conversation identifier filter.",
    )
    limit: int = Field(
        20,
        ge=1,
        le=1000,
        description="Maximum number of catalog rows to return.",
    )
    offset: int = Field(
        0,
        ge=0,
        description="Pagination offset for artifact catalog browsing.",
    )


class ArtifactReadArgs(BaseModel):
    """Arguments for `artifact.read` bounded artifact retrieval."""

    artifact_id: str = Field(
        ...,
        description="Artifact UUID to read. Must come from internal artifact catalog state or recent refs.",
    )
    mode: ArtifactReadMode = Field(
        "auto",
        description="Read strategy. Defaults to excerpt-first `auto`.",
    )
    query: Optional[str] = Field(
        None,
        description="Optional query used by `match` mode to center the excerpt window.",
    )
    max_chars: int = Field(
        4000,
        ge=1,
        le=20000,
        description="Maximum characters returned from a single read call.",
    )
