"""Unit tests for filesystem deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.filesystem import (
    _FILESYSTEM_TOOL_IDS,
    _extract_locator_evidence_from_metadata,
    filesystem_adapter,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)


def test_extract_locator_evidence_formats_filesystem_search_matches() -> None:
    """Search metadata remains path:line:snippet evidence in first-seen order."""
    raw_result = {
        "metadata": {
            "fs_search_text": {
                "matches": [
                    {
                        "path": " artifacts/nmap.xml ",
                        "line": "13",
                        "snippet": " <service name=\"http\"/> ",
                    },
                    {
                        "path": "",
                        "line": 14,
                        "snippet": "missing path is skipped",
                    },
                    {
                        "path": "artifacts/nmap.xml",
                        "line": "not-an-int",
                        "snippet": "missing line is skipped",
                    },
                    {
                        "path": "artifacts/nmap.xml",
                        "line": 15,
                        "snippet": "",
                    },
                ]
            }
        }
    }

    assert _extract_locator_evidence_from_metadata(raw_result) == [
        "artifacts/nmap.xml:13:<service name=\"http\"/>"
    ]


def test_extract_locator_evidence_keeps_read_file_lines_exact_with_current_limit() -> None:
    """Read-file line evidence keeps current string format and evidence cap."""
    raw_result = {
        "metadata": {
            "fs_read": {
                "line_evidence": [
                    "6:<scaninfo services=\"443\"/>",
                    "10:<address addr=\"127.0.0.1\"/>",
                    "11:<hostnames/>",
                ]
            }
        }
    }

    assert _extract_locator_evidence_from_metadata(raw_result, limit=2) == [
        "6:<scaninfo services=\"443\"/>",
        "10:<address addr=\"127.0.0.1\"/>",
    ]


def test_extract_locator_evidence_dedupes_and_preserves_metadata_order() -> None:
    """Tool-authored search evidence stays before read evidence and is de-duped."""
    raw_result = {
        "metadata": {
            "fs_search_text": {
                "matches": [
                    {
                        "path": "artifacts/result.txt",
                        "line": 7,
                        "snippet": "service=ssh",
                    },
                    {
                        "path": "artifacts/result.txt",
                        "line": 7,
                        "snippet": "service=ssh",
                    },
                ]
            },
            "fs_read": {
                "line_evidence": [
                    "8:service=http",
                    "8:service=http",
                ]
            },
        }
    }

    assert _extract_locator_evidence_from_metadata(raw_result) == [
        "artifacts/result.txt:7:service=ssh",
        "8:service=http",
    ]


def test_extract_locator_evidence_ignores_generic_colon_prefixed_stdout() -> None:
    """Non-filesystem raw output is not promoted without structured metadata."""
    raw_result = {
        "stdout": "10:not a filesystem evidence locator",
        "metadata": {"tool_name": "shell.exec"},
    }

    assert _extract_locator_evidence_from_metadata(raw_result) == []


def test_filesystem_adapter_registers_requested_exact_tool_ids() -> None:
    """Phase 4.2 filesystem coverage is registered for the requested tools."""

    for tool_id in _FILESYSTEM_TOOL_IDS:
        assert get_adapter(tool_id) is filesystem_adapter


def test_filesystem_read_adapter_preserves_path_and_line_evidence_without_content() -> None:
    """Read adapters expose exact locators without copying raw file contents."""
    result = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.read_file",
            raw_result={
                "stdout": "SECRET_FILE_CONTENT_SHOULD_NOT_BE_PROMOTED",
                "parameters": {"path": "artifacts/app.log"},
                "metadata": {
                    "fs_read": {
                        "bytes_read": 128,
                        "lines_read": 2,
                        "read_mode_used": "grep",
                        "line_evidence": [
                            "12| ERROR login failed",
                            "12| ERROR login failed",
                            "18| WARN retry",
                        ],
                    }
                },
            },
        )
    )

    assert result.completeness == "partial"
    assert result.summary == "Read artifacts/app.log with grep mode (2 lines, 128 bytes)"
    assert result.decision_evidence == (
        "artifacts/app.log:12| ERROR login failed",
        "artifacts/app.log:18| WARN retry",
    )
    assert "SECRET_FILE_CONTENT" not in result.summary
    assert all("SECRET_FILE_CONTENT" not in item for item in result.key_findings)


def test_filesystem_search_adapter_preserves_path_line_column_evidence() -> None:
    """Search adapters keep exact path and line locator evidence from metadata."""
    result = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.search_text",
            raw_result={
                "parameters": {"path": "src", "query": "token"},
                "metadata": {
                    "fs_search_text": {
                        "matches": [
                            {
                                "path": "src/config.py",
                                "line": 7,
                                "column": 12,
                                "snippet": "api_token = load_token()",
                            }
                        ],
                        "truncated": False,
                    }
                },
            },
        )
    )

    assert result.summary == "Searched text under src for 'token'; 1 matches found"
    assert result.decision_evidence == (
        "src/config.py:7:12:api_token = load_token()",
    )
    assert result.key_findings == ("match: src/config.py",)
    assert result.lossiness_risk == "low"


def test_filesystem_mutation_adapter_bounds_paths_and_does_not_promote_content() -> None:
    """Mutation adapters summarize operation metadata without exposing content."""
    result = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.write_file",
            raw_result={
                "parameters": {
                    "path": "reports/summary.md",
                    "content": "RAW_SECRET_CONTENT_SHOULD_NOT_APPEAR",
                },
                "metadata": {
                    "fs_write": {
                        "path": "reports/summary.md",
                        "action": "updated",
                        "bytes_changed": 42,
                        "message": "Wrote 42 bytes to reports/summary.md",
                    }
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
    assert result.summary == "write_file updated reports/summary.md (42 bytes changed)"
    assert result.key_findings == (
        "operation: write_file",
        "action: updated",
        "affected_paths: reports/summary.md",
        "bytes_changed: 42",
    )
    assert result.decision_evidence == (
        "write_file: Wrote 42 bytes to reports/summary.md",
    )
    assert "RAW_SECRET_CONTENT" not in rendered


def test_filesystem_copy_adapter_uses_source_and_destination_paths() -> None:
    """Transfer mutations preserve bounded source/destination path summaries."""
    result = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.copy_path",
            raw_result={
                "parameters": {"src": "templates/base.txt", "dest": "out/base.txt"},
                "metadata": {
                    "fs_copy": {
                        "path": "out/base.txt",
                        "action": "copied",
                        "message": "Copied templates/base.txt -> out/base.txt",
                        "extra": {
                            "source": "templates/base.txt",
                            "destination": "out/base.txt",
                        },
                    }
                },
            },
        )
    )

    assert result.summary == "copy_path copied out/base.txt, templates/base.txt"
    assert "affected_paths: out/base.txt, templates/base.txt" in result.key_findings
    assert result.structured_signals[0]["affected_paths"] == [
        "out/base.txt",
        "templates/base.txt",
    ]


def test_filesystem_empty_error_and_timeout_cases_stay_compact() -> None:
    """Empty, error, and timeout cases do not invent file contents."""
    empty = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.search_text",
            raw_result={
                "parameters": {"path": "src", "query": "missing"},
                "metadata": {"fs_search_text": {"matches": [], "truncated": False}},
            },
        )
    )
    error = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.read_tail",
            raw_result={
                "stdout": "RAW_OUTPUT_SHOULD_NOT_APPEAR",
                "success": False,
                "status": "error",
                "parameters": {"path": "missing.log"},
                "metadata": {"error": "not_found"},
            },
        )
    )
    timeout = compress_deterministically(
        CompressionInput(
            tool_name="filesystem.delete_path",
            raw_result={
                "stdout": "RAW_DELETE_OUTPUT_SHOULD_NOT_APPEAR",
                "success": False,
                "status": "timeout",
                "parameters": {"path": "slow/path"},
                "metadata": {},
            },
        )
    )

    assert empty.summary == "Searched text under src for 'missing'; 0 matches found"
    assert empty.decision_evidence == ()
    assert error.summary == "read_tail failed for missing.log: not_found"
    assert error.errors == ("not_found",)
    assert timeout.summary == "delete_path failed for slow/path: timeout"
    assert timeout.errors == ("timeout",)
    combined = " ".join(
        item
        for result in (empty, error, timeout)
        for item in (result.summary or "", *result.errors, *result.decision_evidence)
    )
    assert "RAW_OUTPUT_SHOULD_NOT_APPEAR" not in combined
    assert "RAW_DELETE_OUTPUT_SHOULD_NOT_APPEAR" not in combined
