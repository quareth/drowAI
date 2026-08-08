"""Executable shell-syntax tests for the shared PTY command frame.

These tests prove that framing preserves Bash pipelines, redirects, quoting,
and multiline command structure while still yielding a parseable exit marker.
"""

from __future__ import annotations

import subprocess

from runtime_shared.shell_session_framing import (
    create_pty_command_frame,
    parse_marked_command_output,
)


def test_framed_command_preserves_pipeline_redirect_quoting_and_multiline(tmp_path) -> None:
    command = '''printf '%s\\n' 'quoted value' \\
  | sed 's/ /-/g' > 'result file.txt'
cat < 'result file.txt'
printf '%s\\n' "second line"'''
    frame = create_pty_command_frame(command, command_id="syntax123")

    completed = subprocess.run(
        ["/bin/bash", "-c", frame.wrapped_command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout, exit_code = parse_marked_command_output(
        completed.stdout,
        frame.start_marker,
        frame.end_marker,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert exit_code == 0
    assert stdout == "quoted-value\nsecond line\n"
    assert (tmp_path / "result file.txt").read_text() == "quoted-value\n"
