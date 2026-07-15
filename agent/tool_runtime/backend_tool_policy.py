"""Tool lane and route-policy helpers for runtime transport decisions.

This module owns execution-lane classification and simple allow/deny decisions
used by the transport router. Policy is intentionally explicit and fail-closed:
unknown tools are treated as container-scoped (never implicitly backend-scoped).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


CVE_LOOKUP_TOOL_ID = "knowledge.cve_lookup"

_BACKEND_SCOPED_TOOL_IDS: frozenset[str] = frozenset(
    {
        CVE_LOOKUP_TOOL_ID,
    }
)

ExecutionLane = Literal["container_scoped", "backend_scoped", "artifact_scoped"]
DispatchAuthority = Literal[
    "container_local_transport",
    "container_runner_transport",
    "backend_direct",
    "artifact_direct",
]
ToolSurfaceClass = Literal[
    "runtime_container_tool",
    "management_artifact_tool",
    "management_knowledge_tool",
    "unsupported_in_runner_v1",
]

_SUPPORTED_RUNTIME_PLACEMENT_MODES: frozenset[str] = frozenset({"local", "runner"})


@dataclass(frozen=True)
class RunnerRuntimeToolSupport:
    """Runner v1 support decision for one tool id."""

    tool_id: str
    supported: bool
    classification: ToolSurfaceClass
    error_code: str | None = None
    error_message: str | None = None


def is_backend_scoped_tool(tool_id: str) -> bool:
    """Return True when ``tool_id`` must execute in backend runtime scope."""
    normalized = str(tool_id or "").strip()
    return normalized in _BACKEND_SCOPED_TOOL_IDS


def normalize_runtime_placement_mode(value: object) -> str | None:
    """Return normalized runtime placement mode, or None when missing."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def require_runtime_placement_mode(value: object) -> str:
    """Return explicit runtime placement mode or raise a fail-closed error."""
    placement = normalize_runtime_placement_mode(value)
    if placement is None:
        raise ValueError(
            "tool execution runtime context requires explicit runtime_placement_mode."
        )
    if placement not in _SUPPORTED_RUNTIME_PLACEMENT_MODES:
        raise ValueError(
            "tool execution runtime context has unsupported runtime_placement_mode: "
            f"{placement}."
        )
    return placement


def iter_backend_scoped_tools(tool_ids: Iterable[str]) -> list[str]:
    """Return deterministic list of backend-scoped tool ids from ``tool_ids``."""
    return [str(tool_id) for tool_id in tool_ids if is_backend_scoped_tool(str(tool_id))]


def resolve_execution_lane(tool_id: str) -> ExecutionLane:
    """Return execution lane classification for ``tool_id``."""
    normalized = str(tool_id or "").strip()
    if is_backend_scoped_tool(normalized):
        return "backend_scoped"
    if normalized.startswith("artifact."):
        return "artifact_scoped"
    return "container_scoped"


def classify_tool_surface(tool_id: str) -> ToolSurfaceClass:
    """Classify tool ownership for execution_plane runtime packaging policy."""
    normalized = str(tool_id or "").strip()
    if is_backend_scoped_tool(normalized):
        return "management_knowledge_tool"
    if normalized.startswith("artifact."):
        return "management_artifact_tool"
    return "runtime_container_tool"


def resolve_runner_runtime_tool_support(tool_id: str) -> RunnerRuntimeToolSupport:
    """Return runner-runtime-image v1 support decision for ``tool_id``."""
    normalized = str(tool_id or "").strip()
    surface = classify_tool_surface(normalized)
    if surface == "runtime_container_tool":
        return RunnerRuntimeToolSupport(
            tool_id=normalized,
            supported=True,
            classification="runtime_container_tool",
        )
    if surface == "management_artifact_tool":
        return RunnerRuntimeToolSupport(
            tool_id=normalized,
            supported=False,
            classification="unsupported_in_runner_v1",
            error_code="unsupported_management_artifact_tool_runner_v1",
            error_message=(
                f"Tool `{normalized}` requires management-plane artifact services and "
                "is unavailable in runner runtime image v1."
            ),
        )
    return RunnerRuntimeToolSupport(
        tool_id=normalized,
        supported=False,
        classification="unsupported_in_runner_v1",
        error_code="unsupported_management_knowledge_tool_runner_v1",
        error_message=(
            f"Tool `{normalized}` requires management-plane knowledge/index services and "
            "is unavailable in runner runtime image v1."
        ),
    )


def is_supported_in_runner_runtime_image_v1(tool_id: str) -> bool:
    """Return True when ``tool_id`` is executable in runner runtime image v1."""
    return resolve_runner_runtime_tool_support(tool_id).supported


def lane_allows_pty(lane: ExecutionLane) -> bool:
    """Return whether ``lane`` may use PTY transport."""
    return lane == "container_scoped"


def lane_allows_file_comm(lane: ExecutionLane) -> bool:
    """Return whether ``lane`` may use file-comm transport."""
    return lane == "container_scoped"


def lane_allows_direct_execution(lane: ExecutionLane) -> bool:
    """Return whether ``lane`` may use direct in-process execution."""
    return lane in {"backend_scoped", "artifact_scoped"}


def resolve_selected_authority(
    *,
    lane: ExecutionLane,
    runtime_placement_mode: str | None,
) -> DispatchAuthority:
    """Resolve execution authority from lane + runtime placement mode."""
    placement = require_runtime_placement_mode(runtime_placement_mode)
    if lane == "backend_scoped":
        return "backend_direct"
    if lane == "artifact_scoped":
        return "artifact_direct"
    if placement == "runner":
        return "container_runner_transport"
    return "container_local_transport"


def build_route_policy_metadata(
    *,
    event: str,
    tool_id: str,
    lane: ExecutionLane,
    selected_authority: DispatchAuthority,
    selected_transport: str,
    fallback_reason: str = "",
) -> dict[str, str]:
    """Return deterministic route-policy metadata for logs and fail-closed results."""
    return {
        "event": str(event),
        "tool_id": str(tool_id),
        "selected_lane": str(lane),
        "selected_authority": str(selected_authority),
        "selected_transport": str(selected_transport),
        "fallback_reason": str(fallback_reason or ""),
    }


__all__ = [
    "CVE_LOOKUP_TOOL_ID",
    "ExecutionLane",
    "build_route_policy_metadata",
    "is_backend_scoped_tool",
    "iter_backend_scoped_tools",
    "lane_allows_direct_execution",
    "lane_allows_file_comm",
    "lane_allows_pty",
    "normalize_runtime_placement_mode",
    "require_runtime_placement_mode",
    "resolve_selected_authority",
    "classify_tool_surface",
    "is_supported_in_runner_runtime_image_v1",
    "resolve_runner_runtime_tool_support",
    "resolve_execution_lane",
    "RunnerRuntimeToolSupport",
    "DispatchAuthority",
    "ToolSurfaceClass",
]
