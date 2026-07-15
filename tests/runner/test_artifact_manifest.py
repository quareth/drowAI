"""Tests for runner-side artifact manifest scanning and path normalization."""

from __future__ import annotations

from pathlib import Path

from drowai_runner.artifact_manifest import scan_runner_artifacts_for_manifest


def test_scan_runner_artifacts_accepts_workspace_local_and_workspace_mount_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "task-91"
    artifact_dir = workspace / "artifacts" / "cmd-91"
    artifact_dir.mkdir(parents=True)
    stdout_path = artifact_dir / "stdout.txt"
    stdout_path.write_text("ok\n", encoding="utf-8")

    result = scan_runner_artifacts_for_manifest(
        workspace_path=workspace,
        artifacts=[
            "artifacts/cmd-91/stdout.txt",
            "/workspace/artifacts/cmd-91/stdout.txt",
        ],
    )

    assert len(result.manifest_items) == 2
    assert result.manifest_items[0].relative_path == "artifacts/cmd-91/stdout.txt"
    assert result.manifest_items[0].size_bytes == 3
    assert result.skipped_count == 0


def test_scan_runner_artifacts_rejects_out_of_workspace_and_missing_paths_with_bounded_warnings(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "task-91"
    workspace.mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")

    result = scan_runner_artifacts_for_manifest(
        workspace_path=workspace,
        artifacts=[
            "../outside.txt",
            str(tmp_path / "outside.txt"),
            "artifacts/missing.txt",
        ],
        max_warnings=1,
    )

    assert result.manifest_items == ()
    assert result.skipped_count == 3
    assert len(result.warnings) == 1
    assert result.warnings_truncated_count == 2


def test_scan_runner_artifacts_rejects_symlink_without_reading_target(tmp_path: Path) -> None:
    workspace = tmp_path / "task-91"
    artifact_dir = workspace / "artifacts"
    artifact_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("host-secret", encoding="utf-8")
    (artifact_dir / "leak.txt").symlink_to(outside)

    result = scan_runner_artifacts_for_manifest(
        workspace_path=workspace,
        artifacts=["artifacts/leak.txt"],
    )

    assert result.manifest_items == ()
    assert result.skipped_count == 1
    assert result.warnings[0].code == "RUNNER_WORKSPACE_ENTRY_UNSAFE"
