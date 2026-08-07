"""Conditionally compose stable shell capability-selection instructions."""

from __future__ import annotations

from collections.abc import Iterable

from core.prompts.registry import PromptRegistry
from runtime_shared.shell_capabilities import MODEL_FACING_SHELL_START_TOOL_IDS


SHELL_UTILITY_PROFILE_PROMPT_ID = "shell_capability_profile_utility"
SHELL_ASSESSMENT_PROFILE_PROMPT_ID = "shell_capability_profile_assessment"


def build_shell_capability_profiles(
    callable_tool_ids: Iterable[object],
    *,
    prompt_registry: PromptRegistry | None = None,
) -> str:
    """Return both profiles once when either shell start alias is callable."""

    normalized_ids = {
        str(tool_id or "").strip() for tool_id in callable_tool_ids if tool_id
    }
    if MODEL_FACING_SHELL_START_TOOL_IDS.isdisjoint(normalized_ids):
        return ""

    registry = prompt_registry or PromptRegistry()
    utility = registry.get_template(SHELL_UTILITY_PROFILE_PROMPT_ID).strip()
    assessment = registry.get_template(SHELL_ASSESSMENT_PROFILE_PROMPT_ID).strip()
    return (
        "Shell Capability Profiles:\n"
        f"Utility profile:\n{utility}\n\n"
        f"Assessment profile:\n{assessment}"
    )


__all__ = [
    "SHELL_ASSESSMENT_PROFILE_PROMPT_ID",
    "SHELL_UTILITY_PROFILE_PROMPT_ID",
    "build_shell_capability_profiles",
]
