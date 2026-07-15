"""Tests for tooling_plane remote provider result projection into graph tool-result shape."""

from __future__ import annotations

import pytest

from agent.graph.compression.compressor import compress_tool_output
from agent.graph.subgraphs.tool_execution_runtime.remote_result_adapter import (
    adapt_remote_tool_result,
)


def test_remote_result_adapter_normalizes_succeeded_status_to_success() -> None:
    result = adapt_remote_tool_result(
        tool_id="shell.exec",
        provider_ok=True,
        provider_error_code=None,
        provider_error_message=None,
        provider_metadata={
            "runtime_job_id": "job-1",
            "command_id": "cmd-1",
            "workspace_id": "task-5",
        },
        delegate_result={
            "status": "succeeded",
            "success": True,
            "stdout": "ok",
            "stderr": "",
            "exit_code": 0,
            "result": {"command_text": "echo ok"},
            "metadata": {"runner_id": "runner-1"},
        },
        duration_seconds=0.12,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 30.0, "grace_seconds": 2.0},
        missing_result=False,
    )

    assert result["success"] is True
    assert result["status"] == "success"
    assert result["command_text"] == "echo ok"
    assert result["metadata"]["runtime_job_id"] == "job-1"
    assert result["metadata"]["command_id"] == "cmd-1"
    assert result["metadata"]["workspace_id"] == "task-5"
    assert result["metadata"]["runner_id"] == "runner-1"


def test_remote_result_adapter_marks_runner_artifacts_as_unpromoted() -> None:
    result = adapt_remote_tool_result(
        tool_id="information_gathering.network_discovery.nmap",
        provider_ok=True,
        provider_error_code=None,
        provider_error_message=None,
        provider_metadata={},
        delegate_result={
            "status": "success",
            "success": True,
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
            "artifacts": ["/workspace/artifacts/scan.xml"],
            "metadata": {},
        },
        duration_seconds=0.08,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 30.0, "grace_seconds": 2.0},
        missing_result=False,
    )

    assert result["artifacts"] == ["/workspace/artifacts/scan.xml"]
    assert result["metadata"]["artifact_scope"] == "runner_local"
    assert result["metadata"]["artifact_promotion_status"] == "unpromoted"
    assert result["metadata"]["artifact_visibility"] == "runner_workspace_only"


def test_remote_result_adapter_projects_promoted_artifacts_as_cloud_data_plane() -> None:
    result = adapt_remote_tool_result(
        tool_id="information_gathering.network_discovery.nmap",
        provider_ok=True,
        provider_error_code=None,
        provider_error_message=None,
        provider_metadata={},
        delegate_result={
            "status": "success",
            "success": True,
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
            "artifacts": ["/workspace/artifacts/scan.xml"],
            "metadata": {
                "promoted_artifact_ids": ["artifact-1"],
            },
        },
        duration_seconds=0.08,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 30.0, "grace_seconds": 2.0},
        missing_result=False,
    )

    assert result["metadata"]["artifact_scope"] == "cloud_data_plane"
    assert result["metadata"]["artifact_promotion_status"] == "ready"
    assert result["metadata"]["artifact_visibility"] == "artifact_catalog"


def test_remote_result_adapter_projects_cloud_artifact_refs_without_object_keys() -> None:
    result = adapt_remote_tool_result(
        tool_id="information_gathering.network_discovery.nmap",
        provider_ok=True,
        provider_error_code=None,
        provider_error_message=None,
        provider_metadata={},
        delegate_result={
            "status": "success",
            "success": True,
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
            "artifacts": ["/workspace/artifacts/scan.xml"],
            "metadata": {
                "artifact_promotion_status": "ready",
                "artifact_refs": [
                    {
                        "artifact_id": "artifact-1",
                        "relative_path": "artifacts/scan.xml",
                    }
                ],
            },
        },
        duration_seconds=0.08,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 30.0, "grace_seconds": 2.0},
        missing_result=False,
    )

    assert result["metadata"]["artifact_scope"] == "cloud_data_plane"
    assert result["metadata"]["artifact_promotion_status"] == "ready"
    assert result["metadata"]["artifact_visibility"] == "artifact_catalog"
    refs = result["metadata"]["artifact_refs"]
    assert refs[0]["artifact_id"] == "artifact-1"
    assert "object_key" not in refs[0]


def test_remote_result_adapter_projects_missing_delegate_as_failed_result() -> None:
    result = adapt_remote_tool_result(
        tool_id="shell.exec",
        provider_ok=True,
        provider_error_code=None,
        provider_error_message=None,
        provider_metadata={"error_code": "tool_result_missing"},
        delegate_result=None,
        duration_seconds=0.01,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 10.0, "grace_seconds": 1.0},
        missing_result=True,
    )

    assert result["success"] is False
    assert result["status"] == "tool_result_missing"
    assert result["metadata"]["error_code"] == "tool_result_missing"
    assert "did not return a terminal result" in result["stderr"]


def test_remote_result_adapter_does_not_leak_provider_error_into_tool_success() -> None:
    result = adapt_remote_tool_result(
        tool_id="information_gathering.network_discovery.fping",
        provider_ok=False,
        provider_error_code="RUNNER_TOOL_COMMAND_FAILED",
        provider_error_message="Runner tool.command runtime job failed before tool result projection.",
        provider_metadata={
            "runtime_job_id": "job-fping",
            "runtime_job_status": "failed",
            "error_code": "RUNNER_TOOL_COMMAND_FAILED",
        },
        delegate_result={
            "status": "success",
            "success": True,
            "stdout": "Alive hosts: 4\n172.17.0.1\n172.17.0.2\n172.17.0.3\n172.17.0.4",
            "stderr": "",
            "exit_code": 1,
            "metadata": {},
        },
        duration_seconds=7.89,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 600.0, "grace_seconds": 5.0},
        missing_result=False,
    )

    assert result["success"] is True
    assert result["status"] == "success"
    assert result["exit_code"] == 1
    assert result["stderr"] == ""
    assert "error_code" not in result["metadata"]
    assert result["metadata"]["runner_provider_diagnostics"] == {
        "provider_error_code": "RUNNER_TOOL_COMMAND_FAILED",
        "runtime_job_status": "failed",
        "provider_error_message": "Runner tool.command runtime job failed before tool result projection.",
    }


@pytest.mark.asyncio
async def test_provider_projection_error_does_not_become_compact_key_finding() -> None:
    adapted = adapt_remote_tool_result(
        tool_id="information_gathering.network_discovery.fping",
        provider_ok=False,
        provider_error_code="RUNNER_TOOL_COMMAND_FAILED",
        provider_error_message="Runner tool.command runtime job failed before tool result projection.",
        provider_metadata={"runtime_job_status": "failed"},
        delegate_result={
            "status": "success",
            "success": True,
            "stdout": (
                "Alive hosts: 4\n"
                "Unresponsive hosts: 250\n"
                "172.17.0.1\n"
                "172.17.0.2\n"
                "172.17.0.3\n"
                "172.17.0.4"
            ),
            "stderr": "",
            "exit_code": 1,
            "metadata": {},
        },
        duration_seconds=7.89,
        route_policy={"selected_lane": "container_scoped", "selected_authority": "container_runner_transport"},
        timeout_policy={"deadline_seconds": 600.0, "grace_seconds": 5.0},
        missing_result=False,
    )

    compression = await compress_tool_output(
        tool_name="information_gathering.network_discovery.fping",
        raw_result={**adapted, "parameters": {}},
        artifact_path=None,
        execution_id=None,
        llm_client=object(),
    )

    compact = compression.compact_output.to_dict()
    assert compact["summary"] == "Alive hosts: 4"
    assert "172.17.0.1" in compact["key_findings"]
    assert all(
        "Runner tool.command runtime job failed" not in finding
        for finding in compact["key_findings"]
    )
