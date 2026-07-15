"""mTLS and trust-material coverage for HTTP request/download tools."""

from __future__ import annotations

import os
from pathlib import Path

from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs
from agent.tools.tool_registry import run_tool_by_name


def _request_stdout() -> str:
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "ok\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t2\t0\t0.01"
    )


def _download_stdout() -> str:
    return "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://example.com/file.bin\t2\t0\t0.01"


def _workspace_setup(tmp_path: Path):
    previous = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    cwd = Path.cwd()
    os.chdir(tmp_path)
    return previous, cwd


def _workspace_teardown(previous: str | None, cwd: Path):
    os.chdir(cwd)
    if previous is None:
        os.environ.pop("WORKSPACE", None)
    else:
        os.environ["WORKSPACE"] = previous


def test_http_request_maps_mtls_flags_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        (tmp_path / "certs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "certs" / "client.crt").write_text("crt", encoding="utf-8")
        (tmp_path / "certs" / "client.key").write_text("key", encoding="utf-8")
        (tmp_path / "certs" / "ca.pem").write_text("ca", encoding="utf-8")

        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com",
            client_cert_path="certs/client.crt",
            client_key_path="certs/client.key",
            client_key_passphrase="topsecret",
            ca_cert_path="certs/ca.pem",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_request_stdout(), stderr="", exit_code=0, args=args)

        assert "--cert" in cmd and "certs/client.crt" in cmd
        assert "--key" in cmd and "certs/client.key" in cmd
        assert "--pass" in cmd and "topsecret" in cmd
        assert "--cacert" in cmd and "certs/ca.pem" in cmd
        assert metadata["mtls_used"] is True
        assert metadata["ca_cert_used"] is True
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_maps_mtls_flags_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        (tmp_path / "certs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "certs" / "client.crt").write_text("crt", encoding="utf-8")
        (tmp_path / "certs" / "client.key").write_text("key", encoding="utf-8")
        (tmp_path / "certs" / "ca.pem").write_text("ca", encoding="utf-8")

        tool = HttpDownloadTool()
        args = HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="out/file.bin",
            client_cert_path="certs/client.crt",
            client_key_path="certs/client.key",
            client_key_passphrase="topsecret",
            ca_cert_path="certs/ca.pem",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_download_stdout(), stderr="", exit_code=0, args=args)

        assert "--cert" in cmd and "certs/client.crt" in cmd
        assert "--key" in cmd and "certs/client.key" in cmd
        assert "--pass" in cmd and "topsecret" in cmd
        assert "--cacert" in cmd and "certs/ca.pem" in cmd
        assert metadata["mtls_used"] is True
        assert metadata["ca_cert_used"] is True
    finally:
        _workspace_teardown(previous, cwd)


def test_mtls_schema_validation_requires_cert_for_key():
    request_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "client_key_path": "certs/client.key",
        },
    )
    download_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {
            "target": "https://example.com/file.bin",
            "output_path": "out/file.bin",
            "client_key_passphrase": "x",
        },
    )

    assert request_result.success is False
    assert "client_key_path requires client_cert_path" in request_result.stderr
    assert download_result.success is False
    assert "client_key_passphrase requires client_key_path" in download_result.stderr


def test_mtls_runtime_path_validation_rejects_missing_or_outside_workspace(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        req_missing = run_tool_by_name(
            "information_gathering.web_enumeration.http_request",
            {
                "target": "https://example.com",
                "client_cert_path": "certs/missing.crt",
            },
        )
        dl_outside = run_tool_by_name(
            "information_gathering.web_enumeration.http_download",
            {
                "target": "https://example.com/file.bin",
                "output_path": "out/file.bin",
                "client_cert_path": "../outside.crt",
            },
        )

        assert req_missing.success is False
        assert req_missing.exit_code == -1
        assert req_missing.metadata.get("error_type") == "validation_error"
        assert "client_cert_path must point to an existing file" in req_missing.stderr

        assert dl_outside.success is False
        assert dl_outside.exit_code == -1
        assert dl_outside.metadata.get("error_type") == "validation_error"
    finally:
        _workspace_teardown(previous, cwd)
