"""Tests for reusable web discovery and response analysis helpers."""

from __future__ import annotations

from agent.tools.web_applications.web_discovery_analysis import (
    analyze_ffuf_web_discovery,
)
from agent.tools.web_applications.web_response_analysis import (
    analyze_http_download_response,
    analyze_http_request_response,
)


MAX_EXAMPLES_PER_GROUP = 5
MAX_INPUT_RANGES_PER_GROUP = 8


def test_ffuf_web_discovery_analysis_groups_numeric_ranges() -> None:
    results = [
        _ffuf_row("https://example.com/data/1", "1", 200, 100, 10, 2),
        _ffuf_row("https://example.com/data/2", "2", 200, 100, 10, 2),
        _ffuf_row("https://example.com/data/3", "3", 302, 20, 4, 1, redirect="/"),
        _ffuf_row("https://example.com/data/4", "4", 302, 20, 4, 1, redirect="/"),
    ]

    analysis = analyze_ffuf_web_discovery(
        results,
        source_tool="web_applications.web_crawlers.ffuf",
        max_examples_per_group=MAX_EXAMPLES_PER_GROUP,
        max_input_ranges_per_group=MAX_INPUT_RANGES_PER_GROUP,
        target_template="https://example.com/data/FUZZ",
    )

    assert analysis.source_tool == "web_applications.web_crawlers.ffuf"
    assert analysis.target_template == "https://example.com/data/FUZZ"
    assert len(analysis.records) == 4
    assert analysis.status_distribution == {"200": 2, "302": 2}
    assert analysis.groups[0].count == 2
    assert analysis.groups[0].status_code == 200
    assert analysis.groups[0].response_size == 100
    assert analysis.groups[0].input_ranges == ("1-2",)
    assert analysis.groups[0].examples == ("/data/1", "/data/2")
    assert analysis.groups[1].count == 2
    assert analysis.groups[1].status_code == 302
    assert analysis.groups[1].redirect == "/"
    assert analysis.groups[1].input_ranges == ("3-4",)


def test_ffuf_web_discovery_analysis_keeps_non_numeric_examples() -> None:
    analysis = analyze_ffuf_web_discovery(
        [
            _ffuf_row("https://example.com/admin", "admin", 200, 50, 5, 1),
            _ffuf_row("https://example.com/login", "login", 200, 50, 5, 1),
            _ffuf_row("https://example.com/config.php.bak", "config.php.bak", 200, 50, 5, 1),
        ],
        source_tool="web_applications.web_crawlers.ffuf",
        max_examples_per_group=MAX_EXAMPLES_PER_GROUP,
        max_input_ranges_per_group=MAX_INPUT_RANGES_PER_GROUP,
    )

    assert len(analysis.groups) == 1
    group = analysis.groups[0]
    assert group.input_ranges == ()
    assert group.input_examples == ("admin", "login", "config.php.bak")
    assert group.examples == ("/admin", "/login", "/config.php.bak")


def test_ffuf_web_discovery_analysis_handles_partial_fields() -> None:
    analysis = analyze_ffuf_web_discovery(
        [
            {"url": "https://example.com/partial", "status": 204},
            {"url": "https://example.com/partial-two", "status": 204},
        ],
        source_tool="web_applications.web_crawlers.ffuf",
        max_examples_per_group=MAX_EXAMPLES_PER_GROUP,
        max_input_ranges_per_group=MAX_INPUT_RANGES_PER_GROUP,
    )

    assert len(analysis.records) == 2
    assert analysis.groups == (
        analysis.groups[0],
    )
    assert analysis.groups[0].count == 2
    assert analysis.groups[0].status_code == 204
    assert analysis.groups[0].response_size is None
    assert analysis.groups[0].examples == ("/partial", "/partial-two")


def test_http_request_response_analysis_normalizes_current_metadata_facts() -> None:
    analysis = analyze_http_request_response(
        source_tool="information_gathering.web_enumeration.http_request",
        metadata={
            "request_method": "POST",
            "effective_url": "https://example.com/home",
            "status_code": "302",
            "content_type": "text/html",
            "content_length": "184",
            "redirect_count": "1",
            "body_truncated": True,
            "body_captured": True,
            "response_mode": "text",
        },
        parameters={"target": "https://example.com/login", "method": "GET"},
    )

    assert analysis.source_tool == "information_gathering.web_enumeration.http_request"
    assert analysis.method == "POST"
    assert analysis.url == "https://example.com/home"
    assert analysis.status_code == 302
    assert analysis.content_type == "text/html"
    assert analysis.content_length == 184
    assert analysis.redirect_count == 1
    assert analysis.body_truncated is True
    assert analysis.body_captured is True
    assert analysis.response_mode == "text"


def test_http_request_response_analysis_extracts_browser_inspection_facts() -> None:
    response_text = """HTTP/1.1 200 OK
Server: gunicorn
Content-Type: text/html; charset=utf-8

<!doctype html>
<html>
  <head>
    <title>Security Dashboard</title>
    <meta name="generator" content="Flask">
    <link rel="stylesheet" href="/static/css/bootstrap.min.css">
    <script src="https://cdn.example.test/chart.js"></script>
  </head>
  <body>
    <h1>Dashboard</h1>
    <a href="/capture">Capture</a>
    <a href="/download/1">Download</a>
    <a href="https://docs.example.test/help">Docs</a>
    <button onclick="location.href='/download/2'">Download latest</button>
    <button data-url="/reports/latest.csv">Report</button>
    <button onclick="doSomething('/not-a-link')">Ignore JS argument</button>
    <img src="/static/avatar.png">
    <form method="post" action="/login">
      <input type="hidden" name="csrf_token">
      <input type="text" name="username">
      <input type="password" name="password">
    </form>
  </body>
</html>
"""

    analysis = analyze_http_request_response(
        source_tool="information_gathering.web_enumeration.http_request",
        metadata={
            "request_method": "GET",
            "effective_url": "https://example.com/",
            "status_code": "200",
            "content_type": "text/html; charset=utf-8",
            "content_length": "1000",
            "body_captured": True,
            "response_headers": {
                "Server": "gunicorn",
                "Set-Cookie": "session=SECRET; HttpOnly; Secure; SameSite=Lax; Path=/",
            },
        },
        parameters={"target": "https://example.com/"},
        response_text=response_text,
        safe_url=lambda value: value.replace("https://docs.example.test", "https://<external>")
        if value
        else value,
    )

    assert analysis.title == "Security Dashboard"
    assert analysis.headings == ("Dashboard",)
    assert analysis.internal_links == (
        "/capture",
        "/download/1",
        "/download/2",
        "/reports/latest.csv",
    )
    assert analysis.external_links == ("https://<external>/help",)
    assert analysis.download_links == (
        "/download/1",
        "/download/2",
        "/reports/latest.csv",
    )
    assert analysis.script_srcs == ("https://cdn.example.test/chart.js",)
    assert analysis.stylesheet_refs == ("/static/css/bootstrap.min.css",)
    assert analysis.image_refs == ("/static/avatar.png",)
    assert analysis.forms[0].method == "POST"
    assert analysis.forms[0].action == "/login"
    assert analysis.forms[0].input_names == ("csrf_token", "username", "password")
    assert analysis.forms[0].input_types == ("hidden", "text", "password")
    assert analysis.cookies[0].name == "session"
    assert analysis.cookies[0].flags == ("Secure", "HttpOnly", "SameSite=Lax")
    assert analysis.cookies[0].path == "/"
    assert "server=gunicorn" in analysis.tech_hints
    assert "generator=Flask" in analysis.tech_hints
    assert "asset=Bootstrap" in analysis.tech_hints
    assert analysis.body_line_count is not None


def test_http_download_response_analysis_normalizes_download_metadata() -> None:
    analysis = analyze_http_download_response(
        source_tool="information_gathering.web_enumeration.http_download",
        metadata={
            "effective_url": "https://example.com/download.bin",
            "saved_path": "downloads/download.bin",
            "status_code": "200",
            "content_type": "application/octet-stream",
            "content_length": "512",
            "redirect_count": "2",
            "response_mode": "binary",
            "bytes_written": "512",
            "sha256": "a" * 64,
            "checksum_verified": True,
        },
        parameters={
            "target": "https://example.com/original.bin",
            "output_path": "downloads/fallback.bin",
        },
        safe_url=lambda value: value.replace("example.com", "<host>") if value else value,
        safe_workspace_path=lambda value: f"safe/{value}" if value else value,
    )

    assert analysis.source_tool == "information_gathering.web_enumeration.http_download"
    assert analysis.method == "GET"
    assert analysis.url == "https://<host>/download.bin"
    assert analysis.status_code == 200
    assert analysis.content_type == "application/octet-stream"
    assert analysis.content_length == 512
    assert analysis.redirect_count == 2
    assert analysis.body_truncated is False
    assert analysis.body_captured is False
    assert analysis.response_mode == "binary"
    assert analysis.saved_path == "safe/downloads/download.bin"
    assert analysis.bytes_written == 512
    assert analysis.sha256 == "a" * 64
    assert analysis.checksum_verified is True


def _ffuf_row(
    url: str,
    input_value: str,
    status: int,
    length: int,
    words: int,
    lines: int,
    *,
    redirect: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "url": url,
        "input": {"FUZZ": input_value},
        "status": status,
        "length": length,
        "words": words,
        "lines": lines,
    }
    if redirect:
        row["redirectlocation"] = redirect
    return row
