"""Tests for startup validation of declarative agent and skill references."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent.subagents.definition import load_subagent_definitions
from agent.subagents.registry import SubagentRegistry
from agent.subagents.skill_validation import validate_skill_activation_references
from core.skills.contracts import LoadedSkill, SkillActivationPolicy, SkillMetadata
from core.skills.errors import SkillRegistryError, SkillResolutionError
from core.skills.registry import SkillRegistry


def test_startup_validation_accepts_direct_agent_and_visible_tool_references() -> None:
    subagents = _subagents()
    validate_skill_activation_references(
        SkillRegistry((_skill(),)),
        subagents,
        visible_tool_ids=_definition_tool_ids(subagents),
    )


def test_startup_validation_rejects_unknown_agent_reference() -> None:
    skill = _skill(agent_ids=("missing",))

    with pytest.raises(SkillRegistryError, match="unresolved agent-ids: missing"):
        validate_skill_activation_references(
            SkillRegistry((skill,)),
            _subagents(),
            visible_tool_ids=_definition_tool_ids(_subagents()),
        )


def test_startup_validation_rejects_unavailable_subagent_tool() -> None:
    pathfinder = _subagents().require("pathfinder")
    invalid = replace(pathfinder, tool_ids=(*pathfinder.tool_ids, "missing.tool"))

    with pytest.raises(SkillRegistryError, match="unavailable tool-ids: missing.tool"):
        validate_skill_activation_references(
            SkillRegistry(()),
            SubagentRegistry((invalid,)),
            visible_tool_ids=pathfinder.tool_ids,
        )


def test_startup_validation_rejects_mandatory_prompt_budget_overflow() -> None:
    skills = SkillRegistry(
        tuple(
            _skill(skill_id=f"required-{index}", body="x" * 20_000)
            for index in range(3)
        )
    )

    with pytest.raises(SkillResolutionError, match="mandatory skill guidance exceeds budget"):
        validate_skill_activation_references(
            skills,
            _subagents(),
            visible_tool_ids=_definition_tool_ids(_subagents()),
        )


def _subagents() -> SubagentRegistry:
    return SubagentRegistry(load_subagent_definitions())


def _definition_tool_ids(registry: SubagentRegistry) -> tuple[str, ...]:
    return tuple(
        tool_id
        for definition in registry.definitions()
        for tool_id in definition.tool_ids
    )


def _skill(
    *,
    skill_id: str = "validation-fixture",
    activation: str = "mandatory",
    agent_ids: tuple[str, ...] = ("pathfinder",),
    body: str = "Use bounded fixture guidance.",
) -> LoadedSkill:
    return LoadedSkill(
        metadata=SkillMetadata(name=skill_id, description="Validate references."),
        activation=SkillActivationPolicy(activation=activation, agent_ids=agent_ids),
        body=body,
        source=f"{skill_id}/SKILL.md",
        digest=(skill_id.encode().hex() + "0" * 64)[:64],
    )
