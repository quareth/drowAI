"""Phase 1 lane-dispatch tests for per-call tool execution authority."""

from __future__ import annotations

import pytest

from agent.graph.subgraphs.tool_execution_runtime.lane_dispatch import (
    ToolCallDispatchInput,
    dispatch_tool_call_by_lane,
    resolve_tool_lane_dispatch,
)


async def _unexpected_execute_session(*_args, **_kwargs):
    raise AssertionError("session callback should not be called")


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
    assert shell.lane == "runtime_session_scoped"
    assert shell.authority == "runtime_session_control"
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
        "runtime_session_control",
        "backend_direct",
        "artifact_direct",
    ]


def test_shell_exec_uses_runtime_session_control_for_local_placement() -> None:
    decision = resolve_tool_lane_dispatch(
        tool_id="shell.exec",
        runtime_placement_mode="local",
    )
    assert decision.authority == "runtime_session_control"


def test_shell_write_stdin_uses_runtime_session_control_for_all_placements() -> None:
    local = resolve_tool_lane_dispatch(
        tool_id="shell.write_stdin",
        runtime_placement_mode="local",
    )
    runner = resolve_tool_lane_dispatch(
        tool_id="shell.write_stdin",
        runtime_placement_mode="runner",
    )

    assert local.lane == "runtime_session_scoped"
    assert local.authority == "runtime_session_control"
    assert runner.lane == "runtime_session_scoped"
    assert runner.authority == "runtime_session_control"


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
        execute_session=_unexpected_execute_session,
    )

    assert calls == []
    assert result["success"] is False
    assert result["status"] == "missing_runtime_placement"
    assert result["metadata"]["error_code"] == "missing_runtime_placement"


@pytest.mark.asyncio
async def test_runtime_session_missing_owner_fails_before_any_dispatch_callback() -> None:
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
            runtime_placement_mode="runner",
            tenant_id=3,
            task_id=5,
            runtime_metadata={"workspace_id": "task-5"},
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
        execute_session=_unexpected_execute_session,
    )

    assert calls == []
    assert result["success"] is False
    assert result["status"] == "missing_shell_session_identity"
    assert result["metadata"]["error_code"] == "missing_shell_session_identity"
    assert "execution_owner_id" in result["stderr"]


@pytest.mark.asyncio
async def test_runtime_session_dispatch_uses_session_callback_before_one_shot_transports() -> None:
    calls: list[str] = []

    async def _execute_local(*_args, **_kwargs):
        calls.append("local")
        return {"success": True, "metadata": {}}

    async def _execute_runner(*_args, **_kwargs):
        calls.append("runner")
        return {"success": True, "metadata": {}}

    async def _execute_session(_decision, lane_input):
        calls.append("session")
        assert lane_input.normalized_parameters == {"command": "sleep 10"}
        return {"success": True, "metadata": {}}

    result = await dispatch_tool_call_by_lane(
        dispatch_input=ToolCallDispatchInput(
            tool_id="shell.exec",
            normalized_parameters={"command": "sleep 10"},
            timeout_plan=None,
            tool_call_id="call-shell",
            tool_batch_id="batch-shell",
            runtime_placement_mode="runner",
            tenant_id=3,
            task_id=5,
            execution_owner_id="main:turn-1",
            runtime_metadata={"workspace_id": "task-5"},
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
        execute_session=_execute_session,
    )

    assert calls == ["session"]
    assert result["success"] is True
    assert result["metadata"]["route_policy"]["selected_lane"] == "runtime_session_scoped"
    assert (
        result["metadata"]["route_policy"]["selected_authority"]
        == "runtime_session_control"
    )
    assert result["metadata"]["lane_dispatch"]["lane"] == "runtime_session_scoped"
    assert result["metadata"]["lane_dispatch"]["authority"] == "runtime_session_control"


@pytest.mark.asyncio
async def test_container_tool_missing_owner_still_uses_existing_dispatch() -> None:
    calls: list[str] = []

    async def _execute_local(*_args, **_kwargs):
        calls.append("local")
        return {"success": True, "metadata": {}}

    async def _execute_runner(*_args, **_kwargs):
        calls.append("runner")
        return {"success": True, "metadata": {}}

    result = await dispatch_tool_call_by_lane(
        dispatch_input=ToolCallDispatchInput(
            tool_id="filesystem.read_file",
            normalized_parameters={"path": "/workspace/file.txt"},
            timeout_plan=None,
            tool_call_id=None,
            tool_batch_id=None,
            runtime_placement_mode="runner",
            tenant_id=3,
            task_id=5,
            runtime_metadata={"workspace_id": "task-5"},
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
        execute_session=_unexpected_execute_session,
    )

    assert calls == ["runner"]
    assert result["success"] is True


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
            tool_id="filesystem.read_file",
            normalized_parameters={"path": "/workspace/file.txt"},
            timeout_plan=None,
            tool_call_id=None,
            tool_batch_id=None,
            runtime_placement_mode="runner",
        ),
        execute_local=_execute_local,
        execute_runner=_execute_runner,
        execute_session=_unexpected_execute_session,
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
        execute_session=_unexpected_execute_session,
    )

    assert calls == []
    assert result["success"] is False
    assert result["status"] == "unsupported_management_artifact_tool_runner_v1"
    assert result["metadata"]["route_policy"]["selected_transport"] == "blocked-pre-dispatch"
