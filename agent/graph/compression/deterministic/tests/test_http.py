"""Unit tests for HTTP deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.http import (
    HTTP_DOWNLOAD_TOOL_ID,
    HTTP_REQUEST_TOOL_ID,
    http_adapter,
    registered_http_tool_ids,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)


def test_http_adapter_registers_visible_http_tool_ids() -> None:
    """Visible HTTP tools resolve to the deterministic HTTP adapter."""

    assert registered_http_tool_ids() == (HTTP_REQUEST_TOOL_ID, HTTP_DOWNLOAD_TOOL_ID)
    assert get_adapter(HTTP_REQUEST_TOOL_ID) is http_adapter
    assert get_adapter(HTTP_DOWNLOAD_TOOL_ID) is http_adapter


def test_http_request_summary_and_security_findings_use_redacted_metadata() -> None:
    """HTTP request metadata yields bounded status/header facts without secrets."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HTTP_REQUEST_TOOL_ID,
            raw_result={
                "success": True,
                "parameters": {
                    "target": "https://user:pass@example.com/login?token=RAW_TOKEN",
                    "method": "POST",
                    "headers": {"Authorization": "Bearer RAW_SECRET"},
                },
                "artifacts": [
                    "artifacts/http_request_123.html",
                    "artifacts/http_request_123_headers.txt",
                ],
                "metadata": {
                    "status_code": 302,
                    "effective_url": "https://<REDACTED>@example.com/home",
                    "request_method": "POST",
                    "content_type": "text/html",
                    "content_length": 184,
                    "redirect_count": 1,
                    "body_truncated": True,
                    "body_captured": True,
                    "auth_mode_used": "bearer",
                    "cookies_persisted": True,
                    "request_headers": {"Authorization": "<REDACTED>"},
                    "response_headers": {
                        "Location": "https://example.com/home",
                        "Strict-Transport-Security": "max-age=31536000",
                        "Content-Security-Policy": "default-src 'self'",
                        "Set-Cookie": "<REDACTED>",
                    },
                },
            },
        )
    )

    rendered = " ".join(
        [
            result.summary or "",
            *result.key_findings,
            *result.decision_evidence,
            str(tuple(result.structured_signals)),
        ]
    )

    assert result.summary == (
        "HTTP POST https://<REDACTED>@example.com/home; status 302; "
        "content_type text/html; bytes 184; redirects 1"
    )
    assert "redirects: 1 to https://example.com/home" in result.key_findings
    assert "auth mode used: bearer" in result.key_findings
    assert "request header present: authorization" in result.key_findings
    assert "response header set-cookie: present" in result.key_findings
    assert "response body truncated; full body may be available as artifact" in result.key_findings
    assert result.structured_signals[0]["artifacts"] == [
        {"path": "artifacts/http_request_123.html"},
        {"path": "artifacts/http_request_123_headers.txt"},
    ]
    assert "RAW_SECRET" not in rendered
    assert "RAW_TOKEN" not in rendered
    assert "user:pass" not in rendered


def test_http_download_preserves_sanitized_download_artifact_refs() -> None:
    """HTTP download metadata preserves downloaded-file refs and sanitizes object URLs."""

    signed_url = (
        "https://objects.example.invalid/private/download.bin"
        "?X-Amz-Signature=RAW_SIGNATURE"
    )
    result = compress_deterministically(
        CompressionInput(
            tool_name=HTTP_DOWNLOAD_TOOL_ID,
            raw_result={
                "success": True,
                "parameters": {
                    "target": "https://user:pass@downloads.example.com/tool.bin",
                    "output_path": "downloads/tool.bin",
                },
                "artifacts": [
                    {
                        "artifact_id": "artifact-1",
                        "artifact_kind": "object_store",
                        "label": "Downloaded body",
                        "path": signed_url,
                        "relative_path": "downloads/tool.bin",
                    }
                ],
                "metadata": {
                    "status_code": 200,
                    "effective_url": "https://<REDACTED>@downloads.example.com/tool.bin",
                    "saved_path": "downloads/tool.bin",
                    "bytes_written": 512,
                    "sha256": "a" * 64,
                    "checksum_verified": True,
                    "runtime_output_files": [
                        {
                            "relative_path": "downloads/tool.bin",
                            "size_bytes": 512,
                            "content_sha256": "a" * 64,
                        }
                    ],
                },
            },
        )
    )

    rendered = " ".join(
        [
            result.summary or "",
            *result.key_findings,
            *result.decision_evidence,
            str(tuple(result.structured_signals)),
        ]
    )

    assert result.summary == (
        "HTTP GET download https://<REDACTED>@downloads.example.com/tool.bin; "
        "status 200; bytes 512; saved downloads/tool.bin"
    )
    assert "download saved: downloads/tool.bin" in result.key_findings
    assert "bytes_written: 512" in result.key_findings
    assert "checksum verified" in result.key_findings
    assert result.structured_signals[0]["artifacts"] == [
        {
            "path": "downloads/tool.bin",
            "artifact_id": "artifact-1",
            "artifact_kind": "object_store",
            "label": "Downloaded body",
            "relative_path": "downloads/tool.bin",
        }
    ]
    assert signed_url not in rendered
    assert "RAW_SIGNATURE" not in rendered
    assert "user:pass" not in rendered


def test_http_request_empty_success_remains_explicit_bounded_and_sanitized() -> None:
    """Empty successful HTTP responses still produce explicit bounded facts."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HTTP_REQUEST_TOOL_ID,
            raw_result={
                "success": True,
                "stdout": "RAW_EMPTY_BODY_SECRET_SHOULD_NOT_APPEAR",
                "parameters": {
                    "target": "https://user:pass@example.com/empty?token=RAW_TOKEN",
                    "method": "GET",
                    "headers": {"Authorization": "Bearer RAW_BEARER_SHOULD_NOT_APPEAR"},
                },
                "metadata": {
                    "status_code": 204,
                    "effective_url": "https://user:pass@example.com/empty?token=RAW_TOKEN",
                    "request_method": "GET",
                    "content_length": 0,
                    "body_captured": False,
                    "request_headers": {"Authorization": "<REDACTED>"},
                    "response_headers": {
                        "X-Content-Type-Options": "nosniff",
                    },
                },
            },
        )
    )

    rendered = " ".join(
        [
            result.summary or "",
            *result.key_findings,
            *result.decision_evidence,
            str(tuple(result.structured_signals)),
        ]
    )

    assert result.summary == (
        "HTTP GET https://<REDACTED>@example.com/empty?token=%3CREDACTED%3E; "
        "status 204; bytes 0"
    )
    assert "request header present: authorization" in result.key_findings
    assert "response header x-content-type-options: nosniff" in result.key_findings
    assert result.decision_evidence == (
        "request header present: authorization",
        "response header x-content-type-options: nosniff",
    )
    assert result.structured_signals == (
        {
            "kind": "http_request",
            "tool": HTTP_REQUEST_TOOL_ID,
            "method": "GET",
            "url": "https://<REDACTED>@example.com/empty?token=%3CREDACTED%3E",
            "status_code": 204,
            "content_length": 0,
            "redirect_count": 0,
            "body_truncated": False,
            "body_captured": False,
        },
    )
    assert len(result.summary or "") <= 240
    assert all(len(item) <= 240 for item in result.key_findings)
    assert all(len(item) <= 240 for item in result.decision_evidence)
    assert "RAW_EMPTY_BODY_SECRET_SHOULD_NOT_APPEAR" not in rendered
    assert "RAW_BEARER_SHOULD_NOT_APPEAR" not in rendered
    assert "RAW_TOKEN" not in rendered
    assert "user:pass" not in rendered


def test_http_request_extracts_page_links_assets_forms_and_cookies() -> None:
    """HTTP request compression exposes browser-inspection facts instead of raw body dumps."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HTTP_REQUEST_TOOL_ID,
            raw_result={
                "success": True,
                "stdout": """HTTP/1.1 200 OK
Server: gunicorn
Content-Type: text/html; charset=utf-8

<!doctype html>
<html>
  <head>
    <title>Security Dashboard</title>
    <link rel="stylesheet" href="/static/css/bootstrap.min.css">
    <script src="https://cdn.example.test/chart.js"></script>
  </head>
  <body>
    <h1>Dashboard</h1>
    <p>RAW_SECRET_BODY_LINE_SHOULD_NOT_BE_PROMOTED</p>
    <a href="/capture">Capture</a>
    <a href="/download/1">Download</a>
    <a href="https://docs.example.test/help">Docs</a>
    <button onclick="location.href='/download/2'">Download latest</button>
    <button data-url="/reports/latest.csv">Report</button>
    <form method="post" action="/login">
      <input type="hidden" name="csrf_token">
      <input type="text" name="username">
      <input type="password" name="password">
    </form>
  </body>
</html>
""",
                "parameters": {"target": "https://example.com/", "method": "GET"},
                "metadata": {
                    "status_code": 200,
                    "effective_url": "https://example.com/",
                    "request_method": "GET",
                    "content_type": "text/html; charset=utf-8",
                    "content_length": 900,
                    "body_captured": True,
                    "response_headers": {
                        "Server": "gunicorn",
                        "Set-Cookie": "session=SECRET; HttpOnly; Secure; SameSite=Lax; Path=/",
                    },
                },
            },
        )
    )

    rendered = " ".join(
        [
            result.summary or "",
            *result.key_findings,
            *result.decision_evidence,
            str(tuple(result.structured_signals)),
        ]
    )

    assert 'page title: "Security Dashboard"' in result.key_findings
    assert "headings: Dashboard" in result.key_findings
    assert (
        "internal links: /capture, /download/1, /download/2, /reports/latest.csv"
        in result.key_findings
    )
    assert (
        "download links: /download/1, /download/2, /reports/latest.csv"
        in result.key_findings
    )
    assert "assets: total=2 scripts=1 styles=1 images=0 examples=/static/css/bootstrap.min.css,https://cdn.example.test/chart.js" in result.key_findings
    assert "form: method=POST action=/login fields=csrf_token,username,password types=hidden,text,password" in result.key_findings
    assert "cookie set: session flags=Secure,HttpOnly,SameSite=Lax path=/" in result.key_findings
    assert "tech hints: server=gunicorn, asset=Bootstrap, asset=Chart" in result.key_findings
    assert result.structured_signals[0]["title"] == "Security Dashboard"
    assert result.structured_signals[0]["internal_link_count"] == 4
    assert result.structured_signals[0]["download_links"] == [
        "/download/1",
        "/download/2",
        "/reports/latest.csv",
    ]
    assert result.structured_signals[0]["cookie_names"] == ["session"]
    assert "RAW_SECRET_BODY_LINE_SHOULD_NOT_BE_PROMOTED" not in rendered


def test_http_timeout_error_remains_bounded_and_omits_request_secrets() -> None:
    """Error/timeout compression does not promote raw stdout, stderr, or request secrets."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HTTP_REQUEST_TOOL_ID,
            raw_result={
                "success": False,
                "status": "timeout",
                "stdout": "RAW_BODY_SECRET_SHOULD_NOT_APPEAR",
                "stderr": "Authorization: Bearer RAW_BEARER_SHOULD_NOT_APPEAR",
                "parameters": {
                    "target": "https://user:pass@example.com/api?token=RAW_TOKEN",
                    "method": "GET",
                    "headers": {"Authorization": "Bearer RAW_BEARER_SHOULD_NOT_APPEAR"},
                },
                "metadata": {
                    "curl_exit_code": -2,
                    "effective_url": "https://user:pass@example.com/api?token=RAW_TOKEN",
                    "request_method": "GET",
                },
            },
        )
    )

    rendered = " ".join(
        [
            result.summary or "",
            *result.errors,
            *result.decision_evidence,
            str(tuple(result.structured_signals)),
        ]
    )

    assert result.summary == (
        "HTTP GET https://<REDACTED>@example.com/api?token=%3CREDACTED%3E failed: timeout"
    )
    assert result.errors == ("timeout",)
    assert "RAW_BODY_SECRET_SHOULD_NOT_APPEAR" not in rendered
    assert "RAW_BEARER_SHOULD_NOT_APPEAR" not in rendered
    assert "RAW_TOKEN" not in rendered
    assert "user:pass" not in rendered
