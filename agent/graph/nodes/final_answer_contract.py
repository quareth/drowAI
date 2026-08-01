"""Validate structured final-answer sections and render user-facing output.

This module is the trust boundary between provider-generated finalizer data and
assistant-visible text. Provider response prose is never rendered directly;
only the four validated section values accepted here may reach the event stream.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from typing import Any

from agent.providers.llm.core.exceptions import LLMResponseError


_SECTION_HEADINGS: tuple[tuple[str, str], ...] = (
    ("action", "Action"),
    ("findings", "Findings"),
    ("impact", "Impact"),
    ("recommended_next_action", "Recommended Next Action"),
)
_FINAL_ANSWER_FIELDS = tuple(field for field, _heading in _SECTION_HEADINGS)
_TOP_LEVEL_HEADING_RE = re.compile(r"(?m)^#{1,2}\s+")


def _validated_sections(payload: Any) -> dict[str, str]:
    """Return canonical section text or reject an invalid provider payload."""
    if not isinstance(payload, Mapping):
        raise LLMResponseError("Finalizer response did not contain structured sections.")

    unexpected = set(payload) - set(_FINAL_ANSWER_FIELDS)
    missing = set(_FINAL_ANSWER_FIELDS) - set(payload)
    if missing or unexpected:
        raise LLMResponseError(
            "Finalizer structured sections did not match the final-answer contract."
        )

    sections: dict[str, str] = {}
    for field in _FINAL_ANSWER_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise LLMResponseError(
                f"Finalizer structured section '{field}' was empty or invalid."
            )
        text = value.strip()
        if _TOP_LEVEL_HEADING_RE.search(text):
            raise LLMResponseError(
                f"Finalizer structured section '{field}' contained a reserved heading."
            )
        sections[field] = text
    return sections


def render_final_answer(payload: Any, *, output_format: str | None = None) -> str:
    """Render validated final-answer sections in a deterministic user format."""
    sections = _validated_sections(payload)
    normalized_format = str(output_format or "markdown").strip().lower()

    if normalized_format == "json":
        return f"```json\n{json.dumps(sections, indent=2, ensure_ascii=False)}\n```"

    if normalized_format == "csv":
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(("section", "content"))
        for field, heading in _SECTION_HEADINGS:
            writer.writerow((heading, sections[field]))
        return f"```csv\n{buffer.getvalue().rstrip()}\n```"

    return "\n\n".join(
        f"## {heading}\n{sections[field]}"
        for field, heading in _SECTION_HEADINGS
    )


__all__ = ["render_final_answer"]
