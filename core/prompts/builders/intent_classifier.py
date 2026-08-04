"""Prompt builder for the intent classifier role.

Owns the classifier user-prompt assembly — including the conditional
"Execution Route Policy" block added for the Plan/Chat LangGraph
execution wiring. The service layer
(``backend/services/langgraph_chat/intent/classifier.py``) must supply
runtime facts only; prompt wording, placeholder names, and section
layout live here.

Ownership rules (see
``docs/issues/refactor/monolith/guides/plan_chat_langgraph_execution_implementation_guide.md``
§"Prompt Passing Design"):

- templates are resolved through ``PromptRegistry`` by their stable ids
  (``intent_classifier`` / ``intent_prompt_template``), not by loading
  files from disk directly.
- the conditional route-policy section is the single point that
  transforms an ``execution_route_policy`` metadata dict into prompt
  text; nothing downstream shapes it further.
- the section is omitted entirely when no forced route exists, so the
  classifier prompt stays identical to the v7 shape for ``agent`` /
  ``agent_full`` turns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Mapping, Optional

from core.prompts.registry import PromptRegistry


_registry = PromptRegistry()


def _resolve_policy_source(policy: Mapping[str, Any]) -> str:
    """Return the human-readable policy source label for the prompt.

    Prefers the explicit ``source`` + ``agent_mode`` pair emitted by the
    context builder (``source=agent_mode``) and renders it as
    ``agent_mode=<mode>``. Falls back to the raw ``source`` value when a
    caller provides a policy payload in an alternate shape. Only policy
    shapes produced by wired callers are expected in production; the
    fallback exists purely so the block never renders a blank source.
    """
    source = str(policy.get("source") or "").strip().lower()
    agent_mode = str(policy.get("agent_mode") or "").strip().lower()
    if source == "agent_mode" and agent_mode:
        return f"agent_mode={agent_mode}"
    if agent_mode:
        # Map unknown source but known mode back onto the same canonical
        # surface so the prompt text never disagrees with the policy
        # object.
        return f"agent_mode={agent_mode}"
    return source or "unknown"


def render_route_policy_section(
    execution_route_policy: Optional[Mapping[str, Any]],
) -> str:
    """Render the conditional "Execution Route Policy" prompt section.

    Returns ``""`` when the policy is missing or malformed — the
    template's ``{route_policy_section}`` slot then collapses to empty
    and the prompt stays identical to the v7 shape.

    When a policy is present the section carries:

    - the forced LLM-facing route label (``plan_executor`` /
      ``simple_chat``)
    - the policy source (``agent_mode=plan`` / ``agent_mode=chat``)
    - an explicit instruction restating that routing label must follow
      the policy while target resolution, continuity, readiness, and
      interpretation must still be derived from the conversation

    Heuristic routes are NOT rewritten here. The caller passes the
    forced-route directive separately so the classifier sees both the
    normal heuristic hints and the authoritative execution target.
    """
    if not isinstance(execution_route_policy, Mapping):
        return ""

    forced_label = str(
        execution_route_policy.get("forced_classifier_label") or ""
    ).strip()
    if not forced_label:
        return ""

    policy_source = _resolve_policy_source(execution_route_policy)

    lines = [
        "Execution Route Policy:",
        f"- Forced routing label: {forced_label}",
        f"- Policy source: {policy_source}",
        (
            "- Instruction: the output `label` MUST equal the forced "
            "routing label. Continue to derive target resolution, prior "
            "target reuse, execution readiness, and the full turn "
            "interpretation from the conversation — do not invent "
            "grounding to match the forced route, and do not mark a "
            "blocked/unsafe request ready just because a route was "
            "forced."
        ),
    ]
    # Leading newline keeps the block separated from the preceding
    # environment section without editing the template — the template
    # places ``{route_policy_section}`` immediately after ``{environment}``.
    return "\n\n" + "\n".join(lines)


def render_subagent_catalog_section(
    subagent_catalog: Sequence[Mapping[str, Any]],
) -> str:
    """Render classifier-visible subagent specs supplied by the control plane."""
    lines = [
        "Registered Subagent Catalog:",
        (
            "- Select only a subagent listed below. The catalog is the authority "
            "for names and ownership boundaries."
        ),
    ]
    if not subagent_catalog:
        lines.append("- No subagents are currently available; return no handoffs.")
        return "\n".join(lines)

    for entry in subagent_catalog:
        name = str(entry.get("name") or "").strip()
        purpose = str(entry.get("purpose") or "").strip()
        ownership = str(entry.get("ownership_boundary") or "").strip()
        supported = _string_sequence(entry.get("supported_task_categories"))
        excluded = _string_sequence(entry.get("excluded_task_categories"))
        maximum = entry.get("max_active_runs_per_task")
        requires_target = bool(entry.get("requires_resolved_target"))
        if not name or not purpose or not ownership:
            raise ValueError("subagent catalog contains an incomplete specification")

        lines.extend(
            [
                f"- Name: {name}",
                f"  Purpose: {purpose}",
                f"  Ownership boundary: {ownership}",
                (
                    "  Supported task categories: "
                    + (", ".join(supported) if supported else "none")
                ),
                (
                    "  Excluded task categories: "
                    + (", ".join(excluded) if excluded else "none")
                ),
                f"  Maximum active runs per task: {maximum}",
                f"  Requires a resolved target: {'yes' if requires_target else 'no'}",
            ]
        )
    return "\n".join(lines)


def _string_sequence(value: Any) -> tuple[str, ...]:
    """Return non-empty prompt tokens from one catalog sequence."""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(
        normalized
        for item in value
        if (normalized := str(item or "").strip())
    )


def build_classifier_system_prompt(
    *,
    subagent_catalog: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Return the classifier prompt plus the runtime subagent catalog."""
    template = _registry.get_template("intent_classifier").rstrip()
    catalog_section = render_subagent_catalog_section(subagent_catalog)
    return f"{template}\n\n{catalog_section}\n"


def build_classifier_user_prompt(
    *,
    history: str,
    tool_hints: Any,
    targets: Any,
    eligible_routes: Any,
    risk_flags: Any,
    environment: str,
    execution_route_policy: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render the classifier user prompt with optional route-policy block.

    Inputs are deliberately raw runtime facts. Serialization of list
    hints happens here (``str(value)``) to keep behavioral parity with
    the previous service-local ``PROMPT_TEMPLATE.format(...)`` call.
    """
    template = _registry.get_template("intent_prompt_template")
    route_policy_section = render_route_policy_section(execution_route_policy)
    return template.format(
        history=history,
        tool_hints=tool_hints,
        targets=targets,
        eligible_routes=eligible_routes,
        risk_flags=risk_flags,
        environment=environment,
        route_policy_section=route_policy_section,
    )


__all__ = [
    "build_classifier_system_prompt",
    "build_classifier_user_prompt",
    "render_subagent_catalog_section",
    "render_route_policy_section",
]
