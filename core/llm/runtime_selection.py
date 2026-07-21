"""Canonical non-secret runtime-selection contracts for durable LLM state.

This module owns the provider-neutral deployment identity that may cross the
backend/agent boundary or be persisted in LangGraph checkpoints. It deliberately
contains no database access, credentials, endpoints, provider SDKs, or live
authorization behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DeploymentRef:
    """Checkpoint-safe deployment identity with optimistic revision."""

    deployment_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "deployment_id",
            _canonical_uuid(self.deployment_id, "deployment_id"),
        )
        _positive_revision(self.expected_revision)

    def to_dict(self) -> dict[str, Any]:
        """Return the complete safe serialized deployment reference."""

        return {
            "deployment_id": self.deployment_id,
            "expected_revision": self.expected_revision,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "DeploymentRef":
        """Parse a deployment reference without accepting extra facts."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("DeploymentRef requires a mapping")
        extra = set(value) - {"deployment_id", "expected_revision"}
        if extra:
            raise ValueError("DeploymentRef contains unsupported fields")
        return cls(
            deployment_id=str(value["deployment_id"]),
            expected_revision=value["expected_revision"],
        )


@dataclass(frozen=True, slots=True)
class LLMRuntimeSelectionV2:
    """Checkpoint-safe deployment selection without live infrastructure facts."""

    deployment_ref: DeploymentRef
    preferred_route_id: str | None = None
    reasoning_effort: str | None = None
    legacy_provider: str | None = None
    legacy_model: str | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("LLM runtime selection schema_version must be 2")
        if not isinstance(self.deployment_ref, DeploymentRef):
            raise TypeError("deployment_ref must be DeploymentRef")
        if self.preferred_route_id is not None:
            object.__setattr__(
                self,
                "preferred_route_id",
                _canonical_uuid(self.preferred_route_id, "preferred_route_id"),
            )
        for field_name in (
            "reasoning_effort",
            "legacy_provider",
            "legacy_model",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be non-empty when supplied")

    def to_dict(self) -> dict[str, Any]:
        """Serialize only checkpoint-safe identity and diagnostic fields."""

        return {
            "schema_version": self.schema_version,
            "deployment_ref": self.deployment_ref.to_dict(),
            "preferred_route_id": self.preferred_route_id,
            "reasoning_effort": self.reasoning_effort,
            "legacy_provider": self.legacy_provider,
            "legacy_model": self.legacy_model,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "LLMRuntimeSelectionV2":
        """Parse V2 identity while rejecting resolved or unknown fields."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("LLMRuntimeSelectionV2 requires a mapping")
        allowed = {
            "schema_version",
            "deployment_ref",
            "preferred_route_id",
            "reasoning_effort",
            "legacy_provider",
            "legacy_model",
        }
        if set(value) - allowed:
            raise ValueError("LLMRuntimeSelectionV2 contains unsupported fields")
        return cls(
            schema_version=value.get("schema_version"),
            deployment_ref=DeploymentRef.from_mapping(value["deployment_ref"]),
            preferred_route_id=value.get("preferred_route_id"),
            reasoning_effort=value.get("reasoning_effort"),
            legacy_provider=value.get("legacy_provider"),
            legacy_model=value.get("legacy_model"),
        )


def has_versioned_runtime_selection_marker(value: Any) -> bool:
    """Return whether a mapping claims to contain versioned deployment identity."""

    return isinstance(value, Mapping) and (
        "schema_version" in value or "deployment_ref" in value
    )


def project_checkpoint_runtime_selection(
    value: Any,
    *,
    include_legacy_diagnostics: bool = True,
) -> dict[str, Any] | None:
    """Validate and project a versioned selection onto its durable safe fields.

    Unknown fields are intentionally discarded so historical checkpoints that
    accidentally carried live material can be read without propagating it. Once
    a versioned marker is present, malformed or unsupported identity raises
    instead of silently downgrading to legacy provider/model resolution.
    """

    if not has_versioned_runtime_selection_marker(value):
        return None
    assert isinstance(value, Mapping)
    if value.get("schema_version") != 2:
        raise ValueError("Unsupported LLM runtime selection schema_version")
    deployment_ref = value.get("deployment_ref")
    if not isinstance(deployment_ref, Mapping):
        raise ValueError("LLM runtime selection deployment_ref is required")

    safe_ref = {
        key: deployment_ref[key]
        for key in ("deployment_id", "expected_revision")
        if key in deployment_ref
    }
    safe_value: dict[str, Any] = {
        "schema_version": 2,
        "deployment_ref": safe_ref,
    }
    for key in ("preferred_route_id", "reasoning_effort"):
        if value.get(key) is not None:
            safe_value[key] = value[key]
    if include_legacy_diagnostics:
        for key in ("legacy_provider", "legacy_model"):
            if value.get(key) is not None:
                safe_value[key] = value[key]

    parsed = LLMRuntimeSelectionV2.from_mapping(safe_value)
    payload: dict[str, Any] = {
        "schema_version": 2,
        "deployment_ref": parsed.deployment_ref.to_dict(),
    }
    for key, field_value in (
        ("preferred_route_id", parsed.preferred_route_id),
        ("reasoning_effort", parsed.reasoning_effort),
        ("legacy_provider", parsed.legacy_provider),
        ("legacy_model", parsed.legacy_model),
    ):
        if field_value is not None:
            payload[key] = field_value
    return payload


def deployment_runtime_selection_identity(value: Any) -> tuple[Any, ...]:
    """Return routing identity, excluding legacy diagnostic labels."""

    payload = project_checkpoint_runtime_selection(value)
    if payload is None:
        raise ValueError("Deployment runtime selection is required")
    deployment_ref = payload["deployment_ref"]
    return (
        payload["schema_version"],
        deployment_ref["deployment_id"],
        deployment_ref["expected_revision"],
        payload.get("preferred_route_id"),
        payload.get("reasoning_effort"),
    )


def _canonical_uuid(value: Any, field_name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("expected_revision must be a positive integer")
    return value


__all__ = [
    "DeploymentRef",
    "LLMRuntimeSelectionV2",
    "deployment_runtime_selection_identity",
    "has_versioned_runtime_selection_marker",
    "project_checkpoint_runtime_selection",
]
