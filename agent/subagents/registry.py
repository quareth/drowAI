"""Definition-backed registry for generic subagent metadata.

Purpose
-------
Owns read-only lookup and projection of declarative subagent definitions for
generic classifier, display, tool, limit, and availability consumers.

Responsibility boundary
-----------------------
This module derives metadata only from ``SubagentDefinition`` objects loaded
from package TOML files. It does not import backend services, graph builders,
LLM clients, tool implementations, or runtime handles, and it does not dispatch
agent runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from agent.subagents.definition import (
    SubagentDefinition,
    load_subagent_definitions,
)


@dataclass(frozen=True, slots=True)
class SubagentDisplayMetadata:
    """Classifier/UI-safe display facts for one subagent definition."""

    agent_id: str
    display_name: str
    description: str
    icon: str


@dataclass(frozen=True, slots=True)
class SubagentToolMetadata:
    """Static tool and ownership facts for one subagent definition."""

    agent_id: str
    tool_ids: tuple[str, ...]
    supported_task_categories: tuple[str, ...]
    excluded_task_categories: tuple[str, ...]
    ownership_boundary: str


@dataclass(frozen=True, slots=True)
class SubagentLimits:
    """Static execution limits for one subagent definition."""

    agent_id: str
    max_active_runs_per_task: int
    max_iterations: int
    max_tool_calls_per_iteration: int
    requires_resolved_target: bool


class SubagentRegistry:
    """Read-only lookup registry for enabled declarative subagent definitions."""

    def __init__(self, definitions: Iterable[SubagentDefinition]) -> None:
        ordered_definitions = tuple(definitions)
        by_id: dict[str, SubagentDefinition] = {}
        for definition in ordered_definitions:
            if definition.id in by_id:
                raise ValueError(f"duplicate subagent definition id: {definition.id}")
            by_id[definition.id] = definition
        self._definitions = ordered_definitions
        self._by_id: Mapping[str, SubagentDefinition] = MappingProxyType(by_id)

    def ids(self) -> tuple[str, ...]:
        """Return enabled classifier-visible definition ids in load order."""
        return tuple(
            definition.id for definition in self._definitions if definition.enabled
        )

    def definitions(self) -> tuple[SubagentDefinition, ...]:
        """Return enabled definitions in load order."""
        return tuple(
            definition for definition in self._definitions if definition.enabled
        )

    def get(self, agent_id: str) -> SubagentDefinition | None:
        """Return one enabled definition by canonical id."""
        normalized = str(agent_id or "").strip().lower()
        definition = self._by_id.get(normalized)
        if definition is None or not definition.enabled:
            return None
        return definition

    def require(self, agent_id: str) -> SubagentDefinition:
        """Return one enabled definition or raise for invalid configuration."""
        definition = self.get(agent_id)
        if definition is None:
            raise KeyError(f"subagent is not registered or enabled: {agent_id}")
        return definition

    def classifier_catalog(self) -> tuple[Mapping[str, Any], ...]:
        """Return ordered prompt-safe projections for enabled definitions."""
        return tuple(
            MappingProxyType(
                {
                    "name": definition.id,
                    "agent_id": definition.id,
                    "display_name": definition.display_name,
                    "purpose": definition.description,
                    "ownership_boundary": definition.ownership_boundary,
                    "supported_task_categories": (
                        definition.supported_task_categories
                    ),
                    "excluded_task_categories": (
                        definition.excluded_task_categories
                    ),
                    "max_active_runs_per_task": (
                        definition.max_active_runs_per_task
                    ),
                    "requires_resolved_target": (
                        definition.requires_resolved_target
                    ),
                }
            )
            for definition in self.definitions()
        )

    def display_metadata(self, agent_id: str) -> SubagentDisplayMetadata:
        """Return display metadata for one enabled definition."""
        definition = self.require(agent_id)
        return SubagentDisplayMetadata(
            agent_id=definition.id,
            display_name=definition.display_name,
            description=definition.description,
            icon=definition.icon,
        )

    def tool_metadata(self, agent_id: str) -> SubagentToolMetadata:
        """Return tool and ownership metadata for one enabled definition."""
        definition = self.require(agent_id)
        return SubagentToolMetadata(
            agent_id=definition.id,
            tool_ids=definition.tool_ids,
            supported_task_categories=definition.supported_task_categories,
            excluded_task_categories=definition.excluded_task_categories,
            ownership_boundary=definition.ownership_boundary,
        )

    def limits(self, agent_id: str) -> SubagentLimits:
        """Return execution limits for one enabled definition."""
        definition = self.require(agent_id)
        return SubagentLimits(
            agent_id=definition.id,
            max_active_runs_per_task=definition.max_active_runs_per_task,
            max_iterations=definition.max_iterations,
            max_tool_calls_per_iteration=definition.max_tool_calls_per_iteration,
            requires_resolved_target=definition.requires_resolved_target,
        )

    def is_available(self, agent_id: str, *, active_runs_for_task: int) -> bool:
        """Return whether static and task-local limits allow a new run."""
        definition = self.get(agent_id)
        if definition is None:
            return False
        active_count = max(0, int(active_runs_for_task))
        return active_count < definition.max_active_runs_per_task


_SUBAGENT_REGISTRY: SubagentRegistry | None = None


def load_subagent_registry(definitions_dir: Path | None = None) -> SubagentRegistry:
    """Construct a registry from TOML definitions loaded from package data."""
    return SubagentRegistry(load_subagent_definitions(definitions_dir))


def get_subagent_registry() -> SubagentRegistry:
    """Return the process-wide registry built from declarative definitions."""
    global _SUBAGENT_REGISTRY
    if _SUBAGENT_REGISTRY is None:
        _SUBAGENT_REGISTRY = load_subagent_registry()
    return _SUBAGENT_REGISTRY


__all__ = [
    "SubagentDisplayMetadata",
    "SubagentLimits",
    "SubagentRegistry",
    "SubagentToolMetadata",
    "get_subagent_registry",
    "load_subagent_registry",
]
