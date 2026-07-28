"""Declarative subagent definition schema and package loader.

Purpose
-------
Owns the static, serializable agent-definition contract loaded from
package-owned TOML files under ``agent/subagents/definitions``.

Responsibility boundary
-----------------------
This module validates definition metadata only. It does not import backend
services, graph builders, LLM clients, tool implementations, or runtime
handles, and it does not dispatch agent runs.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
DEFINITION_PACKAGE = "agent.subagents"
DEFINITION_DIRECTORY = "definitions"
_CANONICAL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "display_name",
        "kind",
        "description",
        "ownership_boundary",
        "supported_task_categories",
        "excluded_task_categories",
        "tool_ids",
        "max_active_runs_per_task",
        "max_iterations",
        "max_tool_calls_per_iteration",
        "requires_resolved_target",
        "icon",
        "instructions",
    }
)
_OPTIONAL_KEYS = frozenset({"enabled"})
_KNOWN_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS


class SubagentDefinitionError(ValueError):
    """Raised when a declarative subagent definition is invalid."""


@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    """Immutable static metadata for one declarative subagent."""

    schema_version: int
    id: str
    display_name: str
    kind: str
    description: str
    ownership_boundary: str
    supported_task_categories: tuple[str, ...]
    excluded_task_categories: tuple[str, ...]
    tool_ids: tuple[str, ...]
    enabled: bool
    max_active_runs_per_task: int
    max_iterations: int
    max_tool_calls_per_iteration: int
    requires_resolved_target: bool
    icon: str
    instructions: str

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        source: str,
    ) -> "SubagentDefinition":
        """Build and validate one definition from parsed TOML data."""
        _reject_unknown_keys(data, source=source)
        schema_version = _require_int(data, "schema_version", source=source)
        if schema_version != SCHEMA_VERSION:
            raise SubagentDefinitionError(
                f"{source}: unsupported schema_version {schema_version!r}"
            )

        definition_id = _require_canonical_id(data, "id", source=source)
        return cls(
            schema_version=schema_version,
            id=definition_id,
            display_name=_require_text(data, "display_name", source=source),
            kind=_require_canonical_id(data, "kind", source=source),
            description=_require_text(data, "description", source=source),
            ownership_boundary=_require_text(
                data,
                "ownership_boundary",
                source=source,
            ),
            supported_task_categories=_require_text_tuple(
                data,
                "supported_task_categories",
                source=source,
                require_non_empty=True,
                canonical_items=True,
            ),
            excluded_task_categories=_require_text_tuple(
                data,
                "excluded_task_categories",
                source=source,
                require_non_empty=False,
                canonical_items=True,
            ),
            tool_ids=_require_text_tuple(
                data,
                "tool_ids",
                source=source,
                require_non_empty=True,
                pattern=_TOOL_ID_PATTERN,
                pattern_message="must be a dotted canonical tool id",
            ),
            enabled=_require_bool(data, "enabled", source=source, default=True),
            max_active_runs_per_task=_require_positive_int(
                data,
                "max_active_runs_per_task",
                source=source,
            ),
            max_iterations=_require_positive_int(
                data,
                "max_iterations",
                source=source,
            ),
            max_tool_calls_per_iteration=_require_positive_int(
                data,
                "max_tool_calls_per_iteration",
                source=source,
            ),
            requires_resolved_target=_require_bool(
                data,
                "requires_resolved_target",
                source=source,
            ),
            icon=_require_canonical_id(data, "icon", source=source),
            instructions=_require_text(data, "instructions", source=source),
        )


def _reject_unknown_keys(data: Mapping[str, Any], *, source: str) -> None:
    unknown_keys = sorted(str(key) for key in data if key not in _KNOWN_KEYS)
    if unknown_keys:
        joined_keys = ", ".join(unknown_keys)
        raise SubagentDefinitionError(f"{source}: unknown definition keys: {joined_keys}")


def load_subagent_definitions(
    definitions_dir: Path | None = None,
) -> tuple[SubagentDefinition, ...]:
    """Load sorted ``*.toml`` definitions from a directory or package data."""
    if definitions_dir is None:
        package_root = resources.files(DEFINITION_PACKAGE)
        definitions_root = package_root.joinpath(DEFINITION_DIRECTORY)
        paths = sorted(
            (
                path
                for path in definitions_root.iterdir()
                if path.is_file() and path.name.endswith(".toml")
            ),
            key=lambda path: path.name,
        )
    else:
        definitions_root = definitions_dir
        if not definitions_root.exists():
            raise SubagentDefinitionError(
                f"definition directory not found: {definitions_root}"
            )
        paths = sorted(definitions_root.glob("*.toml"), key=lambda path: path.name)

    if not paths:
        raise SubagentDefinitionError("no subagent definitions found")

    definitions: list[SubagentDefinition] = []
    seen_ids: set[str] = set()
    for path in paths:
        definition = load_subagent_definition_file(path)
        if definition.id in seen_ids:
            raise SubagentDefinitionError(
                f"duplicate subagent definition id: {definition.id}"
            )
        seen_ids.add(definition.id)
        definitions.append(definition)
    return tuple(definitions)


def load_subagent_definition_file(
    path: Path | resources.abc.Traversable,
) -> SubagentDefinition:
    """Load and validate one declarative subagent definition file."""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SubagentDefinitionError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise SubagentDefinitionError(f"{path}: unable to read definition") from exc

    if not isinstance(raw, Mapping):
        raise SubagentDefinitionError(f"{path}: definition root must be a table")
    return SubagentDefinition.from_mapping(raw, source=str(path))


def _require_text(
    data: Mapping[str, Any],
    key: str,
    *,
    source: str,
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SubagentDefinitionError(f"{source}: {key} must be a non-empty string")
    return value.strip()


def _require_canonical_id(
    data: Mapping[str, Any],
    key: str,
    *,
    source: str,
) -> str:
    value = _require_text(data, key, source=source)
    if not _CANONICAL_ID_PATTERN.fullmatch(value):
        raise SubagentDefinitionError(
            f"{source}: {key} must be a canonical lowercase identifier"
        )
    return value


def _require_int(
    data: Mapping[str, Any],
    key: str,
    *,
    source: str,
) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SubagentDefinitionError(f"{source}: {key} must be an integer")
    return value


def _require_positive_int(
    data: Mapping[str, Any],
    key: str,
    *,
    source: str,
) -> int:
    value = _require_int(data, key, source=source)
    if value < 1:
        raise SubagentDefinitionError(f"{source}: {key} must be positive")
    return value


def _require_bool(
    data: Mapping[str, Any],
    key: str,
    *,
    source: str,
    default: bool | None = None,
) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SubagentDefinitionError(f"{source}: {key} must be a boolean")
    return value


def _require_text_tuple(
    data: Mapping[str, Any],
    key: str,
    *,
    source: str,
    require_non_empty: bool,
    canonical_items: bool = False,
    pattern: re.Pattern[str] | None = None,
    pattern_message: str = "has invalid format",
) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise SubagentDefinitionError(f"{source}: {key} must be a list")

    values: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise SubagentDefinitionError(
                f"{source}: {key}[{index}] must be a non-empty string"
            )
        text = item.strip()
        if canonical_items and not _CANONICAL_ID_PATTERN.fullmatch(text):
            raise SubagentDefinitionError(
                f"{source}: {key}[{index}] must be a canonical lowercase identifier"
            )
        if pattern is not None and not pattern.fullmatch(text):
            raise SubagentDefinitionError(
                f"{source}: {key}[{index}] {pattern_message}"
            )
        if text in seen:
            raise SubagentDefinitionError(f"{source}: {key} contains duplicate {text!r}")
        seen.add(text)
        values.append(text)

    if require_non_empty and not values:
        raise SubagentDefinitionError(f"{source}: {key} must not be empty")
    return tuple(values)


__all__ = [
    "DEFINITION_DIRECTORY",
    "DEFINITION_PACKAGE",
    "SCHEMA_VERSION",
    "SubagentDefinition",
    "SubagentDefinitionError",
    "load_subagent_definition_file",
    "load_subagent_definitions",
]
