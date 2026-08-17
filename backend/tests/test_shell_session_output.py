"""Tests for text-only bounded shell-session output accumulation."""

from backend.services.terminal.shell_session_output import ShellSessionOutputAccumulator


def test_accumulator_preserves_raw_spacing_and_blank_lines() -> None:
    accumulator = ShellSessionOutputAccumulator(max_output_chars=200)
    accumulator.ingest("login banner\r\n  first  \r\n\r\n second \r\n")

    assert accumulator.stdout() == (
        "login banner\r\n  first  \r\n\r\n second \r\n",
        False,
    )
    assert accumulator.stdout_ends_with_newline is True


def test_accumulator_bounds_large_output_without_parsing_content() -> None:
    accumulator = ShellSessionOutputAccumulator(max_output_chars=256)
    for _ in range(128):
        accumulator.ingest("x" * 4096)
        assert accumulator.retained_state_chars <= accumulator.retained_state_limit_chars
    accumulator.ingest("finished")
    stdout, truncated = accumulator.stdout()

    assert truncated is True
    assert stdout.startswith("x")
    assert "[... shell output truncated ...]" in stdout
    assert stdout.endswith("finished")
    assert len(stdout) <= 256


def test_provider_loss_marks_projection_truncated() -> None:
    accumulator = ShellSessionOutputAccumulator(max_output_chars=200)
    accumulator.ingest("visible", provider_output_truncated=True)
    assert accumulator.stdout() == ("visible", True)
