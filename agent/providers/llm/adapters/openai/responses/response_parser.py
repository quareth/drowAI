"""Parse Responses API outputs for the GPT-5 provider.

This module owns provider-local extraction of text, structured content, tool
calls, and usage data from OpenAI Responses API response objects.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from ....core.base import ToolCall


def coerce_text_fragment(value: Any) -> Optional[str]:
    """Normalize text fragments from SDK response objects."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        candidate = value.get("value") or value.get("text")
        if isinstance(candidate, str):
            return candidate
    candidate = getattr(value, "value", None)
    if isinstance(candidate, str):
        return candidate
    candidate = getattr(value, "text", None)
    if isinstance(candidate, str):
        return candidate
    return None


def extract_output_text(response: Any, logger: logging.Logger) -> Optional[str]:
    """Extract text from Responses API response."""
    content = getattr(response, "output_text", None)
    if content:
        logger.debug(f"Extracted text from output_text attribute: {len(content)} chars")
        return str(content)

    try:
        output = getattr(response, "output", None)
        if output:
            pieces: List[str] = []
            logger.debug(
                f"Checking {len(output) if hasattr(output, '__len__') else '?'} output items"
            )

            for idx, item in enumerate(output):
                item_type = (
                    item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
                )
                logger.debug(f"Output item {idx}: type={item_type}")

                if item_type in ("reasoning", "thinking"):
                    logger.debug(f"Skipping reasoning item {idx}")
                    continue

                if item_type == "message":
                    msg_content = (
                        item.get("content")
                        if isinstance(item, dict)
                        else getattr(item, "content", None)
                    )
                    if msg_content:
                        for part in msg_content:
                            part_text = (
                                part.get("text")
                                if isinstance(part, dict)
                                else getattr(part, "text", None)
                            )
                            normalized_part_text = coerce_text_fragment(part_text)
                            if normalized_part_text:
                                pieces.append(normalized_part_text)
                    continue

                item_content = (
                    item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
                )
                if item_content:
                    for part in item_content:
                        part_type = (
                            part.get("type")
                            if isinstance(part, dict)
                            else getattr(part, "type", None)
                        )
                        part_text = (
                            part.get("text")
                            if isinstance(part, dict)
                            else getattr(part, "text", None)
                        )
                        if part_type in ("text", "output_text"):
                            normalized_part_text = coerce_text_fragment(part_text)
                            if normalized_part_text:
                                pieces.append(normalized_part_text)

                item_text = (
                    item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                )
                if item_text and isinstance(item_text, str):
                    pieces.append(item_text)

            if pieces:
                result = "".join(pieces)
                logger.debug(f"Extracted text from output items: {len(result)} chars")
                return result

    except Exception as exc:
        logger.debug(f"Failed to extract output from response.output: {exc}")

    try:
        choices = getattr(response, "choices", None)
        if choices and len(choices) > 0:
            first_choice = choices[0]
            message = getattr(first_choice, "message", None)
            if message:
                msg_content = getattr(message, "content", None)
                if msg_content:
                    logger.debug(
                        f"Extracted text from choices[0].message.content: {len(msg_content)} chars"
                    )
                    return str(msg_content)
    except Exception as exc:
        logger.debug(f"Failed to extract from choices: {exc}")

    logger.warning("Could not extract output text from response")
    return None


def extract_structured_content_text(
    response: Any,
    logger: logging.Logger,
) -> Optional[str]:
    """Extract JSON text for structured-output responses when output_text is absent."""
    for candidate_attr in ("output_parsed", "parsed"):
        candidate = getattr(response, candidate_attr, None)
        if isinstance(candidate, dict):
            return json.dumps(candidate, separators=(",", ":"))

    output = getattr(response, "output", None)
    if not output:
        return None

    try:
        for item in output:
            item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if item_type != "message":
                continue
            msg_content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            if not msg_content:
                continue
            for part in msg_content:
                parsed = part.get("parsed") if isinstance(part, dict) else getattr(part, "parsed", None)
                if isinstance(parsed, dict):
                    return json.dumps(parsed, separators=(",", ":"))
                text_value = coerce_text_fragment(
                    part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                )
                if text_value and text_value.strip().startswith("{"):
                    return text_value
    except Exception as exc:
        logger.debug(f"Failed structured output extraction from response.output: {exc}")

    return None


def extract_tool_calls(response: Any, logger: logging.Logger) -> Optional[List[ToolCall]]:
    """Extract tool calls from Responses API response."""
    tool_calls: List[ToolCall] = []

    try:
        output = getattr(response, "output", None)
        if not output:
            return None

        for item in output:
            item_type = (
                item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            )

            if item_type == "function_call":
                call_id = (
                    item.get("call_id") if isinstance(item, dict) else getattr(item, "call_id", "")
                )
                name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
                arguments = (
                    item.get("arguments") if isinstance(item, dict) else getattr(item, "arguments", "{}")
                )

                if name:
                    tool_calls.append(
                        ToolCall(
                            id=str(call_id) if call_id else "",
                            name=str(name),
                            arguments=str(arguments) if arguments else "{}",
                        )
                    )
                continue

            item_content = (
                item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            )
            if item_content:
                for part in item_content:
                    part_type = (
                        part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                    )
                    if part_type in ("tool_call", "function_call"):
                        func_name = (
                            part.get("name")
                            if isinstance(part, dict)
                            else getattr(part, "name", None)
                        )
                        func_args = (
                            part.get("arguments")
                            if isinstance(part, dict)
                            else getattr(part, "arguments", None)
                        )
                        call_id = (
                            part.get("id") if isinstance(part, dict) else getattr(part, "id", "")
                        )

                        if not func_name:
                            func_obj = (
                                part.get("function")
                                if isinstance(part, dict)
                                else getattr(part, "function", None)
                            )
                            if func_obj:
                                func_name = (
                                    func_obj.get("name")
                                    if isinstance(func_obj, dict)
                                    else getattr(func_obj, "name", None)
                                )
                                func_args = func_args or (
                                    func_obj.get("arguments")
                                    if isinstance(func_obj, dict)
                                    else getattr(func_obj, "arguments", None)
                                )

                        if func_name:
                            tool_calls.append(
                                ToolCall(
                                    id=str(call_id) if call_id else "",
                                    name=str(func_name),
                                    arguments=str(func_args) if func_args else "{}",
                                )
                            )

    except Exception as exc:
        logger.debug(f"Failed to extract tool calls: {exc}")
        return None

    return tool_calls if tool_calls else None


def extract_usage_from_response(
    response: Any,
    *,
    model: str,
    usage_tracking_available: bool,
    usage_data_cls: Any,
) -> Optional[Any]:
    """Extract UsageData from Responses API response when tracking is available."""
    if not usage_tracking_available or usage_data_cls is None:
        return None
    return usage_data_cls.from_openai_responses_api(response, model)
