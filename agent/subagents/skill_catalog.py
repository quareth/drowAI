"""Project bounded skill catalogs from direct agent compatibility metadata."""

from __future__ import annotations

from collections.abc import Iterable

from agent.subagents.definition import SubagentDefinition
from core.prompts.builders.subagent_catalog import render_skill_catalog
from core.skills.contracts import (
    SkillCatalogEntry,
    SubagentSkillCatalog,
)
from core.skills.registry import SkillRegistry
from core.skills.resolver import (
    compatible_mandatory_skills,
    eligible_selectable_skills,
)


MAX_SKILL_CATALOG_ENTRIES = 12
MAX_SKILL_CATALOG_CHARACTERS = 6_000


def project_subagent_skill_catalogs(
    definitions: Iterable[SubagentDefinition],
    skill_registry: SkillRegistry,
) -> tuple[SubagentSkillCatalog, ...]:
    """Return one bounded mandatory/selectable projection per definition."""

    catalogs: list[SubagentSkillCatalog] = []
    for definition in definitions:
        mandatory_entries: list[SkillCatalogEntry] = []
        selectable_entries: list[SkillCatalogEntry] = []
        candidates = (
            (
                mandatory_entries,
                compatible_mandatory_skills(skill_registry.skills(), definition.id),
            ),
            (
                selectable_entries,
                eligible_selectable_skills(skill_registry.skills(), definition.id),
            ),
        )
        for destination, skills in candidates:
            for skill in skills:
                if len(mandatory_entries) + len(selectable_entries) >= MAX_SKILL_CATALOG_ENTRIES:
                    break
                entry = SkillCatalogEntry(
                    skill_id=skill.skill_id,
                    description=skill.metadata.description,
                )
                destination.append(entry)
                if len(render_skill_catalog(mandatory_entries, selectable_entries)) > MAX_SKILL_CATALOG_CHARACTERS:
                    destination.pop()
                    break
        catalogs.append(
            SubagentSkillCatalog(
                agent_id=definition.id,
                mandatory_skills=tuple(mandatory_entries),
                selectable_skills=tuple(selectable_entries),
            )
        )
    return tuple(catalogs)


__all__ = [
    "MAX_SKILL_CATALOG_CHARACTERS",
    "MAX_SKILL_CATALOG_ENTRIES",
    "project_subagent_skill_catalogs",
]
