"""Tests for backend-scoped tool routing policy."""

from __future__ import annotations

import pytest

from agent.tool_runtime.backend_tool_policy import (
    build_route_policy_metadata,
    classify_tool_surface,
    is_backend_scoped_tool,
    is_supported_in_runner_runtime_image_v1,
    iter_backend_scoped_tools,
    lane_allows_direct_execution,
    lane_allows_file_comm,
    lane_allows_pty,
    require_runtime_placement_mode,
    resolve_selected_authority,
    resolve_runner_runtime_tool_support,
    resolve_execution_lane,
)


def test_backend_scoped_policy_marks_cve_lookup_only() -> None:
    assert is_backend_scoped_tool("knowledge.cve_lookup") is True
    assert is_backend_scoped_tool("information_gathering.network_discovery.nmap") is False


def test_iter_backend_scoped_tools_filters_input_order() -> None:
    tools = [
        "shell.exec",
        "knowledge.cve_lookup",
        "information_gathering.network_discovery.nmap",
    ]
    scoped = iter_backend_scoped_tools(tools)
    assert scoped == ["knowledge.cve_lookup"]


def test_resolve_execution_lane_is_explicit_and_fail_closed() -> None:
    assert resolve_execution_lane("knowledge.cve_lookup") == "backend_scoped"
    assert resolve_execution_lane("artifact.search") == "artifact_scoped"
    assert resolve_execution_lane("shell.exec") == "container_scoped"


def test_lane_transport_permissions_are_explicit() -> None:
    assert lane_allows_pty("container_scoped") is True
    assert lane_allows_file_comm("container_scoped") is True
    assert lane_allows_direct_execution("container_scoped") is False

    assert lane_allows_pty("backend_scoped") is False
    assert lane_allows_file_comm("backend_scoped") is False
    assert lane_allows_direct_execution("backend_scoped") is True

    assert lane_allows_pty("artifact_scoped") is False
    assert lane_allows_file_comm("artifact_scoped") is False
    assert lane_allows_direct_execution("artifact_scoped") is True


def test_build_route_policy_metadata_is_deterministic() -> None:
    metadata = build_route_policy_metadata(
        event="route_policy_violation",
        tool_id="shell.exec",
        lane="container_scoped",
        selected_authority="container_local_transport",
        selected_transport="blocked-direct",
        fallback_reason="pty_not_selected",
    )

    assert metadata == {
        "event": "route_policy_violation",
        "tool_id": "shell.exec",
        "selected_lane": "container_scoped",
        "selected_authority": "container_local_transport",
        "selected_transport": "blocked-direct",
        "fallback_reason": "pty_not_selected",
    }


def test_resolve_selected_authority_is_lane_and_placement_aware() -> None:
    assert (
        resolve_selected_authority(
            lane="container_scoped",
            runtime_placement_mode="local",
        )
        == "container_local_transport"
    )
    assert (
        resolve_selected_authority(
            lane="container_scoped",
            runtime_placement_mode="runner",
        )
        == "container_runner_transport"
    )
    assert (
        resolve_selected_authority(
            lane="backend_scoped",
            runtime_placement_mode="runner",
        )
        == "backend_direct"
    )
    assert (
        resolve_selected_authority(
            lane="artifact_scoped",
            runtime_placement_mode="runner",
        )
        == "artifact_direct"
    )


def test_runtime_placement_mode_must_be_explicit() -> None:
    assert require_runtime_placement_mode(" LOCAL ") == "local"
    assert require_runtime_placement_mode("runner") == "runner"
    with pytest.raises(ValueError, match="explicit runtime_placement_mode"):
        require_runtime_placement_mode(None)
    with pytest.raises(ValueError, match="unsupported runtime_placement_mode"):
        require_runtime_placement_mode("docker")


def test_tool_surface_classification_covers_execution_plane_runner_split() -> None:
    assert classify_tool_surface("shell.exec") == "runtime_container_tool"
    assert classify_tool_surface("artifact.search") == "management_artifact_tool"
    assert classify_tool_surface("knowledge.cve_lookup") == "management_knowledge_tool"


def test_runner_runtime_v1_support_is_fail_closed_for_management_tools() -> None:
    assert is_supported_in_runner_runtime_image_v1("shell.exec") is True
    assert is_supported_in_runner_runtime_image_v1("artifact.search") is False
    assert is_supported_in_runner_runtime_image_v1("knowledge.cve_lookup") is False

    artifact_decision = resolve_runner_runtime_tool_support("artifact.search")
    assert artifact_decision.classification == "unsupported_in_runner_v1"
    assert artifact_decision.error_code == "unsupported_management_artifact_tool_runner_v1"

    knowledge_decision = resolve_runner_runtime_tool_support("knowledge.cve_lookup")
    assert knowledge_decision.classification == "unsupported_in_runner_v1"
    assert knowledge_decision.error_code == "unsupported_management_knowledge_tool_runner_v1"
