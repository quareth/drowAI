"""Tests for shared mandatory/selectable skill catalog projection."""

from agent.subagents.registry import get_subagent_registry
from agent.subagents.skill_catalog import (
    MAX_SKILL_CATALOG_CHARACTERS,
    project_subagent_skill_catalogs,
)
from core.prompts.builders.intent_classifier import build_classifier_system_prompt
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from core.prompts.builders.subagent_catalog import (
    render_skill_catalog,
    render_subagent_catalog_section,
)
from core.skills.contracts import LoadedSkill, SkillActivationPolicy, SkillMetadata
from core.skills.registry import SkillRegistry


def test_catalog_separates_compatible_mandatory_and_selectable_skills() -> None:
    definitions = get_subagent_registry()
    catalogs = project_subagent_skill_catalogs(definitions.definitions(), _skill_registry())

    catalog = catalogs[0]
    assert catalog.agent_id == "pathfinder"
    assert tuple(skill.skill_id for skill in catalog.mandatory_skills) == ("baseline",)
    assert tuple(skill.skill_id for skill in catalog.selectable_skills) == (
        "network_reconnaissance",
    )

    rendered = build_classifier_system_prompt(
        subagent_catalog=definitions.classifier_catalog(),
        skill_catalogs=catalogs,
    )
    assert "Automatically included skills:" in rendered
    assert "Selectable skills for skill_ids:" in rendered
    assert "Request at most five listed selectable skills" in rendered
    assert "baseline" in rendered
    assert "network_reconnaissance" in rendered
    assert "other_agent_only" not in rendered


def test_followup_builder_uses_the_same_catalog_renderer() -> None:
    definitions = get_subagent_registry()
    catalogs = project_subagent_skill_catalogs(definitions.definitions(), _skill_registry())
    expected = render_subagent_catalog_section(definitions.classifier_catalog(), catalogs)

    prompt = PostToolReasoningPromptBuilder().build_user_prompt(
        interactive={
            "facts": {
                "message": "Continue bounded discovery.",
                "capability": "deep_reasoning",
                "metadata": {},
            }
        },
        synthesized={"tool": "nmap", "summary": "One host remains."},
        subagent_catalog=definitions.classifier_catalog(),
        skill_catalogs=catalogs,
    )

    assert expected in prompt
    assert prompt.count("network_reconnaissance") == 1


def test_catalog_budget_counts_both_sections() -> None:
    definitions = get_subagent_registry()
    catalog = project_subagent_skill_catalogs(
        definitions.definitions(), _many_skill_registry()
    )[0]

    assert len(catalog.mandatory_skills) + len(catalog.selectable_skills) < 12
    assert (
        len(render_skill_catalog(catalog.mandatory_skills, catalog.selectable_skills))
        <= MAX_SKILL_CATALOG_CHARACTERS
    )


def _skill_registry() -> SkillRegistry:
    return SkillRegistry(
        (
            _skill("baseline", activation="mandatory"),
            _skill("network_reconnaissance", activation="selectable", digest="3" * 64),
            _skill(
                "other_agent_only",
                activation="selectable",
                agent_ids=("other_agent",),
                digest="4" * 64,
            ),
        )
    )


def _many_skill_registry() -> SkillRegistry:
    return SkillRegistry(
        _skill(
            f"skill_{index:02d}",
            activation="mandatory" if index < 2 else "selectable",
            description="x" * 700,
            digest=f"{index + 1:064x}",
        )
        for index in range(12)
    )


def _skill(
    skill_id: str,
    *,
    activation: str,
    agent_ids: tuple[str, ...] = ("pathfinder",),
    description: str = "Bounded discovery guidance.",
    digest: str = "2" * 64,
) -> LoadedSkill:
    return LoadedSkill(
        metadata=SkillMetadata(name=skill_id, description=description),
        activation=SkillActivationPolicy(activation=activation, agent_ids=agent_ids),
        body="Use bounded discovery.",
        source=f"{skill_id}/SKILL.md",
        digest=digest,
    )
