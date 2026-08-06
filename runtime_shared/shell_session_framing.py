"""Purpose: provide pure PTY command framing and output parsing helpers.

This module owns shell-session marker construction, command wrapper text, and
terminal-output cleanup. It performs no provider I/O and imports no backend
services.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets

from runtime_shared.terminal_contracts import (
    AGENT_PROMPT_ENV,
    AGENT_PROMPT_MARKER,
)

PTY_EXIT_CODE_MARKER = "__DROWAI_EXIT_CODE__="

# PTY output can include many CSI sequences beyond SGR color codes, including
# bracketed paste mode toggles. Use a broad ECMA-48/VT100-compatible matcher.
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
EXIT_CODE_PATTERN = re.compile(r"__DROWAI_EXIT_CODE__=\d+")


class ShellSessionFramingError(ValueError):
    """Raised when marker-bounded PTY output cannot be parsed safely."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


@dataclass(frozen=True, slots=True)
class PtyCommandFrame:
    """Unique markers and wrapped shell payload for one PTY command."""

    command_id: str
    start_marker: str
    end_marker: str
    wrapped_command: str


def create_pty_command_frame(
    command: str,
    command_id: str | None = None,
) -> PtyCommandFrame:
    """Return marker names and wrapped shell text for a PTY command."""
    safe_command_id = command_id or secrets.token_hex(8)
    start_marker = f"__DROWAI_CMD_START_{safe_command_id}__"
    end_marker = f"__DROWAI_CMD_END_{safe_command_id}__"
    wrapped_command = (
        f"printf '{start_marker}\\n'; "
        f"{{ {command}; }} 2>&1; __drowai_ec=$?; "
        f"printf '\\n{end_marker}={PTY_EXIT_CODE_MARKER}%s\\n' \"$__drowai_ec\"\n"
    )
    return PtyCommandFrame(
        command_id=safe_command_id,
        start_marker=start_marker,
        end_marker=end_marker,
        wrapped_command=wrapped_command,
    )


@dataclass(frozen=True, slots=True)
class PtyFramingCompletion:
    """Parsed completion record for a PTY command frame."""

    exit_code: int


@dataclass(frozen=True, slots=True)
class PtyFramingIngestResult:
    """Visible output and optional completion from one parser ingest."""

    stdout: str
    completion: PtyFramingCompletion | None = None


class StreamingPtyFramingParser:
    """Incrementally parse PTY command protocol records without transcripts."""

    _WAITING_FOR_START = "waiting_for_start"
    _COLLECTING_OUTPUT = "collecting_output"
    _COMPLETE = "complete"
    _ANSI_TEXT = "text"
    _ANSI_ESCAPE = "escape"
    _ANSI_CSI_PARAMETERS = "csi_parameters"
    _ANSI_CSI_INTERMEDIATES = "csi_intermediates"

    def __init__(self, frame: PtyCommandFrame) -> None:
        self._frame = frame
        self._state = self._WAITING_FOR_START
        self._pending_line = ""
        self._pending_carriage_return = False
        self._pending_visible_newline = False
        self._ansi_state = self._ANSI_TEXT
        self._exit_record_prefix = f"{frame.end_marker}={PTY_EXIT_CODE_MARKER}"
        self._line_limit = max(
            len(frame.start_marker),
            len(self._exit_record_prefix) + 3,
        ) + 16

    @property
    def state(self) -> str:
        """Return the parser state for focused protocol tests."""
        return self._state

    @property
    def retained_state_chars(self) -> int:
        """Return retained parser-owned characters."""
        return (
            len(self._pending_line)
            + int(self._pending_carriage_return)
            + int(self._pending_visible_newline)
            + int(self._ansi_state != self._ANSI_TEXT)
        )

    @property
    def retained_state_limit_chars(self) -> int:
        """Return the parser-owned retention bound."""
        return self._line_limit + 3

    def begin_output_window(self) -> None:
        """Start a public read window without carrying a separator into its delta."""
        self._pending_visible_newline = False

    def ingest(self, raw_output: str) -> PtyFramingIngestResult:
        """Consume provider output and return visible output or completion."""
        if not raw_output or self._state == self._COMPLETE:
            return PtyFramingIngestResult(stdout="")

        produced: list[str] = []
        self._pending_line += self._normalize_chunk(raw_output)

        while True:
            newline_index = self._pending_line.find("\n")
            if newline_index == -1:
                break
            line = self._pending_line[:newline_index]
            self._pending_line = self._pending_line[newline_index + 1 :]
            completion = self._consume_complete_line(line, produced)
            if completion is not None:
                return PtyFramingIngestResult(
                    stdout="".join(produced),
                    completion=completion,
                )

        self._flush_pending_output(produced)
        return PtyFramingIngestResult(stdout="".join(produced))

    def _consume_complete_line(
        self,
        line: str,
        produced: list[str],
    ) -> PtyFramingCompletion | None:
        if self._state == self._WAITING_FOR_START:
            if line == self._frame.start_marker:
                self._state = self._COLLECTING_OUTPUT
            return None

        if self._state != self._COLLECTING_OUTPUT:
            return None

        if line.startswith(self._frame.end_marker):
            completion = self._parse_exit_record(line)
            self._state = self._COMPLETE
            self._pending_line = ""
            self._pending_visible_newline = False
            return completion

        visible = self._visible_output(line)
        if visible is not None:
            if self._pending_visible_newline:
                produced.append("\n")
            produced.append(visible)
            self._pending_visible_newline = True
        return None

    def _flush_pending_output(self, produced: list[str]) -> None:
        if not self._pending_line:
            return

        if self._state == self._WAITING_FOR_START:
            if self._frame.start_marker.startswith(self._pending_line):
                return
            self._pending_line = ""
            return

        if self._state != self._COLLECTING_OUTPUT:
            self._pending_line = ""
            return

        if self._is_exit_record_prefix(self._pending_line):
            if len(self._pending_line) > self._line_limit:
                self._pending_line = self._pending_line[: self._line_limit]
            return

        visible = self._visible_output(self._pending_line)
        self._pending_line = ""
        if visible is not None:
            if self._pending_visible_newline:
                produced.append("\n")
                self._pending_visible_newline = False
            produced.append(visible)

    def _normalize_chunk(self, raw_output: str) -> str:
        """Normalize line endings without duplicating a split CRLF sequence."""
        cleaned = self._strip_streaming_ansi(raw_output)
        if self._pending_carriage_return:
            cleaned = f"\r{cleaned}"
            self._pending_carriage_return = False
        if cleaned.endswith("\r"):
            cleaned = cleaned[:-1]
            self._pending_carriage_return = True
        return cleaned.replace("\r\n", "\n").replace("\r", "\n")

    def _strip_streaming_ansi(self, raw_output: str) -> str:
        """Strip CSI controls across arbitrary chunks with constant retained state."""
        visible: list[str] = []
        for char in raw_output:
            codepoint = ord(char)
            if self._ansi_state == self._ANSI_TEXT:
                if char == "\x1b":
                    self._ansi_state = self._ANSI_ESCAPE
                else:
                    visible.append(char)
                continue

            if self._ansi_state == self._ANSI_ESCAPE:
                if char == "[":
                    self._ansi_state = self._ANSI_CSI_PARAMETERS
                elif char == "\x1b":
                    visible.append("\x1b")
                else:
                    visible.extend(("\x1b", char))
                    self._ansi_state = self._ANSI_TEXT
                continue

            if self._ansi_state == self._ANSI_CSI_PARAMETERS:
                if 0x30 <= codepoint <= 0x3F:
                    continue
                if 0x20 <= codepoint <= 0x2F:
                    self._ansi_state = self._ANSI_CSI_INTERMEDIATES
                    continue
                if 0x40 <= codepoint <= 0x7E:
                    self._ansi_state = self._ANSI_TEXT
                    continue
                self._ansi_state = self._ANSI_TEXT
                visible.append(char)
                continue

            if 0x20 <= codepoint <= 0x2F:
                continue
            if 0x40 <= codepoint <= 0x7E:
                self._ansi_state = self._ANSI_TEXT
                continue
            self._ansi_state = self._ANSI_TEXT
            visible.append(char)
        return "".join(visible)

    def _is_exit_record_prefix(self, line: str) -> bool:
        if self._exit_record_prefix.startswith(line):
            return True
        if not line.startswith(self._exit_record_prefix):
            return False
        exit_digits = line[len(self._exit_record_prefix) :]
        return exit_digits.isdigit()

    def _parse_exit_record(self, line: str) -> PtyFramingCompletion:
        if not line.startswith(self._exit_record_prefix):
            raise ShellSessionFramingError(
                "Exit code marker missing or malformed in end marker line",
                raw_output=line,
            )
        exit_digits = line[len(self._exit_record_prefix) :]
        if not exit_digits.isdigit():
            raise ShellSessionFramingError(
                "Exit code marker missing or malformed in end marker line",
                raw_output=line,
            )
        return PtyFramingCompletion(exit_code=int(exit_digits))

    def _visible_output(self, line: str) -> str | None:
        if line == self._frame.start_marker:
            return None
        if line.startswith(self._frame.end_marker):
            return None
        if PTY_EXIT_CODE_MARKER in line:
            return None
        return strip_pty_artifacts(line, self._frame.wrapped_command)


def normalize_pty_output(output: str) -> str:
    """Strip terminal control sequences and normalize PTY line endings."""
    cleaned = ANSI_ESCAPE_PATTERN.sub("", output)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


def parse_marked_command_output(
    raw_output: str,
    start_marker: str,
    end_marker: str,
) -> tuple[str, int]:
    """Parse stdout and exit code from marker-bounded PTY output."""
    cleaned = normalize_pty_output(raw_output)
    start_idx = cleaned.rfind(start_marker)
    end_idx = cleaned.find(end_marker, start_idx) if start_idx != -1 else -1

    if start_idx == -1 or end_idx == -1:
        raise ShellSessionFramingError(
            f"Missing command markers (start_idx={start_idx}, end_idx={end_idx})",
            raw_output=raw_output,
        )

    if cleaned.find(end_marker, end_idx + len(end_marker)) != -1:
        raise ShellSessionFramingError(
            "Duplicate command completion marker",
            raw_output=raw_output,
        )

    nl_idx = cleaned.find("\n", start_idx)
    content_start = (nl_idx + 1) if nl_idx != -1 else (start_idx + len(start_marker))
    if content_start > end_idx:
        raise ShellSessionFramingError(
            f"Invalid marker bounds (content_start={content_start}, end_idx={end_idx})",
            raw_output=raw_output,
        )

    command_output = cleaned[content_start:end_idx]
    if command_output.endswith("\n"):
        command_output = command_output[:-1]
    end_line_start = end_idx
    end_line_end = cleaned.find("\n", end_idx)
    if end_line_end == -1:
        end_line_end = len(cleaned)
    end_line = cleaned[end_line_start:end_line_end]
    match = re.search(rf"{re.escape(PTY_EXIT_CODE_MARKER)}(\d+)", end_line)
    if not match:
        raise ShellSessionFramingError(
            "Exit code marker missing or malformed in end marker line",
            raw_output=raw_output,
        )

    return command_output, int(match.group(1))


def parse_exit_code_from_combined_output(output: str) -> int:
    """Parse an exit code from output containing the PTY exit-code marker."""
    cleaned = normalize_pty_output(output)
    for line in cleaned.split("\n"):
        line = line.strip()
        if not line:
            continue
        if PTY_EXIT_CODE_MARKER in line:
            tail = line.split(PTY_EXIT_CODE_MARKER, 1)[1]
            tail = (
                tail.replace(AGENT_PROMPT_MARKER, "")
                .replace(AGENT_PROMPT_ENV, "")
                .strip()
            )
            if tail.isdigit():
                return int(tail)
    return parse_legacy_exit_code(output)


def strip_exit_code_marker(output: str) -> str:
    """Remove lines containing the internal PTY exit-code marker."""
    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        line for line in normalized.split("\n") if PTY_EXIT_CODE_MARKER not in line
    )


def parse_legacy_exit_code(output: str) -> int:
    """Extract a legacy prompt-delimited integer exit code, defaulting to error."""
    cleaned = normalize_pty_output(output)
    for line in cleaned.strip().split("\n"):
        line = line.strip()
        if line.startswith("echo"):
            continue
        normalized = (
            line.replace(AGENT_PROMPT_MARKER, "")
            .replace(AGENT_PROMPT_ENV, "")
            .strip()
        )
        if normalized and normalized.isdigit():
            return int(normalized)
    return 1


def strip_pty_artifacts(output: str, command: str) -> str:
    """Remove PTY control artifacts while preserving meaningful command output."""
    cleaned = normalize_pty_output(output)
    lines = cleaned.split("\n")

    if lines and command and command in lines[0]:
        lines = lines[1:]

    cleaned_lines: list[str] = []
    for line in lines:
        if AGENT_PROMPT_MARKER in line or AGENT_PROMPT_ENV in line:
            stripped = (
                line.replace(AGENT_PROMPT_MARKER, "")
                .replace(AGENT_PROMPT_ENV, "")
                .strip()
            )
            if stripped and not stripped.startswith("__DROWAI"):
                cleaned_lines.append(stripped)
        elif "__DROWAI_CMD_" in line or "__DROWAI_EXIT_CODE__" in line:
            continue
        else:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)
