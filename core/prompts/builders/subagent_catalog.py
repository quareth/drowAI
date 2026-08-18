"""Render the shared initial and follow-up subagent handoff catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.skills.contracts import SkillCatalogEntry, SubagentSkillCatalog


def render_skill_catalog(
    mandatory_skills: Sequence[SkillCatalogEntry],
    selectable_skills: Sequence[SkillCatalogEntry],
) -> str:
    """Render one agent's mandatory and parent-selectable skill sections."""

    lines = ["  Automatically included skills:"]
    lines.extend(
        (f"    - {skill.skill_id}: {skill.description}" for skill in mandatory_skills)
        if mandatory_skills
        else ("    - none",)
    )
    lines.append("  Selectable skills for skill_ids:")
    lines.extend(
        (f"    - {skill.skill_id}: {skill.description}" for skill in selectable_skills)
        if selectable_skills
        else ("    - none",)
    )
    return "\n".join(lines)


def render_subagent_catalog_section(
    subagent_catalog: Sequence[Mapping[str, Any]],
    skill_catalogs: Sequence[SubagentSkillCatalog] = (),
) -> str:
    """Render definition authority and per-agent selectable skill choices once."""

    skills_by_agent = {catalog.agent_id: catalog for catalog in skill_catalogs}
    lines = [
        "Registered Subagent Catalog:",
        (
            "- Select only a subagent listed below. The catalog is the authority "
            "for names and ownership boundaries."
        ),
        (
            "- Request at most five listed selectable skills that materially help the "
            "bounded objective; return an empty skill_ids list when none are needed."
        ),
    ]
    if not subagent_catalog:
        lines.append("- No subagents are currently available; return no handoffs.")
        return "\n".join(lines)

    for entry in subagent_catalog:
        name = str(entry.get("name") or "").strip()
        purpose = str(entry.get("purpose") or "").strip()
        ownership = str(entry.get("ownership_boundary") or "").strip()
        supported = _string_sequence(entry.get("supported_task_categories"))
        excluded = _string_sequence(entry.get("excluded_task_categories"))
        maximum = entry.get("max_active_runs_per_task")
        requires_target = bool(entry.get("requires_resolved_target"))
        if not name or not purpose or not ownership:
            raise ValueError("subagent catalog contains an incomplete specification")

        lines.extend(
            [
                f"- Name: {name}",
                f"  Purpose: {purpose}",
                f"  Ownership boundary: {ownership}",
                "  Supported task categories: "
                + (", ".join(supported) if supported else "none"),
                "  Excluded task categories: "
                + (", ".join(excluded) if excluded else "none"),
                f"  Maximum active runs per task: {maximum}",
                f"  Requires a resolved target: {'yes' if requires_target else 'no'}",
            ]
        )
        skill_catalog = skills_by_agent.get(name)
        lines.extend(
            render_skill_catalog(
                skill_catalog.mandatory_skills if skill_catalog else (),
                skill_catalog.selectable_skills if skill_catalog else (),
            ).splitlines()
        )
    return "\n".join(lines)


def _string_sequence(value: Any) -> tuple[str, ...]:
    """Return non-empty prompt tokens from one catalog sequence."""

    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(
        normalized
        for item in value
        if (normalized := str(item or "").strip())
    )


__all__ = ["render_skill_catalog", "render_subagent_catalog_section"]
