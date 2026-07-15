"""Focused tests for Data Plane object-key construction and path sanitization."""

from __future__ import annotations

import pytest

from backend.services.data_plane.object_key_builder import (
    build_artifact_object_key,
    build_evidence_object_key,
    sanitize_object_filename,
)


def test_build_artifact_object_key_includes_scope_and_filename() -> None:
    key = build_artifact_object_key(
        tenant_id="tenant-7",
        task_id="task-11",
        execution_id="exec-2",
        artifact_id="artifact-4",
        filename="/workspace/artifacts/report.json",
    )
    assert key == (
        "tenants/tenant-7/tasks/task-11/executions/exec-2/"
        "artifacts/artifact-4/report.json"
    )


def test_build_evidence_object_key_includes_scope_and_filename() -> None:
    key = build_evidence_object_key(
        tenant_id="tenant-7",
        engagement_id="eng-3",
        evidence_id="evidence-8",
        filename="evidence/screenshot.png",
    )
    assert key == "tenants/tenant-7/engagements/eng-3/evidence/evidence-8/screenshot.png"


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "tenant_id": "",
            "task_id": "task-1",
            "execution_id": "exec-1",
            "artifact_id": "artifact-1",
            "filename": "a.txt",
        },
        {
            "tenant_id": "tenant-1",
            "task_id": " ",
            "execution_id": "exec-1",
            "artifact_id": "artifact-1",
            "filename": "a.txt",
        },
    ],
)
def test_artifact_key_builder_rejects_missing_tenant_or_task(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_artifact_object_key(**kwargs)


@pytest.mark.parametrize(
    ("candidate", "expected_filename"),
    [
        ("/etc/passwd", "passwd"),
        ("/workspace/../secret", "secret"),
        ("artifacts/../../x", "x"),
        ("artifacts/scan\x00.txt", "scan.txt"),
        ("artifacts//nested///final.log", "final.log"),
    ],
)
def test_sanitize_object_filename_strips_hostile_path_parts(
    candidate: str,
    expected_filename: str,
) -> None:
    assert sanitize_object_filename(candidate) == expected_filename


@pytest.mark.parametrize("candidate", ["", "   ", "/", "///", "../..", "/workspace/../"])
def test_sanitize_object_filename_rejects_empty_names(candidate: str) -> None:
    with pytest.raises(ValueError):
        sanitize_object_filename(candidate)
