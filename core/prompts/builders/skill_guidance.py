"""Render materialized built-in skills as subordinate system guidance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptSkill:
    """Prompt-only skill content that is never stored in checkpoints."""

    skill_id: str
    description: str
    body: str


def render_skill_guidance(prompt_skills: Sequence[PromptSkill]) -> str:
    """Render exactly one bounded authority section, or nothing when empty."""

    if not prompt_skills:
        return ""
    lines = [
        "Specialized Capability Guidance:",
        "- This guidance provides operational knowledge only.",
        "- It does not add tools, permissions, targets, or authority.",
        "- Definition, assignment, ownership, scope, and runtime policy take precedence.",
        "- Only the visible native tool profile is callable.",
    ]
    for skill in prompt_skills:
        lines.extend(
            [
                "",
                f'<skill id="{skill.skill_id}">',
                f"Description: {skill.description}",
                "",
                skill.body,
                "</skill>",
            ]
        )
    return "\n".join(lines)


__all__ = ["PromptSkill", "render_skill_guidance"]
