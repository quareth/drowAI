"""Focused tests for shared ffuf helper behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.tool_runtime.command_preparation import prepare_tool_command
from agent.tools.web_applications._ffuf_common import (
    materialize_inline_wordlist,
    parse_ffuf_json_text,
    parse_ffuf_text,
    validate_delay,
    validate_fuzz_keyword_present,
    validate_input_cmd,
)
from agent.tools.web_applications.web_application_fuzzers.ffuf import (
    FfufArgs as FuzzerFfufArgs,
    FfufTool as FuzzerFfufTool,
)
from agent.tools.web_applications.web_crawlers.ffuf import (
    FfufArgs as CrawlerFfufArgs,
    FfufTool as CrawlerFfufTool,
)
from runtime_shared.workspace_files import materialize_runtime_workspace_files


def test_materialize_inline_wordlist_writes_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))

    target = materialize_inline_wordlist(["0", "1", "2"], prefix="ffuf_test")

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "0\n1\n2\n"
    assert target.parent == tmp_path / "wordlists"


@pytest.mark.parametrize("value", ["0.1", "1", "0.1-2.0"])
def test_validate_delay_accepts_ffuf_syntax(value):
    assert validate_delay(value) == value


@pytest.mark.parametrize("value", ["abc", "0.1-", "-1", "1,2"])
def test_validate_delay_rejects_invalid_syntax(value):
    with pytest.raises(ValueError):
        validate_delay(value)


def test_validate_fuzz_keyword_present_requires_declared_keywords():
    with pytest.raises(ValueError):
        validate_fuzz_keyword_present(
            target="https://example.com/api?PARAM=1",
            headers=["Content-Type: application/json"],
            data='{"value":"1"}',
            cookies=None,
            extra_keywords=["PARAM", "VALUE"],
        )


def test_parse_ffuf_json_text_accepts_jsonl_stream():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "input": {"FUZZ": "1"},
                    "position": 1,
                    "status": 302,
                    "length": 208,
                    "words": 24,
                    "lines": 7,
                    "url": "http://example.com/data/1",
                }
            ),
            json.dumps(
                {
                    "input": {"FUZZ": "2"},
                    "position": 2,
                    "status": 200,
                    "length": 512,
                    "words": 44,
                    "lines": 12,
                    "url": "http://example.com/data/2",
                }
            ),
        ]
    )

    parsed = parse_ffuf_json_text(stdout)

    assert parsed["stream_format"] == "jsonl"
    assert len(parsed["results"]) == 2
    assert parsed["results"][1]["status"] == 200


def test_parse_ffuf_text_accepts_standard_terminal_rows():
    parsed = parse_ffuf_text(
        "4                       [Status: 302, Size: 208, Words: 21, Lines: 4, Duration: 72ms]\n"
        "1                       [Status: 200, Size: 17144, Words: 7066, Lines: 371, Duration: 70ms]\n",
        target_template="http://example.com/data/FUZZ",
    )

    assert parsed["results"] == [
        {
            "url": "http://example.com/data/4",
            "input": {"FUZZ": "4"},
            "status": 302,
            "length": 208,
            "words": 21,
            "lines": 4,
        },
        {
            "url": "http://example.com/data/1",
            "input": {"FUZZ": "1"},
            "status": 200,
            "length": 17144,
            "words": 7066,
            "lines": 371,
        },
    ]


def test_validate_input_cmd_accepts_single_line_command():
    assert validate_input_cmd("seq 0 200") == "seq 0 200"


@pytest.mark.parametrize(
    "value",
    [
        "python3 - <<'PY'\nprint(1)\nPY",
        "python3 - <<'PY'",
    ],
)
def test_validate_input_cmd_rejects_multiline_and_heredoc(value):
    with pytest.raises(ValueError):
        validate_input_cmd(value)


def test_ffuf_fuzzer_build_command_does_not_force_output_files():
    args = FuzzerFfufArgs(
        target="https://example.com/data/FUZZ",
        wordlist="/usr/share/seclists/Discovery/Web-Content/common.txt",
    )

    command = FuzzerFfufTool().build_command(args)

    assert "-debug-log" not in command
    assert "-audit-log" not in command
    assert "-s" not in command
    assert "-o" not in command
    assert "-of" not in command
    assert "-json" not in command


def test_ffuf_fuzzer_rejects_output_file_controls():
    with pytest.raises(ValidationError):
        FuzzerFfufArgs(
            target="https://example.com/data/FUZZ",
            wordlist="/usr/share/seclists/Discovery/Web-Content/common.txt",
            match_output_dir="artifacts/ffuf_matches",
        )

    with pytest.raises(ValidationError):
        FuzzerFfufArgs(
            target="https://example.com/data/FUZZ",
            wordlist="/usr/share/seclists/Discovery/Web-Content/common.txt",
            debug_log=True,
        )


def test_ffuf_fuzzer_build_command_materializes_inline_wordlist_instead_of_input_cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    tool = FuzzerFfufTool()
    args = FuzzerFfufArgs(
        target="https://example.com/data/FUZZ",
        inline_wordlist=["1", "2", "3", "4"],
    )

    command = tool.build_command(args)

    assert "-w" in command
    assert "-input-cmd" not in command
    wordlist_path = command[command.index("-w") + 1]
    assert wordlist_path.startswith("/workspace/wordlists/ffuf_fuzzer_")
    assert not (tmp_path / wordlist_path.removeprefix("/workspace/")).exists()

    workspace_files = tool.prepare_workspace_files(args)
    materialized = materialize_runtime_workspace_files(
        workspace=tmp_path,
        files=workspace_files,
    )

    assert materialized == [wordlist_path.removeprefix("/workspace/")]
    assert (tmp_path / materialized[0]).read_text(encoding="utf-8") == "1\n2\n3\n4\n"


@pytest.mark.asyncio
async def test_ffuf_crawler_command_prep_declares_inline_wordlist_workspace_file(tmp_path):
    config = SimpleNamespace(task_id=1, tenant_id=7, workspace_path=str(tmp_path))

    prepared = await prepare_tool_command(
        tool_id="web_applications.web_crawlers.ffuf",
        parameters={
            "target": "https://example.com/FUZZ",
            "inline_wordlist": ["admin", "login"],
        },
        config=config,
        transport="file-comm",
        explicit_command_builder=lambda _tool_id, _parameters: "",
    )

    assert " -w /workspace/wordlists/ffuf_crawler_" in f" {prepared.command} "
    assert len(prepared.pre_execution_workspace_files) == 1
    workspace_file = prepared.pre_execution_workspace_files[0]
    assert workspace_file.relative_path.startswith("wordlists/ffuf_crawler_")
    assert workspace_file.content_bytes() == b"admin\nlogin\n"


def test_ffuf_crawler_build_command_does_not_force_output_mode_by_default():
    args = CrawlerFfufArgs(
        target="https://example.com/FUZZ",
        wordlist="/usr/share/wordlists/dirb/common.txt",
    )

    command = CrawlerFfufTool().build_command(args)

    assert "-debug-log" not in command
    assert "-audit-log" not in command
    assert "-s" not in command
    assert "-o" not in command
    assert "-of" not in command
    assert "-json" not in command


def test_ffuf_crawler_build_command_uses_explicit_silent_mode():
    args = CrawlerFfufArgs(
        target="https://example.com/FUZZ",
        wordlist="/usr/share/wordlists/dirb/common.txt",
        silent=True,
    )

    command = CrawlerFfufTool().build_command(args)

    assert "-s" in command


def test_ffuf_crawler_rejects_output_file_controls():
    with pytest.raises(ValidationError):
        CrawlerFfufArgs(
            target="https://example.com/FUZZ",
            wordlist="/usr/share/wordlists/dirb/common.txt",
            json_output_path="artifacts/ffuf.json",
        )


def test_ffuf_fuzzer_postprocess_preserves_stdout_metadata_without_file_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    tool = FuzzerFfufTool()
    args = FuzzerFfufArgs(
        target="https://example.com/data/FUZZ",
        wordlist="/usr/share/seclists/Discovery/Web-Content/common.txt",
    )
    tool.build_command(args)

    result = tool.postprocess_execution(
        args=args,
        stdout="2                       [Status: 200, Size: 512, Words: 44, Lines: 12, Duration: 72ms]\n",
        stderr="",
        exit_code=0,
        success=True,
        metadata={"results": [{"url": "https://example.com/data/2", "status": 200}]},
        artifacts=[],
    )

    assert "Status: 200" in result.stdout
    assert result.metadata["results"][0]["status"] == 200
    assert result.artifacts == []


def test_ffuf_crawler_postprocess_preserves_stdout_metadata_without_file_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    tool = CrawlerFfufTool()
    args = CrawlerFfufArgs(
        target="https://example.com/FUZZ",
        wordlist="/usr/share/wordlists/dirb/common.txt",
    )
    tool.build_command(args)

    result = tool.postprocess_execution(
        args=args,
        stdout="admin                  [Status: 302, Size: 208, Words: 21, Lines: 4, Duration: 72ms]\n",
        stderr="",
        exit_code=0,
        success=True,
        metadata={"results": [{"url": "https://example.com/admin", "status": 302}]},
        artifacts=[],
    )

    assert "admin" in result.stdout
    assert result.metadata["results"][0]["status"] == 302
    assert result.artifacts == []


def test_ffuf_crawler_postprocess_preserves_raw_stdout_without_json_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    tool = CrawlerFfufTool()
    args = CrawlerFfufArgs(
        target="https://example.com/FUZZ",
        wordlist="/usr/share/wordlists/dirb/common.txt",
    )
    tool.build_command(args)

    result = tool.postprocess_execution(
        args=args,
        stdout="4\n13\n20\n",
        stderr="",
        exit_code=0,
        success=True,
        metadata={"results": []},
        artifacts=[],
    )

    assert result.stdout == "4\n13\n20\n"
    assert result.artifacts == []


def test_ffuf_args_reject_unsupported_audit_log_parameter():
    with pytest.raises(ValidationError):
        FuzzerFfufArgs.model_validate(
            {
                "target": "https://example.com/data/FUZZ",
                "wordlist": "/usr/share/seclists/Discovery/Web-Content/common.txt",
                "audit_log": True,
            }
        )

    with pytest.raises(ValidationError):
        CrawlerFfufArgs.model_validate(
            {
                "target": "https://example.com/FUZZ",
                "wordlist": "/usr/share/wordlists/dirb/common.txt",
                "audit_log": True,
            }
        )
