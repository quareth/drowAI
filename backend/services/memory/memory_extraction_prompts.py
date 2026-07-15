"""Build memory extraction prompt messages for gate and extraction calls.

This module owns prompt text and message construction only. It intentionally
contains no LLM invocation, storage, or database logic.
"""

from __future__ import annotations

GATE_MAX_INPUT_CHARS = 8000


def build_gate_classifier_messages(
    user_message: str,
    assistant_response: str,
) -> list[dict[str, str]]:
    """Build two-message chat payload for the memory extraction gate."""
    system_prompt = (
        "Determine whether the following exchange contains any of:\n"
        "- User preferences or constraints (output format, tool preferences, communication style)\n"
        "- Strategic decisions about approach\n"
        "- Engagement or environment context worth remembering across sessions\n"
        "Answer with structured output: { extractable: true/false }"
    )
    user_content = (
        f"User message:\n{user_message}\n\n"
        f"Assistant response:\n{assistant_response}"
    )[:GATE_MAX_INPUT_CHARS]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def build_extraction_messages(
    user_message: str,
    assistant_response: str,
) -> list[dict[str, str]]:
    """Build two-message chat payload for full memory fact extraction."""
    system_prompt = (
        "Extract memorable facts from this conversation exchange.\n\n"
        "EXTRACT as 'user_profile':\n"
        "- User preferences (output format, tool preferences, communication style)\n"
        "- User constraints (time limits, scope boundaries, rules of engagement)\n\n"
        "EXTRACT as 'task_engagement':\n"
        "- Strategic decisions ('focus on the web app first')\n"
        "- Engagement context ('the target network has a DMZ segment')\n"
        "- Environment observations from conversation (not from tool output)\n\n"
        "DO NOT EXTRACT:\n"
        "- Port scan results, service versions, CVEs\n"
        "- Tool output details (these go through the knowledge pipeline)\n"
        "- Generic technical facts unrelated to this engagement\n"
        "- Anything the user explicitly said to forget/ignore\n\n"
        "Return up to 5 facts. Each fact must be a single clear sentence."
    )
    user_content = (
        f"User message:\n{user_message}\n\n"
        f"Assistant response:\n{assistant_response}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
