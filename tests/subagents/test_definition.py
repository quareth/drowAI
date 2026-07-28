"""Tests for declarative subagent definition loading and validation."""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

import pytest

from agent.subagents.definition import (
    SubagentDefinitionError,
    load_subagent_definition_file,
    load_subagent_definitions,
)


VALID_DEFINITION = """
schema_version = 1
id = "pathfinder"
display_name = "Pathfinder"
kind = "recon"
description = "Bounded network reconnaissance agent."
ownership_boundary = "Own host discovery, port scanning, and service enumeration only."
supported_task_categories = ["host_discovery", "port_scanning"]
excluded_task_categories = ["exploitation"]
tool_ids = [
  "information_gathering.network_discovery.fping",
  "information_gathering.network_discovery.nmap",
]
enabled = true
max_active_runs_per_task = 1
max_iterations = 3
max_tool_calls_per_iteration = 3
requires_resolved_target = true
icon = "pathfinder"
instructions = "Own only the assigned reconnaissance objective."
"""


def _write_definition(directory: Path, name: str, body: str = VALID_DEFINITION) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_loads_builtin_pathfinder_definition_from_package_data() -> None:
    definitions = load_subagent_definitions()

    assert [definition.id for definition in definitions] == ["pathfinder"]
    pathfinder = definitions[0]
    assert pathfinder.schema_version == 1
    assert pathfinder.display_name == "Pathfinder"
    assert pathfinder.kind == "recon"
    assert pathfinder.enabled is True
    assert pathfinder.max_active_runs_per_task == 1
    assert pathfinder.max_iterations == 3
    assert pathfinder.max_tool_calls_per_iteration == 3
    assert pathfinder.requires_resolved_target is True
    assert pathfinder.icon == "pathfinder"
    assert pathfinder.runtime_role_prompt == (
        "You are Pathfinder, a bounded recon subagent.\n"
        "Use native tool calls when more evidence is needed; otherwise return a "
        "concise parent handoff."
    )
    assert pathfinder.runtime_boundary_rules == (
        "Use only the targets, objective, scope, and constraints in the assignment "
        "context.",
        "Do not exploit, authenticate, mutate files, run shells, manage agents, or "
        "request credentials.",
    )
    assert pathfinder.tool_ids == (
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
    )
    assert "bounded reconnaissance subagent" in pathfinder.instructions


def test_loads_definition_files_in_sorted_order(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "zeta.toml",
        VALID_DEFINITION.replace("pathfinder", "zeta"),
    )
    _write_definition(
        tmp_path,
        "alpha.toml",
        VALID_DEFINITION.replace("pathfinder", "alpha"),
    )

    definitions = load_subagent_definitions(tmp_path)

    assert [definition.id for definition in definitions] == ["alpha", "zeta"]


def test_rejects_invalid_toml(tmp_path: Path) -> None:
    _write_definition(tmp_path, "broken.toml", "schema_version = [")

    with pytest.raises(SubagentDefinitionError, match="invalid TOML"):
        load_subagent_definitions(tmp_path)


def test_rejects_missing_required_fields(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "missing.toml",
        VALID_DEFINITION.replace(
            'instructions = "Own only the assigned reconnaissance objective."',
            "",
        ),
    )

    with pytest.raises(SubagentDefinitionError, match="instructions"):
        load_subagent_definitions(tmp_path)


def test_rejects_unknown_definition_fields(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "unknown.toml",
        f"{VALID_DEFINITION}\nruntime_branch = \"recon_agent\"\n",
    )

    with pytest.raises(SubagentDefinitionError, match="unknown definition keys"):
        load_subagent_definitions(tmp_path)


def test_rejects_invalid_canonical_ids(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "bad.toml",
        VALID_DEFINITION.replace('id = "pathfinder"', 'id = "Path Finder"'),
    )

    with pytest.raises(SubagentDefinitionError, match="canonical lowercase identifier"):
        load_subagent_definitions(tmp_path)


def test_rejects_invalid_category_ids(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "bad-category.toml",
        VALID_DEFINITION.replace('"host_discovery"', '"Host Discovery"'),
    )

    with pytest.raises(SubagentDefinitionError, match="supported_task_categories"):
        load_subagent_definitions(tmp_path)


def test_rejects_invalid_tool_ids(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "bad-tool.toml",
        VALID_DEFINITION.replace(
            '"information_gathering.network_discovery.nmap"',
            '"Nmap"',
        ),
    )

    with pytest.raises(SubagentDefinitionError, match="dotted canonical tool id"):
        load_subagent_definitions(tmp_path)


def test_rejects_duplicate_definition_ids(tmp_path: Path) -> None:
    _write_definition(tmp_path, "first.toml")
    _write_definition(tmp_path, "second.toml")

    with pytest.raises(SubagentDefinitionError, match="duplicate subagent definition id"):
        load_subagent_definitions(tmp_path)


def test_rejects_duplicate_list_values(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "duplicate-tools.toml",
        VALID_DEFINITION.replace(
            '"information_gathering.network_discovery.nmap"',
            '"information_gathering.network_discovery.fping"',
        ),
    )

    with pytest.raises(SubagentDefinitionError, match="tool_ids contains duplicate"):
        load_subagent_definitions(tmp_path)


def test_rejects_non_positive_limits(tmp_path: Path) -> None:
    _write_definition(
        tmp_path,
        "bad-limit.toml",
        VALID_DEFINITION.replace("max_iterations = 3", "max_iterations = 0"),
    )

    with pytest.raises(SubagentDefinitionError, match="max_iterations must be positive"):
        load_subagent_definitions(tmp_path)


def test_rejects_missing_definition_directory(tmp_path: Path) -> None:
    with pytest.raises(SubagentDefinitionError, match="definition directory not found"):
        load_subagent_definitions(tmp_path / "missing")


def test_rejects_empty_definition_directory(tmp_path: Path) -> None:
    with pytest.raises(SubagentDefinitionError, match="no subagent definitions found"):
        load_subagent_definitions(tmp_path)


def test_package_metadata_includes_definition_toml() -> None:
    definition_resource = resources.files("agent.subagents").joinpath(
        "definitions/pathfinder.toml"
    )
    assert definition_resource.is_file()
    assert load_subagent_definition_file(definition_resource).id == "pathfinder"

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "subagents/definitions/*.toml" in pyproject["tool"]["setuptools"][
        "package-data"
    ]["agent"]
