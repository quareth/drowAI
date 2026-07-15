"""Tests for shared runtime workspace artifact persistence."""

from __future__ import annotations

import json
from pathlib import Path

from agent.tool_runtime.workspace_artifacts import (
    save_and_index_tool_output_artifact,
    save_and_index_tool_output_artifact_with_index_writes,
    should_persist_workspace_artifact,
)


def test_workspace_artifacts_persist_and_index_output(tmp_path: Path) -> None:
    artifact_path = save_and_index_tool_output_artifact(
        workspace_path=str(tmp_path),
        stdout="open port 443\n",
        stderr="",
        selected_tool="shell.exec",
    )

    assert artifact_path
    assert artifact_path.startswith("artifacts/")
    assert (tmp_path / artifact_path).read_text(encoding="utf-8") == "open port 443\n"
    assert list((tmp_path / "index").glob("chunks_*.jsonl"))


def test_workspace_artifacts_returns_command_owned_index_bytes(tmp_path: Path) -> None:
    first = save_and_index_tool_output_artifact_with_index_writes(
        workspace_path=str(tmp_path),
        stdout="first output\n",
        stderr="",
        selected_tool="shell.exec",
    )
    second = save_and_index_tool_output_artifact_with_index_writes(
        workspace_path=str(tmp_path),
        stdout="second output\n",
        stderr="",
        selected_tool="shell.exec",
    )

    assert first.artifact_path
    assert second.artifact_path
    assert len(first.index_writes) == 1
    assert len(second.index_writes) == 1
    assert first.index_writes[0].path == second.index_writes[0].path
    assert b"first output" in first.index_writes[0].content
    assert b"first output" not in second.index_writes[0].content
    assert b"second output" in second.index_writes[0].content


def test_workspace_artifacts_keep_raw_file_but_mask_index_bytes(tmp_path: Path) -> None:
    raw_secret = "workspace-index-secret-token"
    result = save_and_index_tool_output_artifact_with_index_writes(
        workspace_path=str(tmp_path),
        stdout=f"Authorization: Bearer {raw_secret}\n",
        stderr="",
        selected_tool="shell.exec",
    )

    assert result.artifact_path
    assert (tmp_path / result.artifact_path).read_text(encoding="utf-8") == (
        f"Authorization: Bearer {raw_secret}\n"
    )
    assert result.index_writes
    serialized_index = b"".join(item.content for item in result.index_writes).decode("utf-8")
    assert raw_secret not in serialized_index
    assert "<DURABLE_SECRET_MASK:token>" in serialized_index


def test_workspace_artifacts_mask_tshark_protocol_auth_json_index(tmp_path: Path) -> None:
    raw_secret = "PocSecret-DurableMasking-Sentinel-9f4c2a"
    raw_line = json.dumps({"ftp.request.command_parameter": raw_secret})

    result = save_and_index_tool_output_artifact_with_index_writes(
        workspace_path=str(tmp_path),
        stdout=f"{raw_line}\n",
        stderr="",
        selected_tool="sniffing_spoofing.network_sniffers.tshark",
    )

    assert result.artifact_path
    assert (tmp_path / result.artifact_path).read_text(encoding="utf-8") == f"{raw_line}\n"
    assert result.index_writes

    serialized_index = b"".join(item.content for item in result.index_writes).decode("utf-8")
    assert raw_secret not in serialized_index
    assert "ftp.request.command_parameter" in serialized_index
    assert "<DURABLE_SECRET_MASK:secret>" in serialized_index

    manifests = list((tmp_path / "index").glob("chunks_*.jsonl"))
    assert len(manifests) == 1
    records = [
        json.loads(line)
        for line in manifests[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records
    serialized_records = json.dumps(records)
    assert raw_secret not in serialized_records
    assert all(raw_secret not in record["text"] for record in records)
    assert all(raw_secret not in record["digest"] for record in records)


def test_workspace_artifacts_skip_read_only_tools(tmp_path: Path) -> None:
    artifact_path = save_and_index_tool_output_artifact(
        workspace_path=str(tmp_path),
        stdout="artifact content",
        stderr="",
        selected_tool="filesystem.read_file",
    )

    assert artifact_path is None
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "index").exists()
    assert should_persist_workspace_artifact("filesystem.read_file") is False
    assert (
        should_persist_workspace_artifact("information_gathering.web_enumeration.http_download")
        is False
    )
    assert should_persist_workspace_artifact("shell.exec") is True
