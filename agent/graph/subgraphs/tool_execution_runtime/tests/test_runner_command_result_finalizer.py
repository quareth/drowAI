"""Tests for runner command result artifact finalization."""

from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from agent.graph.subgraphs.tool_execution_runtime import (
    runner_command_orchestration as orchestration,
    runner_command_result_finalizer as finalizer,
)
from agent.models import ExecutionResult
from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs
from agent.tool_runtime.workspace_artifacts import (
    WorkspaceArtifactSaveResult,
    WorkspaceIndexWrite,
)
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
    build_runtime_result,
)


def _runtime_request() -> RuntimeOperationRequest:
    return RuntimeOperationRequest(
        tenant_id=1,
        task_id=42,
        user_id=7,
        actor_type=RuntimeActorType.AGENT,
        actor_id="langgraph",
        runtime_placement_mode=RuntimePlacementMode.RUNNER,
        workspace_id="task-42",
        operation="send_tool_command",
        payload={},
        metadata={},
    )


def _prepared(
    tmp_path,
    *,
    tool_id: str = "information_gathering.network_discovery.nmap",
):
    return SimpleNamespace(
        tool_id=tool_id,
        tool=object(),
        args=SimpleNamespace(transport="file-comm"),
        host_workspace_path=str(tmp_path),
        runtime_context=None,
    )


def _prepared_http_download(tmp_path, **overrides: Any):
    args_data = {
        "target": "https://downloads.example.com/tool.bin",
        "output_path": "artifacts/download_2",
    }
    args_data.update(overrides)
    return SimpleNamespace(
        tool_id="information_gathering.web_enumeration.http_download",
        tool=HttpDownloadTool(),
        args=HttpDownloadArgs(**args_data),
        host_workspace_path=str(tmp_path),
        runtime_context=None,
    )


def _http_download_delegate() -> dict[str, Any]:
    return {
        "success": True,
        "stdout": (
            '__DROWAI_HTTP_DOWNLOAD_META__{"http_code":200,'
            '"url_effective":"https://downloads.example.com/tool.bin",'
            '"size_download":16,'
            '"num_redirects":0,'
            '"time_total":0.07}'
        ),
        "stderr": "",
        "exit_code": 0,
        "status": "success",
        "metadata": {"command_text": "curl ..."},
    }


def _runtime_query_result(request: RuntimeOperationRequest, *, sha: str, size: int):
    return build_runtime_result(
        request,
        accepted=True,
        provider="fake",
        status=RuntimeOperationStatus.SUCCEEDED,
        metadata={
            "delegate_result": {
                "metadata": {
                    "items": [
                        {
                            "path": "artifacts/download_2",
                            "size": size,
                            "content_sha256": sha,
                        }
                    ]
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_orchestration_finalize_payload_uses_compact_stdout_not_process_stdout() -> None:
    """Control-plane finalization must not re-promote raw process stdout as model stdout."""
    requests: list[RuntimeOperationRequest] = []

    class _Provider:
        async def finalize_tool_command_result(self, request):
            requests.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={"delegate_result": dict(request.payload)},
            )

    compact_stdout = '{"schema_version":"pcap.compact.v1"}'
    raw_stdout = '{"_source":{"layers":{"frame":{"frame.protocols":"ip:tcp"}}}}'

    result = await orchestration._finalize_and_promote_cloud_runner_command(
        provider=_Provider(),
        runtime_request=_runtime_request(),
        tool_id="sniffing_spoofing.network_sniffers.tshark",
        payload={"command_id": "cmd-1", "tool_call_id": "call-1"},
        provider_metadata={"runtime_job_id": "job-1"},
        enriched_delegate={
            "success": True,
            "status": "success",
            "exit_code": 0,
            "stdout": compact_stdout,
            "stderr": "",
            "process_stdout": raw_stdout,
            "process_stderr": "",
            "metadata": {"pcap_compact": {"schema_version": "pcap.compact.v1"}},
            "artifacts": [],
        },
        raw_delegate={"success": True, "exit_code": 0, "metadata": {}},
    )

    assert result is not None
    assert requests
    assert requests[0].payload["stdout"] == compact_stdout
    assert requests[0].payload["stdout"] != raw_stdout
    assert "_source" not in requests[0].payload["stdout"]


@pytest.mark.asyncio
async def test_finalizer_validates_runner_http_download_without_host_file(
    monkeypatch,
    tmp_path,
) -> None:
    content = b"download-content"
    sha = hashlib.sha256(content).hexdigest()
    query_requests: list[RuntimeOperationRequest] = []

    class _Provider:
        async def query_runtime_artifacts(self, request):
            query_requests.append(request)
            return _runtime_query_result(request, sha=sha, size=len(content))

    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        lambda **_kwargs: WorkspaceArtifactSaveResult(artifact_path=None),
    )
    runtime_request = _runtime_request()
    runtime_request.payload["command_id"] = "cmd-1"

    result = await finalizer.finalize_runner_command_result(
        prepared=_prepared_http_download(tmp_path, expected_sha256=sha),
        delegate=_http_download_delegate(),
        provider_ok=True,
        command="curl ...",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 60.0},
        provider=_Provider(),
        runtime_request=runtime_request,
        provider_metadata={"runtime_job_id": "tool-job-1"},
    )

    assert result is not None
    assert query_requests[0].payload["prefix"] == "artifacts/download_2"
    assert result["success"] is True
    assert result["artifacts"] == []
    assert result["metadata"]["bytes_written"] == len(content)
    assert result["metadata"]["sha256"] == sha
    assert result["metadata"]["checksum_verified"] is True
    assert result["metadata"]["postprocess_applied"] is True
    assert result["metadata"]["runtime_output_files"][0]["relative_path"] == "artifacts/download_2"
    assert not (tmp_path / "artifacts" / "download_2").exists()


@pytest.mark.asyncio
async def test_finalizer_rejects_runner_http_download_checksum_mismatch(
    monkeypatch,
    tmp_path,
) -> None:
    actual_sha = hashlib.sha256(b"wrong").hexdigest()

    class _Provider:
        async def query_runtime_artifacts(self, request):
            return _runtime_query_result(request, sha=actual_sha, size=5)

    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        lambda **_kwargs: WorkspaceArtifactSaveResult(artifact_path=None),
    )
    runtime_request = _runtime_request()
    runtime_request.payload["command_id"] = "cmd-1"

    result = await finalizer.finalize_runner_command_result(
        prepared=_prepared_http_download(tmp_path, expected_sha256="0" * 64),
        delegate=_http_download_delegate(),
        provider_ok=True,
        command="curl ...",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 60.0},
        provider=_Provider(),
        runtime_request=runtime_request,
        provider_metadata={"runtime_job_id": "tool-job-1"},
    )

    assert result is not None
    assert result["success"] is False
    assert result["exit_code"] == 3
    assert "sha256 checksum mismatch" in result["stderr"]
    assert result["metadata"]["sha256"] == actual_sha


@pytest.mark.asyncio
async def test_finalizer_rejects_runner_http_download_size_bounds(
    monkeypatch,
    tmp_path,
) -> None:
    sha = hashlib.sha256(b"tiny").hexdigest()

    class _Provider:
        async def query_runtime_artifacts(self, request):
            return _runtime_query_result(request, sha=sha, size=4)

    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        lambda **_kwargs: WorkspaceArtifactSaveResult(artifact_path=None),
    )
    runtime_request = _runtime_request()
    runtime_request.payload["command_id"] = "cmd-1"

    result = await finalizer.finalize_runner_command_result(
        prepared=_prepared_http_download(tmp_path, min_bytes=8),
        delegate=_http_download_delegate(),
        provider_ok=True,
        command="curl ...",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 60.0},
        provider=_Provider(),
        runtime_request=runtime_request,
        provider_metadata={"runtime_job_id": "tool-job-1"},
    )

    assert result is not None
    assert result["success"] is False
    assert result["exit_code"] == 3
    assert "downloaded file is smaller than min_bytes (4 < 8)" in result["stderr"]

    max_result = await finalizer.finalize_runner_command_result(
        prepared=_prepared_http_download(tmp_path, max_bytes=2),
        delegate=_http_download_delegate(),
        provider_ok=True,
        command="curl ...",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 60.0},
        provider=_Provider(),
        runtime_request=runtime_request,
        provider_metadata={"runtime_job_id": "tool-job-1"},
    )

    assert max_result is not None
    assert max_result["success"] is False
    assert max_result["exit_code"] == 3
    assert "downloaded file exceeds max_bytes (4 > 2)" in max_result["stderr"]


@pytest.mark.asyncio
async def test_finalizer_materializes_tool_artifact_raw_output_and_index(
    monkeypatch,
    tmp_path,
) -> None:
    writes: list[RuntimeOperationRequest] = []

    class _Provider:
        async def write_runtime_artifact_file(self, request):
            writes.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={"path": request.payload["path"]},
            )

    def _fake_enrich(**_kwargs: Any) -> ExecutionResult:
        artifact = tmp_path / "artifacts" / "tool.xml"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("<xml />", encoding="utf-8")
        result = ExecutionResult(True, "raw stdout", "", 0)
        result.artifacts = ["artifacts/tool.xml"]
        result.metadata = {"parsed": True}
        return result

    def _fake_save_and_index_tool_output_artifact(
        **_kwargs: Any,
    ) -> WorkspaceArtifactSaveResult:
        raw_artifact = tmp_path / "artifacts" / "123_tool.txt"
        raw_artifact.parent.mkdir(parents=True, exist_ok=True)
        raw_artifact.write_text("raw stdout", encoding="utf-8")
        index = tmp_path / "index" / "chunks_task-42.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        index_content = b'{"text":"raw stdout"}\n'
        index.write_bytes(index_content)
        return WorkspaceArtifactSaveResult(
            artifact_path="artifacts/123_tool.txt",
            index_writes=(
                WorkspaceIndexWrite(
                    path="index/chunks_task-42.jsonl",
                    content=index_content,
                ),
            ),
        )

    monkeypatch.setattr(finalizer, "build_command_transport_tool_result", _fake_enrich)
    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        _fake_save_and_index_tool_output_artifact,
    )

    delegate = {
        "success": True,
        "stdout": "raw stdout",
        "stderr": "",
        "exit_code": 0,
        "status": "success",
        "metadata": {"command_text": "nmap 127.0.0.1"},
    }
    result = await finalizer.finalize_runner_command_result(
        prepared=_prepared(tmp_path),
        delegate=delegate,
        provider_ok=True,
        command="nmap 127.0.0.1",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 30.0},
        provider=_Provider(),
        runtime_request=_runtime_request(),
    )

    assert result is not None
    assert result["artifacts"] == ["artifacts/tool.xml", "artifacts/123_tool.txt"]
    written_paths = [write.payload["path"] for write in writes]
    assert written_paths == [
        "artifacts/tool.xml",
        "artifacts/123_tool.txt",
        "index/chunks_task-42.jsonl",
    ]
    assert writes[-1].payload["mode"] == "append"
    assert not (tmp_path / "artifacts" / "tool.xml").exists()
    assert not (tmp_path / "artifacts" / "123_tool.txt").exists()
    assert (tmp_path / "index" / "chunks_task-42.jsonl").exists()


@pytest.mark.asyncio
async def test_finalizer_keeps_failed_materialization_staging_file(
    monkeypatch,
    tmp_path,
) -> None:
    class _Provider:
        async def write_runtime_artifact_file(self, request):
            return build_runtime_result(
                request,
                accepted=False,
                provider="fake",
                status=RuntimeOperationStatus.REJECTED,
                error_code="WRITE_REJECTED",
            )

    def _fake_enrich(**_kwargs: Any) -> ExecutionResult:
        artifact = tmp_path / "artifacts" / "tool.xml"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("<xml />", encoding="utf-8")
        result = ExecutionResult(True, "raw stdout", "", 0)
        result.artifacts = ["artifacts/tool.xml"]
        result.metadata = {}
        return result

    monkeypatch.setattr(finalizer, "build_command_transport_tool_result", _fake_enrich)
    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        lambda **_kwargs: WorkspaceArtifactSaveResult(artifact_path=None),
    )

    result = await finalizer.finalize_runner_command_result(
        prepared=_prepared(tmp_path),
        delegate={
            "success": True,
            "stdout": "raw stdout",
            "stderr": "",
            "exit_code": 0,
        },
        provider_ok=True,
        command="nmap 127.0.0.1",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 30.0},
        provider=_Provider(),
        runtime_request=_runtime_request(),
    )

    assert result is not None
    assert result["artifacts"] == []
    assert result["metadata"]["artifact_materialization"]["status"] == "failed"
    assert (tmp_path / "artifacts" / "tool.xml").exists()


@pytest.mark.asyncio
async def test_finalizer_does_not_persist_raw_artifact_for_skipped_tools(
    monkeypatch,
    tmp_path,
) -> None:
    writes: list[RuntimeOperationRequest] = []

    class _Provider:
        async def write_runtime_artifact_file(self, request):
            writes.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
            )

    def _fake_enrich(**_kwargs: Any) -> ExecutionResult:
        result = ExecutionResult(True, "file content", "", 0)
        result.artifacts = []
        result.metadata = {}
        return result

    monkeypatch.setattr(finalizer, "build_command_transport_tool_result", _fake_enrich)

    result = await finalizer.finalize_runner_command_result(
        prepared=_prepared(tmp_path, tool_id="filesystem.read_file"),
        delegate={
            "success": True,
            "stdout": "file content",
            "stderr": "",
            "exit_code": 0,
        },
        provider_ok=True,
        command="cat artifacts/example.txt",
        artifact_stamp=None,
        timeout_policy={"deadline_seconds": 30.0},
        provider=_Provider(),
        runtime_request=_runtime_request(),
    )

    assert result is not None
    assert result["artifacts"] == []
    assert writes == []
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_finalizer_preserves_dual_output_channels_for_fping(
    monkeypatch,
    tmp_path,
) -> None:
    """Model stdout stays compact; process_stdout/stderr stay raw for evidence paths."""
    from agent.tools.information_gathering.network_discovery.fping import (
        FpingArgs,
        FpingTool,
    )

    raw_stdout = "172.17.0.1\n"
    raw_stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "       2 targets\n       1 alive\n       1 unreachable\n"
    )
    saved_payloads: list[dict[str, Any]] = []

    class _Provider:
        async def write_runtime_artifact_file(self, request):
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
            )

    def _capture_save_and_index(**kwargs: Any) -> WorkspaceArtifactSaveResult:
        saved_payloads.append(dict(kwargs))
        artifact = tmp_path / "artifacts" / "captured_tool.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        combined = kwargs.get("stdout") or ""
        stderr = kwargs.get("stderr") or ""
        if stderr:
            combined = f"{combined}\n\n=== STDERR ===\n{stderr}"
        artifact.write_text(combined, encoding="utf-8")
        return WorkspaceArtifactSaveResult(artifact_path="artifacts/captured_tool.txt")

    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        _capture_save_and_index,
    )

    prepared = SimpleNamespace(
        tool_id="information_gathering.network_discovery.fping",
        tool=FpingTool(),
        args=FpingArgs(target="172.17.0.0/24"),
        host_workspace_path=str(tmp_path),
        runtime_context=None,
    )
    delegate = {
        "success": False,
        "stdout": raw_stdout,
        "stderr": raw_stderr,
        "exit_code": 1,
        "status": "completed",
        "metadata": {"command_text": "fping -a -s -r 1 -p 1000 -g 172.17.0.0/24"},
    }

    result = await finalizer.finalize_runner_command_result(
        prepared=prepared,
        delegate=delegate,
        provider_ok=True,
        command="fping -a -s -r 1 -p 1000 -g 172.17.0.0/24",
        artifact_stamp=1234567890,
        timeout_policy={"deadline_seconds": 30.0},
        provider=_Provider(),
        runtime_request=_runtime_request(),
    )

    assert result is not None
    assert result["process_stdout"] == raw_stdout
    assert result["process_stderr"] == raw_stderr
    assert "Alive hosts: 1" in result["stdout"]
    assert "xmt/rcv/%loss" not in result["stdout"]
    assert result["stderr"] == ""

    assert saved_payloads, "expected save_and_index to receive raw process output"
    assert saved_payloads[0]["stdout"] == raw_stdout
    assert saved_payloads[0]["stderr"] == raw_stderr
    assert "Alive hosts:" not in saved_payloads[0]["stdout"]

    fping_artifacts = [
        path for path in result.get("artifacts") or [] if str(path).startswith("artifacts/fping_")
    ]
    assert fping_artifacts, "expected tool-native fping artifact path in delegate"


@pytest.mark.asyncio
async def test_finalizer_keeps_tshark_transport_output_and_artifacts_raw(
    monkeypatch,
    tmp_path,
) -> None:
    """TShark model stdout is compact while process/artifact output stays raw."""
    from agent.tools.sniffing_spoofing.network_sniffers.tshark import (
        TSharkArgs,
        TSharkTool,
    )

    raw_secrets = [
        "synthetic-bearer-token",
        "synthetic-session-secret",
        "synthetic-password",
        "synthetic-api-token",
    ]
    stdout = json.dumps(
        [
            {
                "_source": {
                    "layers": {
                        "frame": {
                            "frame.number": "1",
                            "frame.time": "Jun 14, 2026 10:00:00.000000000 UTC",
                            "frame.protocols": "eth:ip:tcp:http",
                            "frame.len": "512",
                        },
                        "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
                        "tcp": {
                            "tcp.srcport": "49152",
                            "tcp.dstport": "80",
                            "tcp.stream": "7",
                        },
                        "http": {
                            "http.host": "app.example.test",
                            "http.request.method": "GET",
                            "http.request.uri": "http://app.example.test/login",
                            "http.authorization": "Bearer synthetic-bearer-token",
                            "http.cookie": "session=synthetic-session-secret",
                        },
                    }
                }
            }
        ]
    )
    stderr = "\n".join(
        [
            "Warning: password=synthetic-password",
            "Warning: X-Api-Key: synthetic-api-token",
        ]
    )
    saved_payloads: list[dict[str, Any]] = []

    class _Provider:
        async def write_runtime_artifact_file(self, request):
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
            )

    def _capture_save_and_index(**kwargs: Any) -> WorkspaceArtifactSaveResult:
        saved_payloads.append(dict(kwargs))
        artifact = tmp_path / "artifacts" / "captured_tshark.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        combined = kwargs.get("stdout") or ""
        stderr_text = kwargs.get("stderr") or ""
        if stderr_text:
            combined = f"{combined}\n\n=== STDERR ===\n{stderr_text}"
        artifact.write_text(combined, encoding="utf-8")
        return WorkspaceArtifactSaveResult(artifact_path="artifacts/captured_tshark.txt")

    monkeypatch.setattr(
        finalizer,
        "save_and_index_tool_output_artifact_with_index_writes",
        _capture_save_and_index,
    )

    prepared = SimpleNamespace(
        tool_id="sniffing_spoofing.network_sniffers.tshark",
        tool=TSharkTool(),
        args=TSharkArgs(
            target="unused",
            input_file="captures/example.pcap",
            analysis_mode="http",
        ),
        host_workspace_path=str(tmp_path),
        runtime_context=None,
    )

    result = await finalizer.finalize_runner_command_result(
        prepared=prepared,
        delegate={
            "success": True,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": 0,
            "status": "completed",
            "metadata": {"command_text": "tshark -r captures/example.pcap -T json"},
        },
        provider_ok=True,
        command="tshark -r captures/example.pcap -T json",
        artifact_stamp=1234567890,
        timeout_policy={"deadline_seconds": 30.0},
        provider=_Provider(),
        runtime_request=_runtime_request(),
    )

    assert result is not None
    assert saved_payloads, "expected standard tool output artifact persistence"
    assert result["process_stdout"] == stdout
    assert result["process_stderr"] == stderr
    assert result["stderr"] == ""
    assert "pcap.compact.v1" in result["stdout"]
    assert "_source" not in result["stdout"]

    serialized = json.dumps(
        {
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
            "process_stdout": result.get("process_stdout"),
            "process_stderr": result.get("process_stderr"),
            "metadata": result.get("metadata"),
            "artifacts": result.get("artifacts"),
            "saved_payloads": saved_payloads,
        },
        sort_keys=True,
    )
    for secret in raw_secrets:
        assert secret in serialized
    assert saved_payloads[0]["stdout"] == stdout
    assert saved_payloads[0]["stderr"] == stderr
