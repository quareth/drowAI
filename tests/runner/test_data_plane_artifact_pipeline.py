"""Runner-side data_plane artifact pipeline certification coverage.

Scope:
- Validates manifest scanning plus signed-upload execution for mixed text/binary files.
- Confirms uploader sends expected payload bytes and preserves per-item metadata.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drowai_runner.artifact_manifest import scan_runner_artifacts_for_manifest
from drowai_runner.artifact_uploader import RunnerArtifactUploader
from runtime_shared.runner_protocol import RunnerArtifactUploadRequestItem


def test_data_plane_runner_manifest_scan_and_upload_pipeline_handles_text_and_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "task-77"
    artifact_dir = workspace / "artifacts" / "cmd-42"
    artifact_dir.mkdir(parents=True)

    text_path = artifact_dir / "report.txt"
    text_path.write_text("service=nginx\nport=443\n", encoding="utf-8")

    binary_path = artifact_dir / "screenshot.png"
    binary_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    scan = scan_runner_artifacts_for_manifest(
        workspace_path=workspace,
        artifacts=[
            {"relative_path": "artifacts/cmd-42/report.txt", "artifact_kind": "tool_file"},
            {"relative_path": "/workspace/artifacts/cmd-42/screenshot.png", "artifact_kind": "tool_file"},
        ],
    )

    assert len(scan.manifest_items) == 2
    assert scan.skipped_count == 0

    by_client_id = {item.artifact_client_id: item for item in scan.manifest_items}

    uploads = [
        RunnerArtifactUploadRequestItem(
            artifact_id="11111111-1111-1111-1111-111111111111",
            artifact_client_id=client_id,
            object_key=f"data-plane-cert/tenant-1/task-77/{item.relative_path}",
            upload_url="https://object.example.test/upload",
            upload_method="PUT",
            upload_headers={"x-test-signed": "1"},
            size_bytes=item.size_bytes,
            content_sha256=item.content_sha256,
            content_type=item.content_type,
            is_text=item.is_text,
        )
        for client_id, item in by_client_id.items()
    ]

    sent_payloads: list[bytes] = []

    class _FakeResponse:
        status = 200

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    def _fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        del timeout
        sent_payloads.append(bytes(request.data or b""))
        return _FakeResponse()

    monkeypatch.setattr("drowai_runner.artifact_uploader.urllib_request.urlopen", _fake_urlopen)

    uploader = RunnerArtifactUploader(max_attempts=1)
    result = uploader.upload(
        uploads=uploads,
        files_by_client_id=scan.files_by_client_id,
    )

    assert result.failures == ()
    assert len(result.completed) == 2
    assert text_path.read_bytes() in sent_payloads
    assert binary_path.read_bytes() in sent_payloads
