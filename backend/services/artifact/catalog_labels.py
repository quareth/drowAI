"""
Canonical artifact-catalog label rendering for API shaping and query filters.

This module owns the deterministic label contract shared by the artifact
catalog response and the catalog free-text query implementation so both paths
stay aligned.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import String, case, cast, func, literal
from sqlalchemy.sql.elements import ColumnElement


def build_artifact_catalog_label(
    *,
    artifact_kind: str,
    tool_name: str,
    turn_sequence: Optional[int],
    execution_id: str,
) -> str:
    """Return the visible deterministic artifact catalog label."""
    turn_or_execution = (
        f"turn {turn_sequence}"
        if isinstance(turn_sequence, int)
        else f"execution {str(execution_id)[:8]}"
    )
    return f"{artifact_kind} from {tool_name} ({turn_or_execution})"


def build_artifact_catalog_label_expression(
    *,
    artifact_kind: ColumnElement[Any],
    tool_name: ColumnElement[Any],
    turn_sequence: ColumnElement[Any],
    execution_id: ColumnElement[Any],
) -> ColumnElement[str]:
    """Return the SQL expression equivalent of the visible catalog label."""
    execution_prefix = func.substr(cast(func.coalesce(execution_id, literal("")), String), 1, 8)
    turn_or_execution = case(
        (turn_sequence.is_not(None), literal("turn ") + cast(turn_sequence, String)),
        else_=literal("execution ") + execution_prefix,
    )
    return (
        func.coalesce(artifact_kind, literal(""))
        + literal(" from ")
        + func.coalesce(tool_name, literal(""))
        + literal(" (")
        + turn_or_execution
        + literal(")")
    )
