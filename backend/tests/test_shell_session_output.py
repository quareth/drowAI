"""Tests for bounded shell-session output accumulation.

The accumulator must preserve exact visible text while bounding retained state
and still recognize a completion marker after very large output.
"""

from runtime_shared.shell_session_framing import (
    PTY_EXIT_CODE_MARKER,
    StreamingPtyFramingParser,
    create_pty_command_frame,
)

from backend.services.terminal.shell_session_output import (
    ShellSessionOutputAccumulator,
)


def test_accumulator_preserves_untruncated_spacing_and_blank_lines() -> None:
    frame = create_pty_command_frame("printf output")
    accumulator = ShellSessionOutputAccumulator(frame=frame, max_output_chars=200)

    assert accumulator.ingest(f"login banner\r\n{frame.start_marker}\r\n") is None
    assert accumulator.ingest("  first  \r\n\r\n second \r\n") is None
    completion = accumulator.ingest(
        f"{frame.end_marker}={PTY_EXIT_CODE_MARKER}0\r\n"
    )

    assert completion is not None
    assert completion.exit_code == 0
    assert accumulator.stdout() == ("  first  \n\n second ", False)


def test_accumulator_preserves_line_separator_across_output_windows() -> None:
    frame = create_pty_command_frame("printf output")
    parser = StreamingPtyFramingParser(frame)

    first_window = ShellSessionOutputAccumulator(
        parser=parser,
        max_output_chars=200,
    )
    first_window.ingest(f"{frame.start_marker}\none\n")

    second_window = ShellSessionOutputAccumulator(
        parser=parser,
        max_output_chars=200,
    )
    second_window.ingest("two\n")

    first_stdout, _ = first_window.stdout()
    second_stdout, _ = second_window.stdout()
    assert first_stdout == "one"
    assert first_window.stdout_ends_with_newline is True
    assert second_stdout == "two"
    assert second_window.stdout_ends_with_newline is True


def test_accumulator_bounds_large_output_and_finds_trailing_completion() -> None:
    frame = create_pty_command_frame("large output")
    accumulator = ShellSessionOutputAccumulator(frame=frame, max_output_chars=256)

    accumulator.ingest(f"{frame.start_marker}\n")
    for _ in range(128):
        accumulator.ingest("x" * 4096)
        assert (
            accumulator.retained_state_chars
            <= accumulator.retained_state_limit_chars
        )
    completion = accumulator.ingest(
        f"\nfinished\n{frame.end_marker}={PTY_EXIT_CODE_MARKER}23\n"
    )
    stdout, truncated = accumulator.stdout()

    assert completion is not None
    assert completion.exit_code == 23
    assert truncated is True
    assert stdout.startswith("x")
    assert "[... shell output truncated ...]" in stdout
    assert stdout.endswith("finished")
    assert len(stdout) <= 256
