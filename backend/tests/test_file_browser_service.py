"""Tests for workspace file browser service path handling and archive output."""

import html
import json
import zipfile
from pathlib import Path

import pytest

from backend.services.workspace.file_browser_service import FileBrowserService


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    task_workspace = tmp_path / "task-42"
    task_workspace.mkdir(parents=True, exist_ok=True)
    return task_workspace


def test_validate_path_blocks_traversal(workspace: Path) -> None:
    service = FileBrowserService()
    with pytest.raises(ValueError):
        service.validate_path(workspace, "../../etc/passwd")


def test_directory_tree_stops_before_descending_past_depth_limit(
    workspace: Path,
) -> None:
    current = workspace
    for index in range(15):
        current = current / f"level-{index}"
        current.mkdir()

    with pytest.raises(ValueError, match="depth limit"):
        FileBrowserService().get_directory_tree_at_workspace(workspace_path=workspace)


def test_directory_tree_stops_at_entry_limit(workspace: Path) -> None:
    for index in range(2001):
        (workspace / f"file-{index}.txt").touch()

    with pytest.raises(ValueError, match="entry limit"):
        FileBrowserService().get_directory_tree_at_workspace(workspace_path=workspace)


def test_validate_path_resolves_inside_workspace(workspace: Path) -> None:
    service = FileBrowserService()
    safe = service.validate_path(workspace, "reports/result.txt")
    assert safe == (workspace / "reports" / "result.txt")


def test_get_file_content_sanitizes_markdown(workspace: Path) -> None:
    md_file = workspace / "note.md"
    md_file.write_text('hello<script>alert("x")</script><a onclick="evil()" href="ok">link</a>', encoding="utf-8")

    service = FileBrowserService()
    payload = service.get_file_content_at_workspace(workspace_path=workspace, file_path="/note.md")

    assert payload["preview_type"] == "markdown"
    assert "<script>" not in payload["content"]
    assert "onclick" not in payload["content"]
    assert "<a href=" in payload["content"]


def test_get_file_content_sanitizes_json_and_sets_metadata(workspace: Path) -> None:
    json_file = workspace / "data.json"
    json_file.write_text('{"x":"<img src=x onerror=alert(1)>"}', encoding="utf-8")

    service = FileBrowserService()
    payload = service.get_file_content_at_workspace(workspace_path=workspace, file_path="/data.json")

    assert payload["preview_type"] == "json"
    assert payload["metadata"]["is_valid_json"] is True
    parsed = json.loads(payload["content"])
    assert parsed["x"] == html.escape("<img src=x onerror=alert(1)>")


def test_get_file_content_sanitizes_xml_and_sets_metadata(workspace: Path) -> None:
    xml_file = workspace / "scan.xml"
    xml_file.write_text('<root><item name="x">1<script>2</script></item></root>', encoding="utf-8")

    service = FileBrowserService()
    payload = service.get_file_content_at_workspace(workspace_path=workspace, file_path="/scan.xml")

    assert payload["preview_type"] == "xml"
    assert payload["metadata"]["is_valid_xml"] is True
    assert "<script>" not in payload["content"]


def test_get_file_content_sanitizes_text(workspace: Path) -> None:
    txt = workspace / "plain.txt"
    txt.write_text("<b>danger</b>\nline2", encoding="utf-8")

    service = FileBrowserService()
    payload = service.get_file_content_at_workspace(workspace_path=workspace, file_path="/plain.txt")

    assert payload["preview_type"] == "text"
    assert payload["content"].replace("\r\n", "\n") == "&lt;b&gt;danger&lt;/b&gt;\nline2"
    assert payload["metadata"]["line_count"] == 2


def test_get_directory_tree_returns_nested_nodes(workspace: Path) -> None:
    (workspace / "a").mkdir()
    (workspace / "a" / "b.txt").write_text("ok", encoding="utf-8")

    service = FileBrowserService()
    tree = service.get_directory_tree_at_workspace(workspace_path=workspace)

    assert tree["type"] == "folder"
    assert tree["path"] == "/"
    folder = next(child for child in tree["children"] if child["name"] == "a")
    assert folder["type"] == "folder"
    nested_file = next(child for child in folder["children"] if child["name"] == "b.txt")
    assert nested_file["type"] == "file"
    assert nested_file["path"] == "/a/b.txt"


def test_search_files_case_insensitive(workspace: Path) -> None:
    (workspace / "ScanResult.txt").write_text("a", encoding="utf-8")
    (workspace / "other.log").write_text("b", encoding="utf-8")

    service = FileBrowserService()
    result = service.search_files_at_workspace(workspace_path=workspace, query="scan")

    assert result["total_count"] == 1
    assert result["results"][0]["name"] == "ScanResult.txt"


def test_create_zip_archive_creates_temp_zip(workspace: Path) -> None:
    (workspace / "reports").mkdir()
    (workspace / "reports" / "one.txt").write_text("1", encoding="utf-8")
    (workspace / "two.txt").write_text("2", encoding="utf-8")

    service = FileBrowserService()
    zip_path = service.create_zip_archive_at_workspace(
        workspace_path=workspace,
        file_paths=["/reports", "/two.txt"],
    )

    try:
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as archive:
            members = set(archive.namelist())
            assert "reports/one.txt" in members
            assert "two.txt" in members
    finally:
        zip_path.unlink(missing_ok=True)


def test_create_zip_archive_rejects_nested_symlink_without_reading_target(
    workspace: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-canary.txt"
    outside.write_text("HOST_ONLY_CANARY", encoding="utf-8")
    selected = workspace / "reports"
    selected.mkdir()
    (selected / "linked-secret.txt").symlink_to(outside)

    service = FileBrowserService()

    with pytest.raises(ValueError, match="symlink|unsafe"):
        service.create_zip_archive_at_workspace(
            workspace_path=workspace,
            file_paths=["/reports"],
        )

    assert outside.read_text(encoding="utf-8") == "HOST_ONLY_CANARY"
