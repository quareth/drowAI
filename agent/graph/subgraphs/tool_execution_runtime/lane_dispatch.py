"""Per-call execution-lane dispatch helpers for tool execution runtime.

This module owns deterministic lane/authority decisions for a single tool
call and the small dispatcher that invokes the selected per-call authority.
The boundaries stay explicit so orchestration and tests can assert routing
behavior without coupling to executor internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from agent.tool_runtime.backend_tool_policy import (
    DispatchAuthority,
    build_route_policy_metadata,
    classify_tool_surface,
    require_runtime_placement_mode,
    resolve_execution_lane,
    resolve_runner_runtime_tool_support,
    resolve_selected_authority,
)


@dataclass(frozen=True, slots=True)
class ToolLaneDispatchDecision:
    """Resolved execution lane + authority for one tool call."""

    tool_id: str
    lane: str
    runtime_placement_mode: str
    authority: DispatchAuthority

    def as_metadata(self) -> dict[str, str]:
        """Return stable metadata shape for diagnostics."""
        return {
            "tool_id": self.tool_id,
            "lane": self.lane,
            "runtime_placement_mode": self.runtime_placement_mode,
            "authority": self.authority,
        }


@dataclass(frozen=True, slots=True)
class ToolCallDispatchInput:
    """Normalized per-call input required to select + execute an authority."""

    tool_id: str
    normalized_parameters: Mapping[str, Any]
    timeout_plan: Any
    tool_call_id: str | None
    tool_batch_id: str | None
    runtime_placement_mode: str | None
    tenant_id: int | None = None
    task_id: int | None = None
    runtime_metadata: Mapping[str, Any] | None = None


def resolve_tool_lane_dispatch(
    *,
    tool_id: str,
    runtime_placement_mode: str | None,
) -> ToolLaneDispatchDecision:
    """Resolve per-call lane authority for the current placement mode."""
    lane = resolve_execution_lane(tool_id)
    placement = require_runtime_placement_mode(runtime_placement_mode)
    authority = resolve_selected_authority(
        lane=lane,
        runtime_placement_mode=placement,
    )

    return ToolLaneDispatchDecision(
        tool_id=str(tool_id),
        lane=lane,
        runtime_placement_mode=placement,
        authority=authority,
    )


def _dispatch_error_payload(
    *,
    tool_id: str,
    decision: ToolLaneDispatchDecision | None,
    message: str,
    status: str,
    error_code: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable graph-tool failure payload before transport dispatch."""
    metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
    if decision is not None:
        metadata_map.setdefault(
            "route_policy",
            {
                "selected_lane": decision.lane,
                "selected_authority": decision.authority,
            },
        )
        metadata_map.setdefault("lane_dispatch", decision.as_metadata())
    metadata_map.setdefault("error_code", error_code)
    return {
        "tool": tool_id,
        "success": False,
        "stdout": "",
        "stderr": message,
        "stdout_excerpt": "",
        "stderr_excerpt": message,
        "exit_code": 2,
        "observation": message,
        "approval_granted": True,
        "approval_reason": None,
        "approval_metadata": {},
        "duration": 0.0,
        "metadata": metadata_map,
        "status": status,
    }


def runner_unsupported_tool_payload(
    *,
    decision: ToolLaneDispatchDecision,
) -> dict[str, Any] | None:
    """Return a fail-closed payload for runner-v1 unsupported tool surfaces."""
    if decision.runtime_placement_mode != "runner":
        return None
    support = resolve_runner_runtime_tool_support(decision.tool_id)
    if support.supported:
        return None
    error_code = str(support.error_code or "unsupported_in_runner_v1")
    message = str(
        support.error_message
        or f"Tool `{decision.tool_id}` is unsupported in runner runtime image v1."
    )
    route_policy = build_route_policy_metadata(
        event="runner_tool_unsupported",
        tool_id=decision.tool_id,
        lane=decision.lane,
        selected_authority=decision.authority,
        selected_transport="blocked-pre-dispatch",
        fallback_reason=error_code,
    )
    return _dispatch_error_payload(
        tool_id=decision.tool_id,
        decision=decision,
        message=message,
        status=error_code,
        error_code=error_code,
        metadata={
            "route_policy": route_policy,
            "runner_tool_policy": {
                "runtime_placement_mode": decision.runtime_placement_mode,
                "supported": False,
                "classification": support.classification,
                "tool_surface_classification": classify_tool_surface(decision.tool_id),
            },
        },
    )


def missing_runtime_placement_payload(*, tool_id: str, message: str) -> dict[str, Any]:
    """Return a fail-closed payload for missing placement before local fallback."""
    return _dispatch_error_payload(
        tool_id=tool_id,
        decision=None,
        message=message,
        status="missing_runtime_placement",
        error_code="missing_runtime_placement",
    )


async def dispatch_tool_call_by_lane(
    *,
    dispatch_input: ToolCallDispatchInput,
    execute_local: Callable[
        [ToolLaneDispatchDecision, ToolCallDispatchInput],
        Awaitable[dict[str, Any]],
    ],
    execute_runner: Callable[
        [ToolLaneDispatchDecision, ToolCallDispatchInput],
        Awaitable[dict[str, Any]],
    ],
) -> dict[str, Any]:
    """Execute one tool call via the selected lane authority."""
    try:
        decision = resolve_tool_lane_dispatch(
            tool_id=dispatch_input.tool_id,
            runtime_placement_mode=dispatch_input.runtime_placement_mode,
        )
    except ValueError as exc:
        return missing_runtime_placement_payload(
            tool_id=dispatch_input.tool_id,
            message=str(exc),
        )

    unsupported_payload = runner_unsupported_tool_payload(decision=decision)
    if unsupported_payload is not None:
        return unsupported_payload

    if decision.authority == "container_runner_transport":
        result = await execute_runner(decision, dispatch_input)
    else:
        result = await execute_local(decision, dispatch_input)

    metadata = result.get("metadata")
    metadata_map = dict(metadata) if isinstance(metadata, dict) else {}
    metadata_map.setdefault(
        "route_policy",
        {
            "selected_lane": decision.lane,
            "selected_authority": decision.authority,
        },
    )
    metadata_map.setdefault("lane_dispatch", decision.as_metadata())
    result["metadata"] = metadata_map
    return result
