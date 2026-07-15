"""Shared utility for extracting JSON objects from noisy LLM text.

Handles common patterns: pure JSON, markdown-wrapped JSON, and JSON embedded
in prose. Uses brace-matching with string-escape awareness for robustness
against quoted braces.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

_MARKDOWN_JSON_PATTERN = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


def _extract_first_balanced_json_object(content: str) -> str:
    """Return the first balanced JSON object substring or empty string."""
    start_idx = content.find("{")
    if start_idx == -1:
        return ""

    depth = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(content[start_idx:], start=start_idx):
        if escape_next:
            escape_next = False
            continue

        if in_string and char == "\\":
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start_idx:i + 1]

    return ""


def extract_json_object(content: str) -> Dict[str, Any]:
    """Extract and parse the first JSON object from text content.

    Returns:
        Parsed JSON object when valid; otherwise an empty dict.
    """
    if not content or not isinstance(content, str):
        return {}

    text = content.strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    markdown_match = _MARKDOWN_JSON_PATTERN.search(text)
    if markdown_match:
        try:
            parsed = json.loads(markdown_match.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

    json_str = _extract_first_balanced_json_object(text)
    if not json_str:
        return {}

    try:
        parsed = json.loads(json_str)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
