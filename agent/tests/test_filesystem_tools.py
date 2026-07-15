"""Behavior tests for workspace-scoped filesystem tools."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from agent.tools.filesystem.append_file import FsAppendTool
from agent.tools.filesystem.find_paths import FsFindTool
from agent.tools.filesystem.list_dir import FsListDirTool
from agent.tools.filesystem.read_file import FsReadTool
from agent.tools.filesystem.search_text import FsSearchTextTool
from agent.tools.filesystem.write_file import FsWriteTool
from agent.tools.filesystem.contracts import FsFindArgs, FsReadArgs, FsSearchTextArgs
from agent.tools.shell.exec import ShellExecTool


@pytest.fixture()
def workspace(tmp_path: Path):
    original = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if original is None:
            os.environ.pop("WORKSPACE", None)
        else:
            os.environ["WORKSPACE"] = original


def test_write_read_cycle(workspace: Path):
    writer = FsWriteTool()
    content = "Hello workspace tooling!"
    write_result = writer.validate_and_run(
        {
            "path": "notes/test.txt",
            "content": content,
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    assert write_result.success

    reader = FsReadTool()
    read_result = reader.validate_and_run(
        {
            "path": "notes/test.txt",
            "encoding": "utf-8",
            "max_bytes": 1024,
        }
    )
    assert read_result.success
    meta = read_result.metadata.get("fs_read", {})
    assert meta.get("bytes_read") == len(content)
    assert meta.get("content") == content


def test_append_list_and_find(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "project/data.txt",
            "content": "alpha\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )

    appender = FsAppendTool()
    append_result = appender.validate_and_run(
        {
            "path": "project/data.txt",
            "content": "beta\n",
            "encoding": "utf-8",
            "create_if_missing": False,
        }
    )
    assert append_result.success

    lister = FsListDirTool()
    list_result = lister.validate_and_run(
        {
            "path": "project",
            "recursive": False,
            "max_results": 10,
        }
    )
    assert list_result.success
    entries = list_result.metadata.get("fs_list", {}).get("entries", [])
    assert any(entry.get("path") == "project/data.txt" for entry in entries)

    finder = FsFindTool()
    find_result = finder.validate_and_run(
        {
            "path": "project",
            "filename_glob": "*.txt",
            "max_results": 5,
        }
    )
    assert find_result.success
    matches = find_result.metadata.get("fs_find", {}).get("matches", [])
    assert matches and matches[0].get("path") == "project/data.txt"


def test_search_text(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "logs/app.log",
            "content": "INFO ready\nERROR issue detected\nINFO done\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )

    searcher = FsSearchTextTool()
    search_result = searcher.validate_and_run(
        {
            "query": "ERROR",
            "path": "logs",
            "recursive": True,
            "case_sensitive": True,
            "max_results": 5,
        }
    )
    assert search_result.success
    assert "Located 1 matches" in search_result.stdout
    assert "logs/app.log:2:" in search_result.stdout
    assert "ERROR issue detected" in search_result.stdout
    matches = search_result.metadata.get("fs_search_text", {}).get("matches", [])
    assert matches and "issue detected" in matches[0].get("snippet", "")


def test_find_paths_no_match_stdout_is_scoped(workspace: Path):
    (workspace / "project").mkdir()

    finder = FsFindTool()
    find_result = finder.validate_and_run(
        {
            "path": "project",
            "filename_glob": "missing.json",
            "max_results": 5,
        }
    )

    assert find_result.success
    assert "No matching paths found for filename_glob 'missing.json' under /workspace/project." in find_result.stdout
    assert "match_count=0" in find_result.stdout
    assert "use path" not in find_result.stdout


def test_find_paths_empty_command_output_renders_scoped_no_match():
    finder = FsFindTool()
    stdout, stderr = finder.render_result_output(
        args=FsFindArgs(path=".", filename_glob="missing.json"),
        stdout="",
        stderr="",
    )

    assert stderr == ""
    assert "No matching paths found for filename_glob 'missing.json' under /workspace." in stdout
    assert "match_count=0" in stdout
    assert "use path" not in stdout


def test_find_paths_empty_root_search_mentions_root_scope():
    finder = FsFindTool()
    stdout, _ = finder.render_result_output(
        args=FsFindArgs(path="/", filename_glob="missing.json"),
        stdout="",
        stderr="",
    )

    assert "under /." in stdout
    assert "match_count=0" in stdout
    assert "use path" not in stdout


def test_search_text_no_match_stdout_is_scoped(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "logs/app.log",
            "content": "INFO ready\nINFO done\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )

    searcher = FsSearchTextTool()
    search_result = searcher.validate_and_run(
        {
            "query": "ERROR",
            "path": "logs",
            "recursive": True,
            "case_sensitive": True,
            "max_results": 5,
        }
    )

    assert search_result.success
    assert "No text matches found for 'ERROR' under /workspace/logs." in search_result.stdout
    assert "match_count=0" in search_result.stdout
    assert "use path" not in search_result.stdout


def test_search_text_empty_command_output_renders_scoped_no_match():
    searcher = FsSearchTextTool()
    stdout, stderr = searcher.render_result_output(
        args=FsSearchTextArgs(path="/var/log", query="ERROR"),
        stdout="",
        stderr="",
    )

    assert stderr == ""
    assert "No text matches found for 'ERROR' under /var/log." in stdout
    assert "match_count=0" in stdout
    assert "use path" not in stdout


def test_search_text_no_match_exit_is_informational():
    args = FsSearchTextArgs(path="artifacts", query="missing")
    tool = FsSearchTextTool()

    assert tool.is_success_exit_code(1, args, stdout="", stderr="") is True
    assert (
        tool.is_success_exit_code(
            2,
            args,
            stdout="",
            stderr="grep: /workspace/artifacts: Is a directory",
        )
        is False
    )


def test_read_file_grep_no_match_exit_is_informational():
    args = FsReadArgs(path="artifact.txt", read_mode="grep", grep_pattern="missing")
    tool = FsReadTool()

    assert tool.is_success_exit_code(1, args, stdout="", stderr="") is True
    assert (
        tool.is_success_exit_code(
            1,
            args,
            stdout="",
            stderr="grep: artifact.txt: No such file or directory",
        )
        is False
    )


def test_read_file_grep_metadata_includes_line_evidence(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "artifacts/scan.xml",
            "content": (
                "<scaninfo type=\"connect\" protocol=\"tcp\" numservices=\"1\" services=\"443\"/>\n"
                "<address addr=\"127.0.0.1\" addrtype=\"ipv4\"/>\n"
            ),
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )

    reader = FsReadTool()
    read_result = reader.validate_and_run(
        {
            "path": "artifacts/scan.xml",
            "read_mode": "grep",
            "grep_pattern": "scaninfo|address",
            "encoding": "utf-8",
        }
    )

    assert read_result.success
    metadata = read_result.metadata.get("fs_read", {})
    assert metadata.get("line_evidence") == [
        "1:<scaninfo type=\"connect\" protocol=\"tcp\" numservices=\"1\" services=\"443\"/>",
        "2:<address addr=\"127.0.0.1\" addrtype=\"ipv4\"/>",
    ]


def test_search_text_single_file_path(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "artifacts/scan.txt",
            "content": "Security Dashboard\nSecurity Snapshot\nDone\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )

    searcher = FsSearchTextTool()
    search_result = searcher.validate_and_run(
        {
            "query": "Security Snapshot",
            "path": "artifacts/scan.txt",
            "recursive": False,
            "case_sensitive": False,
            "max_results": 5,
            "context_before": 0,
            "context_after": 0,
        }
    )
    assert search_result.success
    assert "Located 1 matches" in search_result.stdout
    assert "artifacts/scan.txt:2:Security Snapshot" in search_result.stdout
    matches = search_result.metadata.get("fs_search_text", {}).get("matches", [])
    assert len(matches) == 1
    assert matches[0].get("path") == "artifacts/scan.txt"
    assert "Security Snapshot" in matches[0].get("snippet", "")


def test_search_text_missing_path_is_successful(workspace: Path):
    searcher = FsSearchTextTool()
    search_result = searcher.validate_and_run(
        {
            "query": "anything",
            "path": "artifacts/does-not-exist.txt",
            "recursive": False,
            "case_sensitive": False,
            "max_results": 5,
        }
    )

    assert search_result.success
    assert search_result.exit_code == 0
    assert search_result.stderr == ""
    metadata = search_result.metadata.get("fs_search_text", {})
    assert metadata.get("not_found") is True
    assert metadata.get("searched_path") == "artifacts/does-not-exist.txt"
    assert metadata.get("matches") == []


def test_find_paths_rejects_single_file_root(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "artifacts/scan.txt",
            "content": "artifact\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )

    finder = FsFindTool()
    find_result = finder.validate_and_run(
        {
            "path": "artifacts/scan.txt",
            "filename_glob": "*.txt",
            "max_results": 5,
        }
    )
    assert find_result.success is False
    assert find_result.metadata.get("error") == "not_directory"
    assert "not a directory" in find_result.stderr


def test_shell_exec_noninteractive(workspace: Path):
    exec_tool = ShellExecTool()
    command = "echo langgraph"
    result = exec_tool.validate_and_run({"command": command, "timeout_sec": 10})
    assert result.success
    metadata = result.metadata.get("shell_exec", {})
    assert "langgraph" in metadata.get("stdout", "") or "langgraph" in result.stdout


def test_read_file_line_count(workspace: Path):
    writer = FsWriteTool()
    lines = [f"line-{i}" for i in range(1, 21)]
    writer.validate_and_run(
        {
            "path": "counts/sample.txt",
            "content": "\n".join(lines) + "\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run({"path": "counts/sample.txt", "read_mode": "full"})
    meta = result.metadata.get("fs_read", {})
    assert meta.get("total_lines") == 20
    assert meta.get("lines_read") == 20
    assert meta.get("read_mode_used") == "full"


def test_read_file_head_mode(workspace: Path):
    writer = FsWriteTool()
    content = "\n".join([f"line-{i}" for i in range(1, 201)]) + "\n"
    writer.validate_and_run(
        {
            "path": "head/long.txt",
            "content": content,
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run({"path": "head/long.txt", "read_mode": "head", "num_lines": 50})
    meta = result.metadata.get("fs_read", {})
    content_lines = meta.get("content", "").splitlines()
    assert len(content_lines) == 50
    assert meta.get("lines_read") == 50
    assert meta.get("total_lines") == 200
    assert meta.get("read_mode_used") == "head"
    assert content_lines[0] == "line-1"
    assert content_lines[-1] == "line-50"


def test_read_file_tail_mode(workspace: Path):
    writer = FsWriteTool()
    content = "\n".join([f"line-{i}" for i in range(1, 201)]) + "\n"
    writer.validate_and_run(
        {
            "path": "tail/long.txt",
            "content": content,
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run({"path": "tail/long.txt", "read_mode": "tail", "num_lines": 50})
    meta = result.metadata.get("fs_read", {})
    content_lines = meta.get("content", "").splitlines()
    assert len(content_lines) == 50
    assert meta.get("lines_read") == 50
    assert meta.get("total_lines") == 200
    assert content_lines[0] == "line-151"
    assert content_lines[-1] == "line-200"


def test_read_file_range_mode(workspace: Path):
    writer = FsWriteTool()
    content = "\n".join([f"line-{i}" for i in range(1, 201)]) + "\n"
    writer.validate_and_run(
        {
            "path": "range/long.txt",
            "content": content,
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run(
        {"path": "range/long.txt", "read_mode": "range", "start_line": 100, "num_lines": 50}
    )
    meta = result.metadata.get("fs_read", {})
    content_lines = meta.get("content", "").splitlines()
    assert len(content_lines) == 50
    assert meta.get("lines_read") == 50
    assert meta.get("line_range") == (100, 149)
    assert content_lines[0] == "line-100"
    assert content_lines[-1] == "line-149"


def test_read_file_grep_mode(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "grep/log.txt",
            "content": "INFO ready\nERROR issue\nDEBUG skip\nERROR critical\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run(
        {"path": "grep/log.txt", "read_mode": "grep", "grep_pattern": "ERROR", "num_lines": 10}
    )
    meta = result.metadata.get("fs_read", {})
    assert meta.get("lines_read") == 2
    assert "ERROR issue" in meta.get("content", "")
    assert "ERROR critical" in meta.get("content", "")
    assert meta.get("read_mode_used") == "grep"


def test_read_file_auto_detect_mode(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "auto/log.txt",
            "content": "INFO ready\nERROR issue\nDEBUG done\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()

    grep_result = reader.validate_and_run({"path": "auto/log.txt", "grep_pattern": "ERROR"})
    assert grep_result.metadata.get("fs_read", {}).get("read_mode_used") == "grep"

    range_result = reader.validate_and_run({"path": "auto/log.txt", "start_line": 2, "num_lines": 1})
    assert range_result.metadata.get("fs_read", {}).get("read_mode_used") == "range"

    head_result = reader.validate_and_run({"path": "auto/log.txt", "num_lines": 1})
    assert head_result.metadata.get("fs_read", {}).get("read_mode_used") == "head"

    full_result = reader.validate_and_run({"path": "auto/log.txt", "max_bytes": 2_000_000})
    assert full_result.metadata.get("fs_read", {}).get("read_mode_used") == "full"


def test_read_file_with_line_numbers(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "numbers/log.txt",
            "content": "alpha\nbeta\ngamma\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run(
        {"path": "numbers/log.txt", "read_mode": "head", "num_lines": 2, "include_line_numbers": True}
    )
    lines = result.stdout.splitlines()
    assert lines[0].startswith("1| ")
    assert lines[0].endswith("alpha")
    assert lines[1].startswith("2| ")
    assert lines[1].endswith("beta")


def test_read_file_byte_based_legacy(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "legacy/data.bin",
            "content": "0123456789",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run({"path": "legacy/data.bin", "start_byte": 2, "max_bytes": 4, "encoding": None})
    meta = result.metadata.get("fs_read", {})
    assert meta.get("bytes_read") == 4
    assert meta.get("lines_read") is None
    assert meta.get("read_mode_used") == "byte"
    assert "Read 4 bytes" in result.stdout


def test_read_file_large_file_progressive(workspace: Path):
    writer = FsWriteTool()
    content = "\n".join([f"line-{i}" for i in range(1, 5001)]) + "\n"
    writer.validate_and_run(
        {
            "path": "large/log.txt",
            "content": content,
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    start = time.time()
    result = reader.validate_and_run({"path": "large/log.txt", "read_mode": "tail", "num_lines": 100})
    duration = time.time() - start
    meta = result.metadata.get("fs_read", {})
    assert duration < 1.0
    assert meta.get("lines_read") == 100
    assert meta.get("total_lines") == 5000


def test_read_file_invalid_range(workspace: Path):
    writer = FsWriteTool()
    content = "\n".join([f"line-{i}" for i in range(1, 101)]) + "\n"
    writer.validate_and_run(
        {
            "path": "invalid/range.txt",
            "content": content,
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run(
        {"path": "invalid/range.txt", "read_mode": "range", "start_line": 1000, "num_lines": 10}
    )
    meta = result.metadata.get("fs_read", {})
    assert meta.get("lines_read") == 0
    assert meta.get("line_range") == (1000, 1009)
    assert result.success


def test_read_file_invalid_grep_pattern(workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "invalid/grep.txt",
            "content": "alpha\nbeta\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()
    result = reader.validate_and_run(
        {"path": "invalid/grep.txt", "read_mode": "grep", "grep_pattern": "["}
    )
    meta = result.metadata.get("fs_read", {})
    assert meta.get("read_mode_used") == "byte"
    assert result.stderr


def test_read_file_subprocess_failure_fallback(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    writer = FsWriteTool()
    writer.validate_and_run(
        {
            "path": "fallback/data.txt",
            "content": "one\ntwo\nthree\n",
            "encoding": "utf-8",
            "create_parents": True,
            "overwrite": "overwrite",
        }
    )
    reader = FsReadTool()

    def boom(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["head"])

    monkeypatch.setattr("agent.tools.filesystem.read_file._read_head", boom)
    result = reader.validate_and_run({"path": "fallback/data.txt", "read_mode": "head", "num_lines": 1})
    meta = result.metadata.get("fs_read", {})
    assert meta.get("read_mode_used") == "byte"
    assert result.stderr
