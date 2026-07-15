"""Pure guard predicates used by LangGraph routers."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .state import FactsState


def has_candidate_tools(facts: FactsState) -> bool:
    """Return True when the classifier selected candidate tools."""
    return bool(facts.tool_ids)


def within_tool_budget(facts: FactsState) -> bool:
    """Check whether the turn can execute another tool call."""
    budget = facts.budgets.max_tool_calls
    if budget is None:
        return True
    return facts.tool_calls_used < budget


def within_iteration_budget(facts: FactsState) -> bool:
    """Check whether more reasoning iterations are allowed."""
    budget = facts.budgets.max_iterations
    if budget is None:
        return True
    return facts.iterations < budget


def _normalize_capability_label(value: Any) -> str:
    """Return a comparable capability/routing label without fuzzy fallback."""

    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def capability_in(facts: FactsState, options: Iterable[str]) -> bool:
    """Helper to check whether the capability matches one of the options.
    
    Supports enum values, enum-value strings, and routing labels such as
    ``deep_reasoning`` and ``simple_tool_execution``.

    This intentionally avoids ``CapabilityType.from_intent()``. That parser is
    advisory and maps unknown labels to ``RESPOND``; using it here would make
    unrelated routing labels compare equal through the shared fallback.
    """
    if facts.capability is None:
        return False

    capability_label = _normalize_capability_label(facts.capability)
    if not capability_label:
        return False

    return any(
        capability_label == _normalize_capability_label(option)
        for option in options
    )


def has_risk_flag(facts: FactsState, flag: Optional[str] = None) -> bool:
    """Return True when a risk flag is present (or matches the provided flag)."""
    if not facts.risk_flags:
        return False
    if flag is None:
        return True
    flag_lower = flag.lower()
    return any(entry.lower() == flag_lower for entry in facts.risk_flags)


def route_eligible(facts: FactsState, route: str) -> bool:
    """Check whether the requested route is still eligible for the current turn."""
    if not facts.eligible_routes:
        return True
    route_lower = route.lower()
    return any(entry.lower() == route_lower for entry in facts.eligible_routes)


def has_eligible_routes(facts: FactsState) -> bool:
    """Return True if any routing options remain available."""
    return bool(facts.eligible_routes)
