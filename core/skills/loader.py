"""Safely parse and validate one built-in ``SKILL.md`` package."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from core.skills.contracts import LoadedSkill, SkillActivationPolicy, SkillMetadata
from core.skills.errors import SkillLoadError, SkillParseError, SkillValidationError
from core.skills.identifiers import normalize_skill_id


SKILL_ENTRYPOINT = "SKILL.md"
MAX_SKILL_FILE_BYTES = 262_144
MAX_SKILL_FRONTMATTER_CHARACTERS = 16_000
MAX_SKILL_LINES = 500
MAX_SKILL_CHARACTERS = 40_000
MAX_SKILL_ESTIMATED_TOKENS = 5_000
MAX_DESCRIPTION_CHARACTERS = 1_024
MAX_SKILL_METADATA_ENTRIES = 32
MAX_SKILL_METADATA_KEY_CHARACTERS = 128
MAX_SKILL_METADATA_VALUE_CHARACTERS = 4_096
_CANONICAL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TOP_LEVEL_FIELDS = frozenset({"name", "description", "license", "compatibility", "metadata"})
_ACTIVATION_KEYS = frozenset({"version", "activation", "agent-ids"})
_RETIRED_ACTIVATION_KEYS = frozenset(
    {
        "priority",
        "agent-kinds",
        "trigger-tool-ids",
        "trigger-capability-ids",
        "trigger-capability-families",
    }
)
_ACTIVATION_MODES = ("mandatory", "selectable")


class SkillLoader:
    """Load packages only from one configured built-in root."""

    def __init__(self, *, builtin_root: Path | str) -> None:
        self._builtin_root = Path(builtin_root)

    def load(self, package_path: Path | str) -> LoadedSkill:
        """Return one validated, digest-pinned skill package."""

        package = Path(package_path)
        self._validate_package_path(package)
        entrypoint = package / SKILL_ENTRYPOINT
        if entrypoint.is_symlink():
            raise SkillLoadError(f"skill entrypoint must not be a symlink: {entrypoint}")
        try:
            with entrypoint.open("rb") as handle:
                raw_bytes = handle.read(MAX_SKILL_FILE_BYTES + 1)
        except OSError as exc:
            raise SkillLoadError(f"unable to read skill entrypoint {entrypoint}: {exc}") from exc
        if len(raw_bytes) > MAX_SKILL_FILE_BYTES:
            raise SkillLoadError(f"skill entrypoint exceeds byte limit: {entrypoint}")
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError(f"skill entrypoint must be UTF-8: {entrypoint}") from exc

        frontmatter, body = _parse_frontmatter(raw_text, source=entrypoint)
        try:
            return _loaded_skill(
                frontmatter,
                body=body,
                package_name=package.name,
                source=f"{package.name}/{SKILL_ENTRYPOINT}",
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise SkillValidationError(f"invalid skill package {package.name}: {exc}") from exc

    def _validate_package_path(self, package: Path) -> None:
        """Reject symlinks, nested packages, and paths outside the root."""

        root = self._builtin_root.resolve()
        if package.is_symlink():
            raise SkillLoadError(f"skill package must not be a symlink: {package}")
        try:
            resolved = package.resolve(strict=True)
        except OSError as exc:
            raise SkillLoadError(f"skill package does not exist: {package}") from exc
        if not resolved.is_dir() or resolved.parent != root:
            raise SkillLoadError(f"skill package must be an immediate child of {root}")


def _parse_frontmatter(raw_text: str, *, source: Path) -> tuple[dict[str, Any], str]:
    lines = raw_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillParseError(f"skill {source} must start with YAML frontmatter")
    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise SkillParseError(f"skill {source} has malformed YAML frontmatter")
    frontmatter_text = "".join(lines[1:closing_index])
    if len(frontmatter_text) > MAX_SKILL_FRONTMATTER_CHARACTERS:
        raise SkillParseError(f"skill {source} frontmatter exceeds character limit")
    try:
        parsed = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"skill {source} frontmatter is invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillParseError(f"skill {source} frontmatter must be a mapping")
    return parsed, "".join(lines[closing_index + 1 :]).strip()


def _loaded_skill(
    frontmatter: Mapping[str, Any],
    *,
    body: str,
    package_name: str,
    source: str,
) -> LoadedSkill:
    unknown_fields = set(frontmatter) - _TOP_LEVEL_FIELDS
    if unknown_fields:
        raise ValueError(f"unknown top-level fields: {sorted(unknown_fields)}")
    name = _required_string(frontmatter.get("name"), "name")
    try:
        canonical_name = normalize_skill_id(name)
    except ValueError as exc:
        raise ValueError("name must be canonical and match the package directory") from exc
    if canonical_name != name or name != package_name:
        raise ValueError("name must be canonical and match the package directory")
    description = _required_string(frontmatter.get("description"), "description")
    if len(description) > MAX_DESCRIPTION_CHARACTERS:
        raise ValueError("description exceeds 1024 characters")
    if not body:
        raise ValueError("body must not be empty")
    if len(body) > MAX_SKILL_CHARACTERS:
        raise ValueError("body exceeds character limit")
    if len(body.splitlines()) > MAX_SKILL_LINES:
        raise ValueError("body exceeds line limit")
    if _estimate_tokens(body) > MAX_SKILL_ESTIMATED_TOKENS:
        raise ValueError("body exceeds estimated token limit")

    raw_metadata = frontmatter.get("metadata") or {}
    if not isinstance(raw_metadata, Mapping):
        raise ValueError("metadata must be a string-to-string mapping")
    if len(raw_metadata) > MAX_SKILL_METADATA_ENTRIES:
        raise ValueError("metadata exceeds entry limit")
    metadata_values: dict[str, str] = {}
    for key, value in raw_metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("metadata must contain string keys and values")
        if len(key) > MAX_SKILL_METADATA_KEY_CHARACTERS:
            raise ValueError("metadata key exceeds character limit")
        if len(value) > MAX_SKILL_METADATA_VALUE_CHARACTERS:
            raise ValueError(f"metadata.{key} exceeds character limit")
        metadata_values[key] = value

    version = metadata_values.get("version", "1").strip()
    if not version.isdigit() or int(version) <= 0:
        raise ValueError("metadata.version must be a positive integer string")
    activation = _activation_policy(metadata_values)
    metadata = SkillMetadata(
        name=name,
        description=description,
        license=_optional_bounded_string(frontmatter.get("license"), "license"),
        compatibility=_optional_bounded_string(
            frontmatter.get("compatibility"), "compatibility"
        ),
        version=version,
    )
    digest_payload = {
        "metadata": metadata.model_dump(mode="json"),
        "activation": activation.model_dump(mode="json"),
        "unrecognized_metadata": {
            key: metadata_values[key]
            for key in sorted(set(metadata_values) - _ACTIVATION_KEYS)
        },
        "body": body,
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return LoadedSkill(
        metadata=metadata,
        activation=activation,
        body=body,
        source=source,
        digest=digest,
    )


def _activation_policy(metadata: Mapping[str, str]) -> SkillActivationPolicy:
    retired_keys = sorted(set(metadata).intersection(_RETIRED_ACTIVATION_KEYS))
    if retired_keys:
        raise ValueError(f"metadata contains retired skill policy keys: {retired_keys}")

    raw_activation = _required_string(metadata.get("activation"), "metadata.activation")
    if "," in raw_activation:
        raise ValueError("metadata.activation must contain exactly one mode")
    if raw_activation not in _ACTIVATION_MODES:
        raise ValueError("metadata.activation contains an unsupported mode")

    agent_ids = _canonical_values(metadata.get("agent-ids"), "agent-ids")
    if not agent_ids:
        raise ValueError("metadata.agent-ids must contain at least one compatible agent id")
    return SkillActivationPolicy(
        activation=raw_activation,
        agent_ids=agent_ids,
    )


def _csv_values(raw: str | None, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    values = tuple(item.strip() for item in raw.split(","))
    if any(not item for item in values):
        raise ValueError(f"metadata.{field_name} contains an empty value")
    if len(set(values)) != len(values):
        raise ValueError(f"metadata.{field_name} contains a duplicate value")
    return values


def _canonical_values(
    raw: str | None,
    field_name: str,
    *,
    pattern: re.Pattern[str] = _CANONICAL_ID_PATTERN,
) -> tuple[str, ...]:
    values = _csv_values(raw, field_name)
    if any(not pattern.fullmatch(value) for value in values):
        raise ValueError(f"metadata.{field_name} contains a non-canonical identifier")
    return values


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _optional_bounded_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    text = _required_string(value, field_name)
    if len(text) > MAX_DESCRIPTION_CHARACTERS:
        raise ValueError(f"{field_name} exceeds 1024 characters")
    return text


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


__all__ = [
    "MAX_DESCRIPTION_CHARACTERS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_FRONTMATTER_CHARACTERS",
    "MAX_SKILL_CHARACTERS",
    "MAX_SKILL_ESTIMATED_TOKENS",
    "MAX_SKILL_LINES",
    "MAX_SKILL_METADATA_ENTRIES",
    "MAX_SKILL_METADATA_KEY_CHARACTERS",
    "MAX_SKILL_METADATA_VALUE_CHARACTERS",
    "SKILL_ENTRYPOINT",
    "SkillLoader",
]
