"""
Shared role classifications and model requirement contracts.

This module defines which roles are user-selected versus internally managed,
and the provider-neutral capabilities each internal role requires from its
resolved model. It is the small policy layer that lets provider profiles be
validated without putting provider defaults in core role resolution.

Boundary: requirements are expressed as simple strings and booleans so this
module stays independent from agent provider imports. Concrete capability
validation happens in the provider profile registry and role resolver.
"""

from __future__ import annotations

from dataclasses import dataclass

from .role_contracts import (
    ROLE_CONVERSATION_MAIN,
    ROLE_INTENT_CLASSIFIER,
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_POST_TOOL_OBSERVATION,
    ROLE_REASONING_MAIN,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
    RoleKey,
)


@dataclass(frozen=True, slots=True)
class RoleRequirements:
    """Provider-neutral model requirements for one role."""

    required_capabilities: tuple[str, ...] = ()
    structured_output_required: bool = False


USER_SELECTED_ROLE_KEYS: frozenset[str] = frozenset(
    {
        ROLE_CONVERSATION_MAIN,
        ROLE_REASONING_MAIN,
        ROLE_POST_TOOL_OBSERVATION,
        ROLE_INTENT_CLASSIFIER,
    }
)

INTERNAL_ROLE_KEYS: frozenset[str] = frozenset(
    {
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        ROLE_TOOL_CATEGORY_SELECTOR,
        ROLE_POST_TOOL_ARTICULATOR,
    }
)

ROLE_REQUIREMENTS: dict[str, RoleRequirements] = {
    ROLE_TOOL_OUTPUT_COMPRESSOR: RoleRequirements(required_capabilities=("chat",)),
    ROLE_TOOL_CATEGORY_SELECTOR: RoleRequirements(
        required_capabilities=("chat",),
        structured_output_required=True,
    ),
    ROLE_POST_TOOL_ARTICULATOR: RoleRequirements(required_capabilities=("chat",)),
}


def get_role_requirements(role: RoleKey | str) -> RoleRequirements:
    """Return requirements for a role, defaulting to no extra requirements."""
    return ROLE_REQUIREMENTS.get(str(role), RoleRequirements())


__all__ = [
    "INTERNAL_ROLE_KEYS",
    "ROLE_REQUIREMENTS",
    "RoleRequirements",
    "USER_SELECTED_ROLE_KEYS",
    "get_role_requirements",
]
