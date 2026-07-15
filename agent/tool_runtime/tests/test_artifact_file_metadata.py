"""Tests for bounded artifact file metadata used by planner prompts."""

from __future__ import annotations

from agent.tool_runtime.artifact_file_metadata import (
    build_artifact_file_metadata_for_prompt,
    collect_artifact_file_ref_candidates,
)


def test_build_artifact_file_metadata_reads_relative_file_stats(tmp_path) -> None:
    artifact = tmp_path / "artifacts" / "scan.xml"
    artifact.parent.mkdir()
    artifact.write_text("one\ntwo\nthree\n", encoding="utf-8")

    entries = build_artifact_file_metadata_for_prompt(
        selected_tools=["filesystem.read_file"],
        workspace_path=str(tmp_path),
        artifact_refs=[{"path": "artifacts/scan.xml"}],
    )

    assert entries == [
        {
            "path": "artifacts/scan.xml",
            "status": "ready",
            "size_bytes": artifact.stat().st_size,
            "line_count": 3,
        }
    ]


def test_build_artifact_file_metadata_maps_workspace_absolute_path(tmp_path) -> None:
    artifact = tmp_path / "artifacts" / "nmap.xml"
    artifact.parent.mkdir()
    artifact.write_text("<host />\n", encoding="utf-8")

    entries = build_artifact_file_metadata_for_prompt(
        selected_tools=["filesystem.search_text"],
        workspace_path=str(tmp_path),
        artifact_refs=[{"path": "/workspace/artifacts/nmap.xml"}],
    )

    assert entries[0]["status"] == "ready"
    assert entries[0]["path"] == "/workspace/artifacts/nmap.xml"
    assert entries[0]["size_bytes"] == artifact.stat().st_size
    assert entries[0]["line_count"] == 1


def test_build_artifact_file_metadata_marks_missing_file_unavailable(tmp_path) -> None:
    entries = build_artifact_file_metadata_for_prompt(
        selected_tools=["filesystem.read_file"],
        workspace_path=str(tmp_path),
        artifact_refs=[{"path": "artifacts/missing.xml"}],
    )

    assert entries == [
        {
            "path": "artifacts/missing.xml",
            "status": "unavailable",
            "reason": "file does not exist",
        }
    ]


def test_build_artifact_file_metadata_rejects_workspace_escape(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    entries = build_artifact_file_metadata_for_prompt(
        selected_tools=["filesystem.read_file"],
        workspace_path=str(tmp_path),
        artifact_refs=[{"path": "../outside.txt"}],
    )

    assert entries == [
        {
            "path": "../outside.txt",
            "status": "unavailable",
            "reason": "path resolves outside workspace",
        }
    ]


def test_build_artifact_file_metadata_omits_when_filesystem_tools_not_selected(tmp_path) -> None:
    artifact = tmp_path / "artifacts" / "scan.xml"
    artifact.parent.mkdir()
    artifact.write_text("<host />\n", encoding="utf-8")

    entries = build_artifact_file_metadata_for_prompt(
        selected_tools=["shell.exec"],
        workspace_path=str(tmp_path),
        artifact_refs=[{"path": "artifacts/scan.xml"}],
    )

    assert entries == []


def test_collect_artifact_file_ref_candidates_from_current_metadata() -> None:
    refs = collect_artifact_file_ref_candidates(
        {
            "last_artifact_path": "artifacts/raw.txt",
            "last_tool_result_compact": {
                "artifact_refs": [
                    {"path": "/workspace/artifacts/scan.xml", "label": "stdout"}
                ]
            },
            "tool_execution_records": [
                {"artifact_refs": [{"path": "artifacts/ffuf.json"}]}
            ],
        }
    )

    assert refs == [
        {"path": "artifacts/raw.txt"},
        {"path": "/workspace/artifacts/scan.xml", "label": "stdout"},
        {"path": "artifacts/ffuf.json"},
    ]
