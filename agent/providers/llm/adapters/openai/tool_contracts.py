"""OpenAI Chat request and tool translation primitives.

This module owns reusable Chat Completions request construction and OpenAI-
specific tool translation. It preserves exact wire model IDs and leaves
feature enablement to each adapter's validated policy.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

from ...contracts.tool_contracts import FunctionToolSpec, ToolChoice
from ...core.base import ToolCall


def build_openai_chat_request(
    *,
    wire_model_id: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: Any,
    max_tokens: Any,
    stream: bool = False,
    include_stream_usage: bool = False,
) -> Dict[str, Any]:
    """Build the common Chat Completions request without rewriting model IDs."""

    request: Dict[str, Any] = {
        "model": wire_model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        request["stream"] = True
    if include_stream_usage:
        request["stream_options"] = {"include_usage": True}
    return request


def build_openai_chat_tool_spec_from_function_spec(spec: FunctionToolSpec) -> Dict[str, Any]:
    """Translate a neutral function tool spec to the OpenAI Chat tool wrapper."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters_schema,
        },
    }


def normalize_openai_chat_tool_spec(tool: Any) -> Any:
    """Return an OpenAI Chat tool payload, preserving legacy dict inputs."""
    if isinstance(tool, FunctionToolSpec):
        return build_openai_chat_tool_spec_from_function_spec(tool)
    return tool


def normalize_openai_chat_tool_choice(choice: Any) -> Any:
    """Return an OpenAI Chat tool_choice payload, preserving legacy choices."""
    if not isinstance(choice, ToolChoice):
        return choice
    if choice.mode == "specific":
        return {
            "type": "function",
            "function": {"name": choice.function_name},
        }
    return choice.mode


def extract_openai_chat_tool_calls(message: Any) -> list[ToolCall] | None:
    """Normalize SDK tool calls from a Chat Completions message."""

    sdk_tool_calls = getattr(message, "tool_calls", None)
    if not sdk_tool_calls:
        return None
    return [
        ToolCall(
            id=tool_call.id,
            name=tool_call.function.name,
            arguments=tool_call.function.arguments,
        )
        for tool_call in sdk_tool_calls
    ]


__all__ = [
    "build_openai_chat_request",
    "build_openai_chat_tool_spec_from_function_spec",
    "extract_openai_chat_tool_calls",
    "normalize_openai_chat_tool_choice",
    "normalize_openai_chat_tool_spec",
]
