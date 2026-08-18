"""Validate declarative skill and subagent references before serving requests."""

from __future__ import annotations

from collections.abc import Iterable

from agent.subagents.registry import SubagentRegistry
from agent.tools.catalog_visibility import visible_available_tools
from core.skills.errors import SkillRegistryError
from core.skills.registry import SkillRegistry
from core.skills.resolver import resolve_skills


def validate_skill_activation_references(
    skill_registry: SkillRegistry,
    subagent_registry: SubagentRegistry,
    *,
    visible_tool_ids: Iterable[str] | None = None,
) -> None:
    """Fail when skill-agent or agent-tool references cannot resolve at startup."""

    definitions = subagent_registry.definitions()
    tools = tuple(
        visible_available_tools()
        if visible_tool_ids is None
        else _normalized_values(visible_tool_ids)
    )
    known_tool_ids = set(tools)
    known_agent_ids = {definition.id for definition in definitions}
    for definition in definitions:
        unresolved_tools = sorted(set(definition.tool_ids) - known_tool_ids)
        if unresolved_tools:
            raise SkillRegistryError(
                f"subagent {definition.id} has unavailable tool-ids: "
                f"{', '.join(unresolved_tools)}"
            )

    skills = skill_registry.skills()
    for skill in skills:
        unresolved_agents = sorted(set(skill.activation.agent_ids) - known_agent_ids)
        if unresolved_agents:
            raise SkillRegistryError(
                f"skill {skill.skill_id} has unresolved agent-ids: "
                f"{', '.join(unresolved_agents)}"
            )

    for definition in definitions:
        resolve_skills(skills, definition.id)


def _normalized_values(values: Iterable[str]) -> tuple[str, ...]:
    """Return stable non-empty identifiers for injected validation catalogs."""

    return tuple(
        value
        for item in values
        if (value := str(item or "").strip())
    )


__all__ = ["validate_skill_activation_references"]
