"""Tests for runner signed upload execution and retry/idempotency behavior."""

from __future__ import annotations

from pathlib import Path
from urllib import error as urllib_error

import pytest

from drowai_runner.artifact_manifest import ScannedArtifactFile
from drowai_runner.artifact_uploader import RunnerArtifactUploader
from runtime_shared.runner_protocol import RunnerArtifactUploadRequestItem


def _build_upload_item(*, artifact_client_id: str, object_key: str, sha256: str) -> RunnerArtifactUploadRequestItem:
    return RunnerArtifactUploadRequestItem(
        artifact_id="11111111-1111-1111-1111-111111111111",
        artifact_client_id=artifact_client_id,
        object_key=object_key,
        upload_url="https://object.example.test/upload",
        upload_method="PUT",
        upload_headers={"x-test-signed": "1"},
        size_bytes=3,
        content_sha256=sha256,
        content_type="text/plain",
        is_text=True,
    )


def test_runner_artifact_uploader_retries_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "stdout.txt"
    artifact_path.write_text("ok\n", encoding="utf-8")
    sha = "dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22"
    scanned = ScannedArtifactFile(
        artifact_client_id="artifact-1",
        relative_path="stdout.txt",
        absolute_path=artifact_path,
        workspace_root=tmp_path,
        size_bytes=3,
        content_sha256=sha,
        content_type="text/plain",
        is_text=True,
    )
    attempts = {"count": 0}

    class _FakeResponse:
        status = 200

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    def _fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise urllib_error.URLError("temporary network issue")
        return _FakeResponse()

    monkeypatch.setattr("drowai_runner.artifact_uploader.urllib_request.urlopen", _fake_urlopen)
    uploader = RunnerArtifactUploader(max_attempts=2, sleep_fn=lambda _seconds: None)
    item = _build_upload_item(artifact_client_id="artifact-1", object_key="tenant/task/one", sha256=sha)
    result = uploader.upload(uploads=[item], files_by_client_id={"artifact-1": scanned})

    assert attempts["count"] == 2
    assert len(result.completed) == 1
    assert result.failures == ()


def test_runner_artifact_uploader_is_idempotent_for_duplicate_object_keys(tmp_path: Path) -> None:
    artifact_path = tmp_path / "stdout.txt"
    artifact_path.write_text("ok\n", encoding="utf-8")
    sha = "dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22"
    scanned = ScannedArtifactFile(
        artifact_client_id="artifact-1",
        relative_path="stdout.txt",
        absolute_path=artifact_path,
        workspace_root=tmp_path,
        size_bytes=3,
        content_sha256=sha,
        content_type="text/plain",
        is_text=True,
    )

    calls = {"count": 0}

    class _FakeResponse:
        status = 200

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    def _fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        calls["count"] += 1
        return _FakeResponse()

    from drowai_runner import artifact_uploader as uploader_module

    original = uploader_module.urllib_request.urlopen
    uploader_module.urllib_request.urlopen = _fake_urlopen
    try:
        uploader = RunnerArtifactUploader(max_attempts=1)
        duplicate_items = [
            _build_upload_item(artifact_client_id="artifact-1", object_key="tenant/task/one", sha256=sha),
            _build_upload_item(artifact_client_id="artifact-1", object_key="tenant/task/one", sha256=sha),
        ]
        result = uploader.upload(uploads=duplicate_items, files_by_client_id={"artifact-1": scanned})
    finally:
        uploader_module.urllib_request.urlopen = original

    assert calls["count"] == 1
    assert len(result.completed) == 2
    assert result.completed[0].object_key == result.completed[1].object_key
