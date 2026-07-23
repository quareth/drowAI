"""Code-owned wire policies for reviewed OpenAI-compatible routes.

This module translates provider-neutral request controls only after the
compatible adapter validates them. It owns no credentials, endpoints,
persistence access, or user-configurable policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ...core.exceptions import LLMConfigurationError


DEFAULT_COMPATIBLE_REQUEST_POLICY_ID = "default_v1"
MISTRAL_SMALL_REQUEST_POLICY_ID = "mistral_small_v1"


@dataclass(frozen=True, slots=True)
class CompatibleRequestOptions:
    """Validated neutral options needed for provider wire translation."""

    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class CompatibleRequestPolicy:
    """Immutable translation policy for one reviewed compatible route."""

    policy_id: str
    reasoning_shape: str | None = None
    required_tool_choice_value: str | None = None
    fixed_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _normalize_policy_id(self.policy_id))
        object.__setattr__(
            self,
            "fixed_fields",
            MappingProxyType(dict(self.fixed_fields)),
        )
        if self.reasoning_shape not in {None, "reasoning_effort", "reasoning.effort"}:
            raise ValueError("Unsupported compatible reasoning shape")
        if self.required_tool_choice_value not in {None, "required", "any"}:
            raise ValueError("Unsupported required tool-choice wire value")

    def translate(
        self,
        request_kwargs: Mapping[str, Any],
        options: CompatibleRequestOptions,
    ) -> dict[str, Any]:
        """Return a wire payload with policy-owned fields applied fail-closed."""

        payload = dict(request_kwargs)
        if (
            payload.get("tool_choice") == "required"
            and self.required_tool_choice_value is not None
        ):
            payload["tool_choice"] = self.required_tool_choice_value
        if options.reasoning_effort is not None and self.reasoning_shape is not None:
            value: Any = options.reasoning_effort
            field_name = "reasoning_effort"
            if self.reasoning_shape == "reasoning.effort":
                field_name = "reasoning"
                value = {"effort": options.reasoning_effort}
            _set_fixed_field(
                payload,
                field_name,
                value,
                policy_id=self.policy_id,
            )
        for field_name, field_value in self.fixed_fields.items():
            _set_fixed_field(
                payload,
                field_name,
                field_value,
                policy_id=self.policy_id,
            )
        return payload


def _normalize_policy_id(policy_id: str) -> str:
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise LLMConfigurationError(
            "OpenAI-compatible request policy id must be non-empty",
            provider="OpenAI-compatible",
        )
    return policy_id.strip().lower()


def _set_fixed_field(
    payload: dict[str, Any],
    field_name: str,
    field_value: Any,
    *,
    policy_id: str,
) -> None:
    existing = payload.get(field_name)
    if existing is not None and existing != field_value:
        raise LLMConfigurationError(
            (
                f"Compatible request policy '{policy_id}' cannot override "
                f"pre-existing request field '{field_name}'"
            ),
            provider="OpenAI-compatible",
        )
    payload[field_name] = field_value


_DEFAULT_POLICY = CompatibleRequestPolicy(
    policy_id=DEFAULT_COMPATIBLE_REQUEST_POLICY_ID,
)
_MISTRAL_SMALL_POLICY = CompatibleRequestPolicy(
    policy_id=MISTRAL_SMALL_REQUEST_POLICY_ID,
    reasoning_shape="reasoning_effort",
    required_tool_choice_value="any",
)
_POLICIES_BY_ID = MappingProxyType(
    {
        policy.policy_id: policy
        for policy in (_DEFAULT_POLICY, _MISTRAL_SMALL_POLICY)
    }
)


def resolve_compatible_request_policy(
    policy_id: str | None,
) -> CompatibleRequestPolicy:
    """Return a registered compatible request policy or fail closed."""

    normalized = (
        DEFAULT_COMPATIBLE_REQUEST_POLICY_ID
        if policy_id is None
        else _normalize_policy_id(policy_id)
    )
    try:
        return _POLICIES_BY_ID[normalized]
    except KeyError as exc:
        raise LLMConfigurationError(
            f"OpenAI-compatible request policy is not registered: {policy_id}",
            provider="OpenAI-compatible",
        ) from exc


def list_compatible_request_policy_ids() -> frozenset[str]:
    """Return the immutable set of code-owned compatible request policy IDs."""

    return frozenset(_POLICIES_BY_ID)


__all__ = [
    "CompatibleRequestOptions",
    "CompatibleRequestPolicy",
    "DEFAULT_COMPATIBLE_REQUEST_POLICY_ID",
    "MISTRAL_SMALL_REQUEST_POLICY_ID",
    "list_compatible_request_policy_ids",
    "resolve_compatible_request_policy",
]
