"""Unit tests for compact envelope assembly helpers."""

from __future__ import annotations

from types import SimpleNamespace

from agent.graph.compression.deterministic.envelope import (
    derive_compact_errors,
    extract_artifact_refs,
    merge_decision_evidence,
)


def test_merge_decision_evidence_preserves_current_precedence() -> None:
    """Metadata evidence precedes filesystem locator and processor evidence."""
    raw_result = {
        "metadata": {
            "compact_decision_evidence": ["metadata proof"],
            "fs_search_text": {
                "matches": [
                    {
                        "path": "artifacts/service.txt",
                        "line": 7,
                        "snippet": "service=ssh",
                    }
                ]
            },
        }
    }

    assert merge_decision_evidence(
        raw_result=raw_result,
        processed_evidence=["processor proof", "metadata proof"],
        limit=5,
    ) == [
        "metadata proof",
        "artifacts/service.txt:7:service=ssh",
        "processor proof",
    ]


def test_extract_artifact_refs_sanitizes_unsafe_mapping_and_dedupes() -> None:
    """Artifact reference extraction blocks unsafe paths and keeps stable fields."""
    signed_url = (
        "https://objects.example.invalid/private/task-output.json"
        "?X-Amz-Signature=dummy-signature"
    )
    refs = extract_artifact_refs(
        artifact_path="/workspace/artifacts/primary.txt",
        raw_result={
            "artifacts": [
                "/workspace/artifacts/primary.txt",
                {
                    "artifact_id": "artifact-1",
                    "tool_call_id": "call-1",
                    "tool_name": "filesystem.read_file",
                    "artifact_kind": "object_store",
                    "label": "Read output",
                    "path": signed_url,
                    "relative_path": "artifacts/task-output.json",
                },
                {
                    "artifact_id": "artifact-2",
                    "artifact_kind": "object_store",
                    "path": "tenant-a/task-123/private/task-output.json",
                    "relative_path": "tenant-a/task-123/private/task-output.json",
                },
                {
                    "artifact_id": "artifact-duplicate",
                    "artifact_kind": "object_store",
                    "path": signed_url,
                    "relative_path": "artifacts/task-output.json",
                },
            ]
        },
        execution_id="exec-123",
    )

    assert [ref.to_dict() for ref in refs] == [
        {
            "path": "/workspace/artifacts/primary.txt",
            "artifact_id": None,
            "execution_id": "exec-123",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": None,
            "label": None,
            "relative_path": None,
        },
        {
            "path": "artifacts/task-output.json",
            "artifact_id": "artifact-1",
            "execution_id": "exec-123",
            "tool_call_id": "call-1",
            "tool_name": "filesystem.read_file",
            "artifact_kind": "object_store",
            "label": "Read output",
            "relative_path": "artifacts/task-output.json",
        },
        {
            "path": "artifact://artifact-2",
            "artifact_id": "artifact-2",
            "execution_id": "exec-123",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": "object_store",
            "label": None,
            "relative_path": None,
        },
    ]


def test_derive_compact_errors_prefers_non_traceback_failure_cause() -> None:
    """Compact error projection skips traceback scaffolding when possible."""
    processed = SimpleNamespace(
        summary="Traceback (most recent call last):",
        key_findings=["File \"/app/main.py\", line 1", "extension vector is not available"],
    )

    assert derive_compact_errors(
        processed=processed,
        summary="fallback summary",
        success=False,
    ) == ["extension vector is not available"]
    assert derive_compact_errors(
        processed=processed,
        summary="fallback summary",
        success=True,
    ) == []
