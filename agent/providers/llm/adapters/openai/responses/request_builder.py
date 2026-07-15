"""Build OpenAI Responses API request payloads for the GPT-5 provider.

This module owns provider-local payload shaping for message history, tool
definitions, tool choice normalization, and common request kwargs builders.
It is intentionally pure and does not perform logging, retries, or SDK calls.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ....contracts.tool_contracts import FunctionToolSpec, ToolChoice


def convert_messages_to_input(
    messages: List[Dict[str, Any]],
) -> tuple[str, List[Dict[str, Any]]]:
    """Convert standard messages to Responses API input format."""
    system_prompt = ""
    input_messages: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
                and part.get("type") in ("text", "input_text", "output_text")
            ]
            content = " ".join(text_parts)

        if not isinstance(content, str):
            content = str(content or "")

        if role == "system":
            system_prompt = content
        elif role == "user":
            input_messages.append(
                {"role": "user", "content": [{"type": "input_text", "text": content}]}
            )
        elif role == "assistant":
            input_messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            )

    return system_prompt, input_messages


def convert_tools_for_responses(
    tools: List[Any],
) -> List[Dict[str, Any]]:
    """Convert neutral or legacy Chat tools into Responses API format."""
    converted: List[Dict[str, Any]] = []

    for tool in tools or []:
        if isinstance(tool, FunctionToolSpec):
            converted.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                }
            )
            continue

        if not isinstance(tool, dict):
            converted.append(tool)
            continue

        if tool.get("type") == "function" and "name" in tool:
            converted.append(tool)
        elif tool.get("type") == "function" and "function" in tool:
            func = tool.get("function") or {}
            converted.append(
                {
                    "type": "function",
                    "name": func.get("name"),
                    "description": func.get("description"),
                    "parameters": func.get("parameters") or {},
                }
            )
        else:
            converted.append(tool)

    return converted


def convert_tool_choice(choice: Any) -> Any:
    """Convert tool_choice to Responses API format."""
    if isinstance(choice, ToolChoice):
        if choice.mode == "specific":
            return {"type": "function", "name": choice.function_name}
        return choice.mode
    if isinstance(choice, dict) and choice.get("type") == "function":
        func = choice.get("function") or {}
        name = func.get("name") or choice.get("name")
        return {"type": "function", "name": name} if name else {"type": "function"}
    return choice


def build_chat_request_kwargs(
    *,
    model: str,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: Any,
    reasoning_effort: str,
) -> Dict[str, Any]:
    """Build request kwargs for single-turn chat calls."""
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            }
        ],
        "instructions": system_prompt or "",
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": reasoning_effort},
    }


def build_chat_messages_request_kwargs(
    *,
    model: str,
    input_messages: List[Dict[str, Any]],
    system_prompt: str,
    max_output_tokens: Any,
    reasoning_effort: str,
) -> Dict[str, Any]:
    """Build request kwargs for history-based chat calls."""
    return {
        "model": model,
        "input": input_messages,
        "instructions": system_prompt,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": reasoning_effort},
    }


def build_tool_request_kwargs(
    *,
    model: str,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: Any,
    reasoning_effort: str,
    responses_tools: List[Dict[str, Any]],
    tool_choice: Any,
    parallel_tool_calls: Any = None,
) -> Dict[str, Any]:
    """Build request kwargs for tool-calling Responses API calls."""
    request_kwargs: Dict[str, Any] = build_chat_request_kwargs(
        model=model,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
    )
    if responses_tools:
        request_kwargs["tools"] = responses_tools
    if tool_choice is not None:
        request_kwargs["tool_choice"] = convert_tool_choice(tool_choice)
    if parallel_tool_calls is not None:
        request_kwargs["parallel_tool_calls"] = parallel_tool_calls
    return request_kwargs
