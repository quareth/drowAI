"""Tests for provider-neutral LLM tool contract types."""

from __future__ import annotations

import pytest

from agent.providers.llm.contracts.tool_contracts import (
    FunctionToolSpec,
    ToolChoice,
    function_tool_spec_from_openai_dict,
)


def test_function_tool_spec_preserves_internal_and_provider_names() -> None:
    spec = FunctionToolSpec(
        tool_id="net.nmap",
        name="tool__net_nmap",
        description="Run nmap",
        parameters_schema={"type": "object", "properties": {}},
    )

    assert spec.tool_id == "net.nmap"
    assert spec.name == "tool__net_nmap"
    assert spec.parameters_schema == {"type": "object", "properties": {}}


def test_function_tool_spec_rejects_empty_identity() -> None:
    with pytest.raises(ValueError, match="tool_id cannot be empty"):
        FunctionToolSpec(
            tool_id=" ",
            name="tool__empty",
            description="",
            parameters_schema={},
        )


def test_tool_choice_accepts_standard_modes() -> None:
    assert ToolChoice("auto").mode == "auto"
    assert ToolChoice("none").mode == "none"
    assert ToolChoice("required").mode == "required"
    assert ToolChoice("specific", function_name="tool__net_nmap").function_name == "tool__net_nmap"


def test_tool_choice_specific_requires_function_name() -> None:
    with pytest.raises(ValueError, match="function_name cannot be empty"):
        ToolChoice("specific")


def test_tool_choice_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        ToolChoice("invalid")  # type: ignore[arg-type]


def test_tool_choice_rejects_non_string_mode() -> None:
    with pytest.raises(TypeError, match="mode must be a string"):
        ToolChoice(None)  # type: ignore[arg-type]


def test_legacy_openai_chat_tool_dict_can_be_coerced_to_neutral_spec() -> None:
    spec = function_tool_spec_from_openai_dict(
        {
            "type": "function",
            "function": {
                "name": "tool__net_nmap",
                "description": "Run nmap",
                "parameters": {"type": "object"},
            },
        },
        tool_id="net.nmap",
    )

    assert spec == FunctionToolSpec(
        tool_id="net.nmap",
        name="tool__net_nmap",
        description="Run nmap",
        parameters_schema={"type": "object"},
    )


def test_legacy_openai_responses_tool_dict_can_be_coerced_to_neutral_spec() -> None:
    spec = function_tool_spec_from_openai_dict(
        {
            "type": "function",
            "name": "tool__net_nmap",
            "description": "Run nmap",
            "parameters": {"type": "object"},
        }
    )

    assert spec.tool_id == "tool__net_nmap"
    assert spec.name == "tool__net_nmap"
    assert spec.description == "Run nmap"
    assert spec.parameters_schema == {"type": "object"}
