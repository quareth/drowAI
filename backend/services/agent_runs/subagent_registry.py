"""Canonical subagent specifications used by classification and dispatch.

The registry is the control-plane source of truth for which subagents are
available, what work they own, and which existing chat branch dispatches them.
It contains no runtime handles, graph instances, database sessions, or secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import AgentKind


_CANONICAL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SubagentSpec:
    """Immutable registration metadata for one dispatchable subagent."""

    name: str
    display_name: str
    agent_kind: AgentKind
    dispatch_branch: str
    purpose: str
    ownership_boundary: str
    supported_task_categories: tuple[str, ...]
    excluded_task_categories: tuple[str, ...]
    enabled: bool
    max_active_runs_per_task: int
    requires_resolved_target: bool

    def __post_init__(self) -> None:
        """Reject ambiguous or unsafe registration metadata."""
        for field_name in (
            "name",
            "display_name",
            "dispatch_branch",
            "purpose",
            "ownership_boundary",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"subagent {field_name} must not be empty")
        if not _CANONICAL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("subagent name must be a canonical lowercase identifier")
        if not _CANONICAL_NAME_PATTERN.fullmatch(self.dispatch_branch):
            raise ValueError(
                "subagent dispatch_branch must be a canonical lowercase identifier"
            )
        if self.max_active_runs_per_task < 1:
            raise ValueError("subagent max_active_runs_per_task must be positive")
        if not self.supported_task_categories:
            raise ValueError("subagent supported_task_categories must not be empty")

    def classifier_projection(self) -> Mapping[str, Any]:
        """Return immutable routing facts safe to include in the LLM prompt."""
        return MappingProxyType(
            {
                "name": self.name,
                "display_name": self.display_name,
                "purpose": self.purpose,
                "ownership_boundary": self.ownership_boundary,
                "supported_task_categories": self.supported_task_categories,
                "excluded_task_categories": self.excluded_task_categories,
                "max_active_runs_per_task": self.max_active_runs_per_task,
                "requires_resolved_target": self.requires_resolved_target,
            }
        )


class SubagentRegistry:
    """Read-only lookup registry for enabled subagent specifications."""

    def __init__(self, specs: Iterable[SubagentSpec]) -> None:
        ordered_specs = tuple(specs)
        by_name: dict[str, SubagentSpec] = {}
        for spec in ordered_specs:
            if spec.name in by_name:
                raise ValueError(f"duplicate subagent name: {spec.name}")
            by_name[spec.name] = spec
        self._specs = ordered_specs
        self._by_name: Mapping[str, SubagentSpec] = MappingProxyType(by_name)

    def names(self) -> tuple[str, ...]:
        """Return enabled classifier-visible names in registration order."""
        return tuple(spec.name for spec in self._specs if spec.enabled)

    def specs(self) -> tuple[SubagentSpec, ...]:
        """Return enabled specs in registration order."""
        return tuple(spec for spec in self._specs if spec.enabled)

    def get(self, name: str) -> SubagentSpec | None:
        """Return one enabled spec by canonical name."""
        normalized = str(name or "").strip().lower()
        spec = self._by_name.get(normalized)
        if spec is None or not spec.enabled:
            return None
        return spec

    def require(self, name: str) -> SubagentSpec:
        """Return one enabled spec or raise for invalid configuration."""
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"subagent is not registered or enabled: {name}")
        return spec

    def classifier_catalog(self) -> tuple[Mapping[str, Any], ...]:
        """Return ordered prompt-safe projections for enabled subagents."""
        return tuple(
            spec.classifier_projection() for spec in self.specs()
        )

    def is_available(self, name: str, *, active_runs_for_task: int) -> bool:
        """Return whether static and task-local concurrency allow a new run."""
        spec = self.get(name)
        if spec is None:
            return False
        return active_runs_for_task < spec.max_active_runs_per_task


SCOUT_SUBAGENT_SPEC = SubagentSpec(
    name="scout",
    display_name="Pathfinder",
    agent_kind="recon",
    dispatch_branch="recon_agent",
    purpose=(
        "Perform bounded network reconnaissance and return concise evidence "
        "to the main agent."
    ),
    ownership_boundary=(
        "Own host discovery, port scanning, and service enumeration only; "
        "do not exploit, authenticate, modify targets, or produce the user's "
        "final answer."
    ),
    supported_task_categories=(
        "host_discovery",
        "port_scanning",
        "service_enumeration",
    ),
    excluded_task_categories=(
        "exploitation",
        "credential_attacks",
        "phishing",
        "payload_delivery",
        "privilege_escalation",
        "reporting",
    ),
    enabled=True,
    max_active_runs_per_task=1,
    requires_resolved_target=True,
)

_SUBAGENT_REGISTRY = SubagentRegistry((SCOUT_SUBAGENT_SPEC,))


def get_subagent_registry() -> SubagentRegistry:
    """Return the immutable process-wide subagent specification registry."""
    return _SUBAGENT_REGISTRY


__all__ = [
    "SCOUT_SUBAGENT_SPEC",
    "SubagentRegistry",
    "SubagentSpec",
    "get_subagent_registry",
]
