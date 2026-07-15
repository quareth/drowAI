"""Transport and metadata contract checks for HTTP request/download tools."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from agent.executor import EnhancedCommandExecutor
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


def _make_executor(workspace_path: str) -> EnhancedCommandExecutor:
    config = MagicMock()
    config.task_id = 1
    config.workspace_path = workspace_path
    config.openai_api_key = "test-key"
    config.model_name = "gpt-4"
    config.individual_tool_timeout = 60
    config.tool_execution_timeout = 60
    return EnhancedCommandExecutor(config=config)


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


def test_http_args_accept_container_transport_values():
    for transport in (None, "file-comm", "pty"):
        request_args = HttpRequestArgs(target="https://example.com", transport=transport)
        download_args = HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="file.bin",
            transport=transport,
        )
        assert request_args.transport == transport
        assert download_args.transport == transport


def test_http_args_reject_direct_transport():
    with pytest.raises(ValidationError):
        HttpRequestArgs(target="https://example.com", transport="direct")
    with pytest.raises(ValidationError):
        HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="file.bin",
            transport="direct",
        )


def test_http_tools_publish_supported_transports_metadata():
    request_meta = get_enhanced_tool_metadata("information_gathering.web_enumeration.http_request")
    download_meta = get_enhanced_tool_metadata("information_gathering.web_enumeration.http_download")

    assert request_meta is not None
    assert download_meta is not None
    assert request_meta.supported_transports == ["file-comm", "pty"]
    assert download_meta.supported_transports == ["file-comm", "pty"]


def test_executor_pty_selection_honors_transport_opt_out(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    executor = _make_executor(str(workspace))
    monkeypatch.setattr(executor, "_is_pty_enabled", lambda: True)

    assert executor._should_use_pty(
        "information_gathering.web_enumeration.http_request",
        {"target": "https://example.com", "transport": "file-comm"},
    ) is False
    assert executor._should_use_pty(
        "information_gathering.web_enumeration.http_download",
        {"target": "https://example.com/file.bin", "output_path": "out.bin", "transport": "file-comm"},
    ) is False
    assert executor._should_use_pty(
        "information_gathering.web_enumeration.http_request",
        {"target": "https://example.com"},
    ) is True
