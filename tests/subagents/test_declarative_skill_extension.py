"""Prove file-only subagent and skill extension through the public registries."""

from pathlib import Path

from agent.subagents.registry import load_subagent_registry
from agent.subagents.skill_catalog import project_subagent_skill_catalogs
from agent.subagents.skill_validation import validate_skill_activation_references
from core.prompts.builders.skill_guidance import PromptSkill
from core.prompts.builders.subagent_runtime import SubagentRuntimePromptBuilder
from core.skills.registry import load_skill_registry
from core.skills.resolver import resolve_skills


TOOL_ID = "information_gathering.network_discovery.nmap"


def test_new_agent_and_skill_files_work_without_registration_edits(tmp_path: Path) -> None:
    definitions_root = tmp_path / "definitions"
    skills_root = tmp_path / "skills"
    definitions_root.mkdir()
    skills_root.mkdir()
    _write_agent(definitions_root / "future_agent.toml")
    _write_skill(skills_root / "future_skill" / "SKILL.md")

    subagents = load_subagent_registry(definitions_root)
    skills = load_skill_registry(skills_root)
    validate_skill_activation_references(
        skills,
        subagents,
        visible_tool_ids=(TOOL_ID,),
    )

    definition = subagents.require("future_agent")
    catalog = project_subagent_skill_catalogs((definition,), skills)[0]
    assert tuple(entry.skill_id for entry in catalog.selectable_skills) == (
        "future_skill",
    )

    resolution = resolve_skills(skills.skills(), definition.id, ("future_skill",))
    loaded = skills.materialize(resolution.selected)
    prompt = SubagentRuntimePromptBuilder().build_system_prompt(
        definition_id=definition.id,
        display_name=definition.display_name,
        role_prompt=definition.runtime_role_prompt or definition.instructions,
        definition_instructions=definition.instructions,
        ownership_boundary=definition.ownership_boundary,
        boundary_rules=definition.runtime_boundary_rules,
        max_committed_tools_per_batch=definition.max_tool_calls_per_iteration,
        callable_tool_ids=definition.tool_ids,
        prompt_skills=tuple(
            PromptSkill(
                skill_id=skill.skill_id,
                description=skill.metadata.description,
                body=skill.body,
            )
            for skill in loaded
        ),
    )

    assert '<skill id="future_skill">' in prompt
    assert "applicable to the assigned task" in prompt
    assert "When no provided native tool is applicable" in prompt
    assert "use the assessment shell" in prompt


def _write_agent(path: Path) -> None:
    path.write_text(
        f'''schema_version = 1
id = "future_agent"
display_name = "Future Agent"
kind = "recon"
description = "Perform one bounded future reconnaissance responsibility."
ownership_boundary = "Own only the assigned bounded reconnaissance objective."
supported_task_categories = ["port_scanning"]
excluded_task_categories = ["exploitation"]
tool_ids = ["{TOOL_ID}"]
enabled = true
max_active_runs_per_task = 1
max_iterations = 3
max_tool_calls_per_iteration = 2
requires_resolved_target = true
icon = "future_agent"
instructions = "Perform the bounded assignment and report evidence to the parent."
runtime_role_prompt = "You are a bounded reconnaissance subagent."
runtime_boundary_rules = ["Stay within the assignment target and scope."]
''',
        encoding="utf-8",
    )


def _write_skill(path: Path) -> None:
    path.parent.mkdir()
    path.write_text(
        '''---
name: future_skill
description: Operate one future shell-based reconnaissance program safely.
metadata:
  version: "1"
  activation: "selectable"
  agent-ids: "future_agent"
---

# Guidance

Use the assessment shell only for the bounded workflow described here.
''',
        encoding="utf-8",
    )
