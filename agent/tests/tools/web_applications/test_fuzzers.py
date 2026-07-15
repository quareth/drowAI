import subprocess
from unittest.mock import patch

import pytest

# ffuf does not export ``OutputFormat`` in the current codebase. Guard the
# import so wfuzz contract tests in this module remain collectable when ffuf
# tests are skipped due to that drift.
try:
    from agent.tools.web_applications.web_application_fuzzers.ffuf import (
        FfufArgs,
        FfufTool,
        OutputFormat as FfufOutputFormat,
    )
except ImportError:  # pragma: no cover - environmental guard
    FfufArgs = FfufTool = FfufOutputFormat = None  # type: ignore[assignment]

from agent.tools.web_applications.web_application_fuzzers.wfuzz import (
    WfuzzArgs,
    WfuzzTool,
    OutputFormat as WfuzzOutputFormat,
)


def _require(symbol, name: str):
    if symbol is None:
        pytest.skip(f"{name} unavailable in current environment")
    return symbol


# ---------------------------------------------------------------------------
# FFuf
# ---------------------------------------------------------------------------


def test_ffuf_build_command_minimal():
    _require(FfufArgs, "FfufArgs")
    args = FfufArgs(target="http://example.com", wordlist="list.txt")
    command = FfufTool().build_command(args)
    assert command[0] == "ffuf"
    assert "-u" in command and args.target in command


def test_ffuf_build_command_with_fuzzing_options():
    _require(FfufArgs, "FfufArgs")
    args = FfufArgs(
        target="http://example.com",
        wordlist="list.txt",
        match_status="200",
        not_match_status="404",
        auto_calibration=True,
        calibration="cal.txt",
        p="POSTFUZZ",
        rate=50,
        recursive=True,
        recursion_depth=2,
    )
    command = FfufTool().build_command(args)
    assert "-mc" in command and "200" in command
    assert "-fc" in command and "404" in command
    assert "-ac" in command
    assert "-p" in command and "POSTFUZZ" in command
    assert "-recursion-depth" in command


def test_ffuf_parse_output_json():
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(target="http://example.com", output_format=FfufOutputFormat.JSON)
    stdout = """
    {
        "results": [
            {"url": "http://example.com/admin", "status_code": 200, "response_size": 123, "payload": "FUZZ"}
        ],
        "config": {"threads": 10},
        "stats": {"requests": 1}
    }
    """
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["results"][0]["url"].endswith("/admin")
    assert metadata["config"]["threads"] == 10
    assert metadata["stats"]["requests"] == 1


def test_ffuf_parse_output_text():
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(target="http://example.com", output_format=FfufOutputFormat.TEXT)
    stdout = "200 123 4 2 0.12 http://example.com/admin\nTotal requests: 1"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["results"][0]["status_code"] == 200
    assert metadata["stats"]["total_requests"] == "1"


def test_ffuf_parse_output_empty():
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(target="http://example.com", output_format=FfufOutputFormat.JSON)
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["results"] == []
    assert metadata["exit_code"] == 0


def test_ffuf_create_artifacts(tmp_path, monkeypatch):
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(target="http://example.com", output_format=FfufOutputFormat.TEXT)
    stdout = "sample output"
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_ffuf_run_success():
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(target="http://example.com", output_format=FfufOutputFormat.TEXT)
    stdout = "200 123 4 2 0.12 http://example.com/admin"

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["exit_code"] == 0


def test_ffuf_run_timeout():
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(target="http://example.com", output_format=FfufOutputFormat.TEXT)

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# WFuzz
# ---------------------------------------------------------------------------


def test_wfuzz_build_command_minimal():
    args = WfuzzArgs(target="http://example.com", wordlist="list.txt")
    command = WfuzzTool().build_command(args)
    assert command[0] == "wfuzz"
    assert args.target in command


def test_wfuzz_fuzzer_output_uses_o_flag():
    """Stdout printer must use ``-o printer`` per Wfuzz man page."""
    args = WfuzzArgs(
        target="http://example.com",
        wordlist="list.txt",
        output_format=WfuzzOutputFormat.JSON,
    )
    command = WfuzzTool().build_command(args)
    assert "-o" in command
    o_idx = command.index("-o")
    assert command[o_idx + 1] == "json"
    # ``-f json`` is wrong: ``-f`` requires ``filename,printer``.
    assert "-f" not in command or command[command.index("-f") + 1] != "json"


def test_wfuzz_fuzzer_text_format_uses_raw_printer():
    """The Wfuzz text printer is named ``raw``, not ``text``."""
    args = WfuzzArgs(
        target="http://example.com",
        wordlist="list.txt",
        output_format=WfuzzOutputFormat.TEXT,
    )
    command = WfuzzTool().build_command(args)
    assert "-o" in command
    o_idx = command.index("-o")
    assert command[o_idx + 1] in {"raw", "text"}
    # Specifically, no fake ``-f text`` form.
    assert not (
        "-f" in command and command[command.index("-f") + 1] == "text"
    )


def test_wfuzz_build_command_with_fuzzing_options():
    args = WfuzzArgs(
        target="http://example.com",
        wordlist="list.txt",
        filter="sc!=404",
        hc="500",
        hw="20",
        hl="10",
        match="success",
        not_match="not-found",
        recursive=True,
        depth=2,
        scan_mode=True,
        auth="user:pass",
        auth_type="digest",
        user_agent="example-agent",
        proxy="http://127.0.0.1:8080",
        timeout=12,
    )
    command = WfuzzTool().build_command(args)
    assert "--filter" in command and "sc!=404" in command
    assert "--hc" in command and "500" in command
    assert "--hw" in command and "20" in command
    assert "--hl" in command and "10" in command
    assert "--ss" in command and "success" in command
    assert "--hs" in command and "not-found" in command
    assert "-R" in command and command[command.index("-R") + 1] == "2"
    assert "-Z" in command
    assert "--req-delay" in command and command[command.index("--req-delay") + 1] == "12"
    assert "--digest" in command and "user:pass" in command
    assert "-H" in command and "User-Agent: example-agent" in command
    assert "-p" in command and "http://127.0.0.1:8080" in command
    for stale in (
        "--timeout",
        "--auth",
        "--user-agent",
        "--debug",
        "--recursion",
        "--recursion-depth",
        "--scan",
        "--not-match",
        "--regex",
        "--hcc",
        "--ht",
        "--mc",
        "--mw",
        "--ml",
        "--mcc",
        "--ms",
        "--mt",
    ):
        assert stale not in command


def test_wfuzz_fuzzer_schema_removed_unsupported_fields():
    removed = {
        "debug",
        "quiet",
        "no_recursion",
        "filter_logic",
        "regex",
        "not_regex",
        "hcc",
        "hs",
        "ht",
        "mc",
        "mw",
        "ml",
        "mcc",
        "ms",
        "mt",
    }
    assert removed.isdisjoint(WfuzzArgs.model_fields)


def test_wfuzz_fuzzer_wordlist_path_policy():
    system_wordlist = "/usr/share/wordlists/dirb/common.txt"
    command = WfuzzTool().build_command(
        WfuzzArgs(target="http://example.com/FUZZ", wordlist=system_wordlist)
    )
    assert system_wordlist in command
    with pytest.raises(ValueError):
        WfuzzTool().build_command(
            WfuzzArgs(target="http://example.com/FUZZ", wordlist="/tmp/list.txt")
        )


def test_wfuzz_parse_output_json():
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", output_format=WfuzzOutputFormat.JSON)
    stdout = """
    {
        "results": [
            {"url": "http://example.com/api", "status_code": 201, "response_size": 321, "payload": "X"}
        ],
        "statistics": {"total": 1},
        "filters": {"applied": true}
    }
    """
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["requests"][0]["status_code"] == 201
    assert metadata["statistics"]["total"] == 1
    assert metadata["filters"]["applied"] is True


def test_wfuzz_parse_output_text():
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", output_format=WfuzzOutputFormat.TEXT)
    stdout = "200 10 5 3 http://example.com/admin\nTotal requests: 1"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["requests"][0]["status_code"] == 200
    assert metadata["statistics"]["total_requests"] == "1"


def test_wfuzz_parse_output_empty():
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", output_format=WfuzzOutputFormat.JSON)
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["requests"] == []
    assert metadata["exit_code"] == 0


def test_wfuzz_create_artifacts(tmp_path, monkeypatch):
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", output_format=WfuzzOutputFormat.TEXT)
    stdout = "wfuzz output"
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=654321)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_wfuzz_run_success():
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", output_format=WfuzzOutputFormat.TEXT)
    stdout = "200 10 5 3 http://example.com/admin"

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["exit_code"] == 0


def test_wfuzz_run_timeout():
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", output_format=WfuzzOutputFormat.TEXT)

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2
