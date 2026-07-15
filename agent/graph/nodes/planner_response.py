"""Planner response parsing and fallback plan construction.

This module owns deterministic conversion of planner LLM output into the
plan/todo/goal tuple used by ``planner_node``. It does not call LLMs or mutate
graph state.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


def extract_planning_contract(
    response: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract planner contract payload from raw LLM output when possible."""
    if isinstance(structured_payload, Mapping):
        return dict(structured_payload)
    try:
        json_start = response.find("{")
        json_end = response.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = response[json_start:json_end]
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def parse_planning_response(
    response: str,
    user_message: str,
    targets: List[str],
    structured_payload: Optional[Mapping[str, Any]] = None,
) -> tuple:
    """Parse LLM planning response into plan, todo list, and first goal.

    Note: clarify-required mode is handled by planner_node contract checks
    before this parser is called.
    """
    try:
        parsed: Optional[Dict[str, Any]] = None
        if isinstance(structured_payload, Mapping):
            parsed = dict(structured_payload)
        else:
            # Try to extract JSON from response
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                json_str = response[json_start:json_end]
                maybe_parsed = json.loads(json_str)
                if isinstance(maybe_parsed, dict):
                    parsed = maybe_parsed

        if isinstance(parsed, dict):
            plan = parsed.get("plan", [])
            todo_list = parsed.get("todo_list", [])
            first_goal = parsed.get("first_goal", "")

            # Validate we got something useful
            if plan and len(plan) >= 2:
                return plan, todo_list, first_goal or plan[0]

    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.warning(f"Failed to parse planning response: {exc}")

    # Fallback if parsing fails
    return create_fallback_plan(user_message, targets)


def create_fallback_plan(user_message: str, targets: List[str]) -> tuple:
    """Create a simple fallback plan when LLM is unavailable."""
    target_str = targets[0] if targets else "target"

    # Simple generic plan
    plan = [
        f"Step 1: Gather initial information about {target_str}",
        "Step 2: Identify services and potential entry points",
        "Step 3: Analyze findings and determine next steps",
        "Step 4: Compile and report results",
    ]

    todo_list = plan[:]

    first_goal = f"Identify open ports and running services on {target_str}"

    return plan, todo_list, first_goal
