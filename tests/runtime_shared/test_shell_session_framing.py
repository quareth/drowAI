"""Tests for pure PTY shell-session framing helpers."""

import pytest

from runtime_shared.shell_session_framing import (
    PTY_EXIT_CODE_MARKER,
    ShellSessionFramingError,
    StreamingPtyFramingParser,
    create_pty_command_frame,
    parse_marked_command_output,
    strip_pty_artifacts,
)


def test_framing_module_is_backend_free() -> None:
    import inspect
    import runtime_shared.shell_session_framing as framing

    source = inspect.getsource(framing)
    assert framing.__doc__
    assert "from backend" not in source
    assert "import backend" not in source


def test_create_pty_command_frame_uses_unique_markers_and_wrapper() -> None:
    frame = create_pty_command_frame("echo ok", command_id="abc12345")

    assert frame.start_marker == "__DROWAI_CMD_START_abc12345__"
    assert frame.end_marker == "__DROWAI_CMD_END_abc12345__"
    assert frame.start_marker in frame.wrapped_command
    assert frame.end_marker in frame.wrapped_command
    assert f"{PTY_EXIT_CODE_MARKER}%s" in frame.wrapped_command
    assert "echo ok" in frame.wrapped_command


def test_parse_marked_output_ignores_echoed_wrapper_marker() -> None:
    raw = (
        "printf '__DROWAI_CMD_START_abc12345__\\n'; { echo ok; } 2>&1; "
        "__drowai_ec=$?; printf '\\n__DROWAI_CMD_END_abc12345__="
        "__DROWAI_EXIT_CODE__=%s\\n' \"$__drowai_ec\"\n"
        "__DROWAI_CMD_START_abc12345__\n"
        "\x1b[32mok\x1b[0m\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=0\n"
        "__DROWAI_PROMPT__> "
    )

    stdout, exit_code = parse_marked_command_output(
        raw,
        "__DROWAI_CMD_START_abc12345__",
        "__DROWAI_CMD_END_abc12345__",
    )

    assert stdout == "ok"
    assert exit_code == 0
    assert "__DROWAI_CMD_" not in stdout
    assert "\x1b" not in stdout


def test_parse_marked_output_allows_completed_command_with_empty_stdout() -> None:
    raw = (
        "__DROWAI_CMD_START_abc12345__\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=0\n"
    )

    stdout, exit_code = parse_marked_command_output(
        raw,
        "__DROWAI_CMD_START_abc12345__",
        "__DROWAI_CMD_END_abc12345__",
    )

    assert stdout == ""
    assert exit_code == 0


def test_parse_marked_output_preserves_whitespace_and_removes_protocol_separator() -> None:
    raw = (
        "login banner that is outside the command frame\r\n"
        "__DROWAI_CMD_START_abc12345__\r\n"
        "  first value  \r\n"
        "\r\n"
        "\r\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=0\r\n"
    )

    stdout, exit_code = parse_marked_command_output(
        raw,
        "__DROWAI_CMD_START_abc12345__",
        "__DROWAI_CMD_END_abc12345__",
    )

    assert stdout == "  first value  \n\n"
    assert exit_code == 0


def test_parse_marked_output_requires_exact_end_marker() -> None:
    raw = (
        "__DROWAI_CMD_START_abc12345__\n"
        "ok\n"
        "__DROWAI_CMD_END_other__=__DROWAI_EXIT_CODE__=0\n"
    )

    with pytest.raises(ShellSessionFramingError):
        parse_marked_command_output(
            raw,
            "__DROWAI_CMD_START_abc12345__",
            "__DROWAI_CMD_END_abc12345__",
        )


def test_parse_marked_output_rejects_duplicate_completion_marker() -> None:
    raw = (
        "__DROWAI_CMD_START_abc12345__\n"
        "ok\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=0\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=0\n"
    )

    with pytest.raises(ShellSessionFramingError):
        parse_marked_command_output(
            raw,
            "__DROWAI_CMD_START_abc12345__",
            "__DROWAI_CMD_END_abc12345__",
        )


def test_parse_marked_output_rejects_malformed_exit_marker() -> None:
    raw = (
        "__DROWAI_CMD_START_abc12345__\n"
        "ok\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=nope\n"
    )

    with pytest.raises(ShellSessionFramingError):
        parse_marked_command_output(
            raw,
            "__DROWAI_CMD_START_abc12345__",
            "__DROWAI_CMD_END_abc12345__",
        )


def test_strip_pty_artifacts_removes_prompts_and_internal_markers() -> None:
    output = (
        "echo ok\r\n"
        "ok\r\n"
        "__DROWAI_CMD_START_abc12345__\r\n"
        "__DROWAI_CMD_END_abc12345__=__DROWAI_EXIT_CODE__=0\r\n"
        "\x1b[?2004h__DROWAI_PROMPT__> "
    )

    cleaned = strip_pty_artifacts(output, "echo ok")

    assert cleaned == "ok"
    assert "__DROWAI" not in cleaned
    assert "\x1b" not in cleaned


def test_strip_pty_artifacts_preserves_partial_output_without_marker() -> None:
    output = "long-running output\r\nstill running\x1b[?2004h"

    cleaned = strip_pty_artifacts(output, "sleep 30")

    assert cleaned == "long-running output\nstill running"


def test_streaming_parser_drops_banner_and_preserves_arbitrary_chunk_boundaries() -> None:
    frame = create_pty_command_frame("printf output", command_id="stream123")
    parser = StreamingPtyFramingParser(frame)
    chunks = [
        "Welcome to Kali GNU/Linux\r\nLast login: today\r\n",
        frame.start_marker[:12],
        f"{frame.start_marker[12:]}\r\n  hello ",
        "world  \r",
        "\n\r\n\r",
        f"\n{frame.end_marker}=__DROWAI_EXIT_CODE__=0\r\n",
    ]

    results = [parser.ingest(chunk) for chunk in chunks]

    assert "".join(result.stdout for result in results) == "  hello world  \n\n"
    assert results[-1].completion is not None
    assert results[-1].completion.exit_code == 0
    assert "Welcome" not in "".join(result.stdout for result in results)
    assert "Last login" not in "".join(result.stdout for result in results)


def test_streaming_parser_preserves_spaces_split_across_reads() -> None:
    frame = create_pty_command_frame("printf output", command_id="spaces123")
    parser = StreamingPtyFramingParser(frame)

    first = parser.ingest(f"{frame.start_marker}\nvalue ")
    second = parser.ingest(" continues")
    final = parser.ingest(
        f"\n{frame.end_marker}=__DROWAI_EXIT_CODE__=0\n"
    )

    assert first.stdout == "value "
    assert second.stdout == " continues"
    assert final.stdout == ""
    assert final.completion is not None
