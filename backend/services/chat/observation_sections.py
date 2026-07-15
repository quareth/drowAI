"""Shared parsing and merge helpers for persisted observation section payloads.

This module centralizes observation-token parsing behavior used by runtime
persistence, message merging, and migration/backfill paths.
"""

from __future__ import annotations

import json
from typing import Any, List, Literal, Optional

NonListStrategy = Literal["empty", "raw", "dict_or_raw"]


def parse_observation_sections(
    value: Any,
    *,
    non_list_strategy: NonListStrategy,
    dict_only: bool = False,
) -> List[Any]:
    """Parse observation payload text into ordered section items.

    Args:
        value: Persisted observation payload text (or any value coercible to str).
        non_list_strategy:
            - ``"empty"``: return [] for non-list payloads/parse failures
            - ``"raw"``: return [raw_input_text] for non-list payloads/parse failures
            - ``"dict_or_raw"``: return [dict_payload] for JSON dict; otherwise [raw_input_text]
        dict_only: keep only dict items from the parsed list output.
    """
    if value is None:
        return []

    raw = str(value).strip()
    if not raw:
        return []

    parse_succeeded = False
    parsed: Any = None
    try:
        parsed = json.loads(raw)
        parse_succeeded = True
    except Exception:
        parse_succeeded = False

    sections: List[Any]
    if parse_succeeded and isinstance(parsed, list):
        sections = list(parsed)
    else:
        if non_list_strategy == "empty":
            sections = []
        elif non_list_strategy == "dict_or_raw" and parse_succeeded and isinstance(parsed, dict):
            sections = [parsed]
        else:
            sections = [raw]

    if not dict_only:
        return sections
    return [dict(item) for item in sections if isinstance(item, dict)]


def merge_observation_tokens(
    existing_tokens: Optional[str],
    incoming_tokens: Optional[str],
) -> Optional[str]:
    """Merge observation payloads so resume updates keep prior sections."""
    if incoming_tokens is None:
        return None

    incoming_sections = parse_observation_sections(
        str(incoming_tokens),
        non_list_strategy="raw",
    )
    if not incoming_sections:
        return existing_tokens

    existing_sections = parse_observation_sections(
        existing_tokens,
        non_list_strategy="raw",
    )
    if not existing_sections:
        return json.dumps(incoming_sections)

    existing_keys = [_observation_item_key(item) for item in existing_sections]
    incoming_keys = [_observation_item_key(item) for item in incoming_sections]

    # If one side is already a full-prefix superset of the other, keep the superset.
    if (
        len(existing_keys) <= len(incoming_keys)
        and existing_keys == incoming_keys[: len(existing_keys)]
    ):
        return json.dumps(incoming_sections)
    if (
        len(incoming_keys) <= len(existing_keys)
        and incoming_keys == existing_keys[: len(incoming_keys)]
    ):
        return json.dumps(existing_sections)

    merged_sections = list(existing_sections)
    merged_keys = list(existing_keys)
    for index, item in enumerate(incoming_sections):
        key = incoming_keys[index]
        if merged_keys and merged_keys[-1] == key:
            continue
        merged_sections.append(item)
        merged_keys.append(key)
    return json.dumps(merged_sections)


def _observation_item_key(item: Any) -> str:
    """Canonical key for section equality checks."""
    if isinstance(item, (dict, list)):
        try:
            return json.dumps(item, sort_keys=True)
        except Exception:
            return str(item)
    return str(item)
