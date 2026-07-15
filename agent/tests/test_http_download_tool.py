"""Unit coverage for HTTP download path safety and metadata."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs
from agent.tools.tool_registry import run_tool_by_name
from agent.tool_runtime.timeout_policy import resolve_tool_timeout_plan


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "http_tools"


def _fixture_text(*parts: str) -> str:
    return (FIXTURE_ROOT / Path(*parts)).read_text(encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    previous = os.environ.get("WORKSPACE")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(cwd)
        if previous is None:
            monkeypatch.delenv("WORKSPACE", raising=False)
        else:
            monkeypatch.setenv("WORKSPACE", previous)


def test_build_command_uses_resume_when_partial_file_exists(workspace: Path):
    (workspace / "downloads").mkdir(parents=True, exist_ok=True)
    (workspace / "downloads" / "tool.bin").write_bytes(b"partial")
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://example.com/tool.bin",
        output_path="downloads/tool.bin",
        resume=True,
    )

    cmd = tool.build_command(args)
    assert cmd[0] == "curl"
    assert "--continue-at" in cmd
    assert "--output" in cmd
    out_idx = cmd.index("--output") + 1
    assert cmd[out_idx] == "downloads/tool.bin"


def test_build_command_declares_missing_output_parent_without_creating_it(workspace: Path):
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://example.com/tool.bin",
        output_path="downloads/nested/tool.bin",
        create_parents=True,
    )

    cmd = tool.build_command(args)
    workspace_dirs = tool.prepare_workspace_directories(args)

    assert "--output" in cmd
    assert "downloads/nested/tool.bin" in cmd
    assert not (workspace / "downloads").exists()
    assert [item.relative_path for item in workspace_dirs] == ["downloads/nested"]


def test_build_command_uses_json_write_out_without_terminal_control_delimiters(workspace: Path):
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://example.com/tool.bin",
        output_path="downloads/tool.bin",
    )

    cmd = tool.build_command(args)
    write_out = cmd[cmd.index("--write-out") + 1]

    assert write_out == "__DROWAI_HTTP_DOWNLOAD_META__%{json}"
    assert "\t" not in write_out
    assert "\n" not in write_out


def test_timeout_policy_sets_http_download_native_timeout():
    config = SimpleNamespace(tool_timeout_default_seconds=900, tool_timeout_max_seconds=1200)

    plan = resolve_tool_timeout_plan(
        tool_id="information_gathering.web_enumeration.http_download",
        parameters={
            "target": "https://example.com/tool.bin",
            "output_path": "downloads/tool.bin",
        },
        config=config,
    )

    assert plan.native_timeout_field == "timeout"
    assert plan.normalized_parameters["timeout"] == 900


def test_parse_output_reads_json_write_out_metadata(workspace: Path):
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://example.com/tool.bin",
        output_path="downloads/tool.bin",
    )
    stdout = (
        '__DROWAI_HTTP_DOWNLOAD_META__{"http_code":200,'
        '"url_effective":"https://example.com/tool.bin",'
        '"size_download":24,'
        '"num_redirects":0,'
        '"time_total":0.135388}'
    )

    metadata = tool.parse_output(stdout, "", 0, args)

    assert metadata["status_code"] == 200
    assert metadata["effective_url"] == "https://example.com/tool.bin"
    assert metadata["redirect_count"] == 0
    assert metadata["timing_ms"] == 135


def test_run_success_sets_integrity_metadata_without_artifacts(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    content = b"downloaded-bytes"
    expected_sha = hashlib.sha256(content).hexdigest()
    stdout = _fixture_text("download", "file_saved_stdout.txt")

    def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
        _ = cwd
        output_idx = cmd.index("--output") + 1
        Path(cmd[output_idx]).write_bytes(content)
        return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    tool = HttpDownloadTool()
    result = tool.run(
        HttpDownloadArgs(
            target="https://downloads.example.com/tool.bin",
            output_path="files/tool.bin",
            expected_sha256=expected_sha,
        )
    )

    assert result.success is True
    assert result.metadata["saved_path"] == "files/tool.bin"
    assert result.metadata["bytes_written"] == len(content)
    assert result.metadata["checksum_verified"] is True
    assert result.metadata["sha256"] == expected_sha
    assert result.metadata["compact_summary"]
    assert result.metadata["compact_key_findings"]
    assert result.metadata["compact_decision_evidence"]
    assert result.artifacts == []


def test_postprocess_uses_existing_workspace_candidate_when_context_root_is_stale(workspace: Path, tmp_path: Path):
    content = b"download-content"
    expected_sha = hashlib.sha256(content).hexdigest()
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://downloads.example.com/tool.bin",
        output_path="files/tool.bin",
        expected_sha256=expected_sha,
    )
    tool.build_command(args)
    output_path = workspace / "files" / "tool.bin"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    stale_workspace = tmp_path / "stale-workspace"
    stale_workspace.mkdir()

    result = tool.postprocess_execution(
        args=args,
        stdout="",
        stderr="",
        exit_code=0,
        success=True,
        metadata={"status_code": 200, "effective_url": args.target},
        artifacts=[],
        runtime_context=SimpleNamespace(host_workspace_path=str(stale_workspace)),
    )

    assert result.success is True
    assert result.metadata["bytes_written"] == len(content)
    assert result.metadata["sha256"] == expected_sha
    assert result.metadata["checksum_verified"] is True


def test_postprocess_uses_runtime_output_metadata_for_runner_file(workspace: Path, tmp_path: Path):
    content = b"download-content"
    expected_sha = hashlib.sha256(content).hexdigest()
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://downloads.example.com/tool.bin",
        output_path="files/tool.bin",
        expected_sha256=expected_sha,
    )
    tool.build_command(args)
    stale_workspace = tmp_path / "stale-workspace"
    stale_workspace.mkdir()

    result = tool.postprocess_execution(
        args=args,
        stdout="",
        stderr="",
        exit_code=0,
        success=True,
        metadata={
            "status_code": 200,
            "effective_url": args.target,
            "runtime_output_files": [
                {
                    "relative_path": "files/tool.bin",
                    "exists": True,
                    "size_bytes": len(content),
                    "content_sha256": expected_sha,
                }
            ],
        },
        artifacts=[],
        runtime_context=SimpleNamespace(host_workspace_path=str(stale_workspace)),
    )

    assert result.success is True
    assert result.metadata["bytes_written"] == len(content)
    assert result.metadata["sha256"] == expected_sha
    assert result.metadata["checksum_verified"] is True


def test_postprocess_rejects_http_error_status_even_when_file_exists(workspace: Path):
    tool = HttpDownloadTool()
    args = HttpDownloadArgs(
        target="https://downloads.example.com/missing.bin",
        output_path="files/missing.bin",
    )
    tool.build_command(args)
    output_path = workspace / "files" / "missing.bin"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("not found", encoding="utf-8")

    result = tool.postprocess_execution(
        args=args,
        stdout="",
        stderr="",
        exit_code=0,
        success=True,
        metadata={"status_code": 404, "effective_url": args.target},
        artifacts=[],
    )

    assert result.success is False
    assert result.exit_code == 3
    assert "status_code=404" in result.stderr


def test_run_fails_on_checksum_mismatch(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    bad_content = b"not-the-expected-file"
    stdout = _fixture_text("download", "partial_download_stdout.txt")
    stderr = _fixture_text("download", "checksum_mismatch_stderr.txt")

    def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
        _ = cwd
        output_idx = cmd.index("--output") + 1
        Path(cmd[output_idx]).write_bytes(bad_content)
        return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), stderr.encode("utf-8"))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    tool = HttpDownloadTool()
    result = tool.run(
        HttpDownloadArgs(
            target="https://downloads.example.com/tool.bin",
            output_path="files/tool.bin",
            expected_sha256="0" * 64,
        )
    )

    assert result.success is False
    assert result.exit_code == 3
    assert "checksum mismatch" in result.stderr.lower()


def test_run_rejects_existing_path_without_overwrite(workspace: Path):
    path = workspace / "existing.bin"
    path.write_bytes(b"already-there")

    tool = HttpDownloadTool()
    result = tool.run(
        HttpDownloadArgs(
            target="https://example.com/existing.bin",
            output_path="existing.bin",
            overwrite=False,
        )
    )

    assert result.success is False
    assert result.exit_code == -1
    assert result.metadata.get("error_type") == "validation_error"


def test_invalid_url_returns_structured_validation_result():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {"target": "ftp://example.com/file.bin", "output_path": "file.bin"},
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.metadata.get("error_type") == "validation_error"
    assert "http or https" in result.stderr.lower()
