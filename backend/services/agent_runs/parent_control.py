"""Decode parent graph output into typed subagent coordination actions.

This module owns only the transport-neutral interpretation of parent result
metadata and stable decision identity. Registry claims, waiting, dispatch, and
metadata mutation remain coordinator responsibilities.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from backend.services.metrics.utils import safe_inc

from .ownership_policy import normalize_agent_handoff_entries


ParentControlAction = Literal[
    "finish",
    "delegate_subagent",
    "wait_for_subagents",
]


@dataclass(frozen=True, slots=True)
class ParentControlOutcome:
    """Validated coordination action returned by the parent graph."""

    action: ParentControlAction
    agent_handoff: Mapping[str, Any] | None = None
    decision_id: str = ""


def parse_parent_control_outcome(
    metadata: Mapping[str, Any],
    *,
    parent_turn_id: str,
    claimed_agent_run_ids: tuple[str, ...],
) -> ParentControlOutcome:
    """Return the supported parent coordination action from result metadata."""
    source = _control_source(metadata)
    if source is None:
        return ParentControlOutcome(action="finish")

    action = _control_action(source)
    if action not in {"delegate_subagent", "wait_for_subagents"}:
        return ParentControlOutcome(action="finish")

    decision_id = _control_decision_id(
        source,
        action=action,
        parent_turn_id=parent_turn_id,
        claimed_agent_run_ids=claimed_agent_run_ids,
    )
    if action == "wait_for_subagents":
        return ParentControlOutcome(action=action, decision_id=decision_id)

    try:
        normalized = normalize_agent_handoff_entries(
            source.get("agent_handoff"),
            max_handoffs=1,
            reject_invalid=True,
        )
    except ValueError:
        normalized = ()
    if not normalized:
        safe_inc("post_action_reasoning_followup_delegation_rejected")
        raise RuntimeError("PAR delegate_subagent outcome missing agent_handoff")
    return ParentControlOutcome(
        action=action,
        agent_handoff=normalized[0],
        decision_id=decision_id,
    )


def _control_source(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("router_outcome", "candidate_decision", "parent_control_outcome"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            return value
    return metadata


def _control_action(source: Mapping[str, Any]) -> str:
    for key in ("action", "next_action", "last_post_tool_action"):
        value = source.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower().replace(" ", "_")
            if normalized:
                return normalized
    return ""


def _control_decision_id(
    source: Mapping[str, Any],
    *,
    action: str,
    parent_turn_id: str,
    claimed_agent_run_ids: tuple[str, ...],
) -> str:
    for key in ("decision_id", "candidate_id", "id"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    identity = {
        "action": action,
        "parent_turn_id": parent_turn_id,
        "claimed_agent_run_ids": list(claimed_agent_run_ids),
        "agent_handoff": source.get("agent_handoff"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    return f"par-decision-{digest}"


__all__ = [
    "ParentControlAction",
    "ParentControlOutcome",
    "parse_parent_control_outcome",
]
