"""Phase 1 lane-dispatch tests for per-call tool execution authority."""

from __future__ import annotations

import pytest

from agent.graph.subgraphs.tool_execution_runtime.lane_dispatch import (
    ToolCallDispatchInput,
    dispatch_tool_call_by_lane,
    resolve_tool_lane_dispatch,
)


def test_lane_dispatch_classifies_known_and_unknown_tools() -> None:
    cve = resolve_tool_lane_dispatch(
        tool_id="knowledge.cve_lookup",
        runtime_placement_mode="runner",
    )
    artifact = resolve_tool_lane_dispatch(
        tool_id="artifact.search",
        runtime_placement_mode="runner",
    )
    shell = resolve_tool_lane_dispatch(
        tool_id="shell.exec",
        runtime_placement_mode="runner",
    )
    filesystem = resolve_tool_lane_dispatch(
        tool_id="filesystem.read_file",
        runtime_placement_mode="runner",
    )
    pentest = resolve_tool_lane_dispatch(
        tool_id="information_gathering.network_discovery.nmap",
        runtime_placement_mode="runner",
    )
    unknown = resolve_tool_lane_dispatch(
        tool_id="unknown.custom.tool",
        runtime_placement_mode="runner",
    )

    assert cve.lane == "backend_scoped"
    assert cve.authority == "backend_direct"
    assert artifact.lane == "artifact_scoped"
    assert artifact.authority == "artifact_direct"
    assert shell.lane == "container_scoped"
    assert shell.authority == "container_runner_transport"
    assert filesystem.lane == "container_scoped"
    assert filesystem.authority == "container_runner_transport"
    assert pentest.lane == "container_scoped"
    assert pentest.authority == "container_runner_transport"
    assert unknown.lane == "container_scoped"
    assert unknown.authority == "container_runner_transport"


def test_runner_placement_keeps_backend_and_artifact_lanes_direct() -> None:
    backend = resolve_tool_lane_dispatch(
        tool_id="knowledge.cve_lookup",
        runtime_placement_mode="runner",
    )
    artifact = resolve_tool_lane_dispatch(
        tool_id="artifact.read",
        runtime_placement_mode="runner",
    )

    assert backend.authority == "backend_direct"
    assert artifact.authority == "artifact_direct"


def test_mixed_lane_batch_resolves_per_call_authority() -> None:
    calls = [
        resolve_tool_lane_dispatch(tool_id="shell.exec", runtime_placement_mode="runner"),
        resolve_tool_lane_dispatch(tool_id="knowledge.cve_lookup", runtime_placement_mode="runner"),
        resolve_tool_lane_dispatch(tool_id="artifact.search", runtime_placement_mode="runner"),
    ]

    authorities = [call.authority for call in calls]
    assert authorities == [
        "container_runner_transport",
        "backend_direct",
        "artifact_direct",
    ]


def test_local_placement_preserves_container_local_transport() -> None:
    decision = resolve_tool_lane_dispatch(
        tool_id="shell.exec",
        runtime_placement_mode="local",
    )
    assert decision.authority == "container_local_transport"


def test_lane_dispatch_requires_explicit_runtime_placement() -> None:
    with pytest.raises(ValueError, match="explicit runtime_placement_mode"):
        resolve_tool_lane_dispatch(
            tool_id="shell.exec",
            runtime_placement_mode=None,
        )


@pytest.mark.asyncio
async def test_missing_placement_fails_before_any_dispatch_callback() -> None:
    calls: list[str] = []

    async def _execute_local(*_args, **_kwargs):
        calls.append("local")
        return {"success": True, "metadata": {}}

    async def _execute_runner(*_args, **_kwargs):
        calls.append("runner")
        return {"success": True, "metadata": {}}

    result = await dispatch_tool_call_by_lane(
        dispatch_input=ToolCallDispatchInput(
            tool_id="shell.exec",
            normalized_parameters={"command": "echo ok"},
            timeout_plan=None,
            tool_call_id=None,
            tool_batch_id=None,
            runtime_placement_mode=None,
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
    )

    assert calls == []
    assert result["success"] is False
    assert result["status"] == "missing_runtime_placement"
    assert result["metadata"]["error_code"] == "missing_runtime_placement"


@pytest.mark.asyncio
async def test_runner_container_tool_dispatches_to_runner_callback() -> None:
    calls: list[str] = []

    async def _execute_local(*_args, **_kwargs):
        calls.append("local")
        return {"success": True, "metadata": {}}

    async def _execute_runner(_decision, _dispatch_input):
        calls.append("runner")
        return {"success": True, "metadata": {}}

    result = await dispatch_tool_call_by_lane(
        dispatch_input=ToolCallDispatchInput(
            tool_id="shell.exec",
            normalized_parameters={"command": "echo ok"},
            timeout_plan=None,
            tool_call_id=None,
            tool_batch_id=None,
            runtime_placement_mode="runner",
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
    )

    assert calls == ["runner"]
    assert result["success"] is True
    assert result["metadata"]["lane_dispatch"]["authority"] == "container_runner_transport"


@pytest.mark.asyncio
async def test_runner_unsupported_management_tool_fails_before_local_callback() -> None:
    calls: list[str] = []

    async def _execute_local(*_args, **_kwargs):
        calls.append("local")
        return {"success": True, "metadata": {}}

    async def _execute_runner(*_args, **_kwargs):
        calls.append("runner")
        return {"success": True, "metadata": {}}

    result = await dispatch_tool_call_by_lane(
        dispatch_input=ToolCallDispatchInput(
            tool_id="artifact.search",
            normalized_parameters={"query": "ioc"},
            timeout_plan=None,
            tool_call_id=None,
            tool_batch_id=None,
            runtime_placement_mode="runner",
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
    )

    assert calls == []
    assert result["success"] is False
    assert result["status"] == "unsupported_management_artifact_tool_runner_v1"
    assert result["metadata"]["route_policy"]["selected_transport"] == "blocked-pre-dispatch"
