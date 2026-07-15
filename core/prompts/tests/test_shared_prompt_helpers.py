"""Tests for shared prompt helper leaf modules.

These tests lock the extracted route-label and todo-formatting helpers
so prompt-builder refactors keep one shared behavior authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.prompts.builders._todo_formatting import (
    extract_todo_description,
    extract_todo_status,
    normalize_progress_status,
    to_progress_marker,
)
from core.prompts.route_labels import llm_facing_route_label


@dataclass
class _TodoObject:
    description: str
    status: object


@dataclass
class _EnumLikeStatus:
    value: str


def test_llm_facing_route_label_matches_prompt_aliases() -> None:
    assert llm_facing_route_label("simple_tool_execution") == "direct_executor"
    assert llm_facing_route_label("deep_reasoning") == "plan_executor"
    assert llm_facing_route_label("normal_chat") == "simple_chat"
    assert llm_facing_route_label("respond_only") == "simple_chat"
    assert llm_facing_route_label("custom_route") == "custom_route"


def test_normalize_progress_status_preserves_prompt_mapping() -> None:
    assert normalize_progress_status("in_progress") == "in_progress"
    assert normalize_progress_status("complete_positive") == "completed"
    assert normalize_progress_status("complete_negative") == "completed"
    assert normalize_progress_status("completed") == "completed"
    assert normalize_progress_status("skipped") == "skipped"
    assert normalize_progress_status("exhausted") == "skipped"
    assert normalize_progress_status("unknown") == "pending"


def test_extract_todo_description_supports_string_dict_and_object_shapes() -> None:
    assert extract_todo_description("scan host") == "scan host"
    assert extract_todo_description({"description": "scan ports"}) == "scan ports"
    assert extract_todo_description({"text": "read artifact"}) == "read artifact"
    assert extract_todo_description(_TodoObject(description="enumerate http", status="pending")) == (
        "enumerate http"
    )


def test_extract_todo_status_supports_dict_object_and_enum_like_values() -> None:
    assert extract_todo_status({"status": "in_progress"}) == "in_progress"
    assert (
        extract_todo_status(_TodoObject(description="scan", status=_EnumLikeStatus("complete_positive")))
        == "completed"
    )
    assert to_progress_marker("in_progress") == "[in_progress]"
    assert to_progress_marker("completed") == "[completed]"
    assert to_progress_marker("skipped") == "[skipped]"
    assert to_progress_marker("pending") == "[pending]"
