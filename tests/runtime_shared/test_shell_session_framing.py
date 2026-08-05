"""Tests for pure PTY shell-session framing helpers."""

import pytest

from runtime_shared.shell_session_framing import (
    PTY_EXIT_CODE_MARKER,
    ShellSessionFramingError,
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
