"""OpenAI tool payload translation for neutral LLM tool contracts.

This module owns OpenAI-specific tool wrapper construction. Neutral tool
contracts remain in ``tool_contracts.py``; adapters and legacy builders use
these helpers when they need OpenAI Chat Completions-compatible payloads.
"""

from __future__ import annotations

from typing import Any, Dict

from ...contracts.tool_contracts import FunctionToolSpec, ToolChoice


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


__all__ = [
    "build_openai_chat_tool_spec_from_function_spec",
    "normalize_openai_chat_tool_choice",
    "normalize_openai_chat_tool_spec",
]
