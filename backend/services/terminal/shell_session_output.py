"""Bounded shell-session output accumulation.

This module owns per-operation PTY output projection for shell sessions. It
keeps bounded public head/tail output plus a small framing tail for marker
detection, and performs no provider I/O or session lifecycle work.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime_shared.shell_session_framing import (
    PtyCommandFrame,
    StreamingPtyFramingParser,
)

_TRUNCATION_MARKER = "\n[... shell output truncated ...]\n"


@dataclass(frozen=True, slots=True)
class ShellSessionCompletion:
    """Completion marker parsed from a bounded output accumulator."""

    exit_code: int


class _BoundedText:
    """Keep deterministic head/tail text without storing a full transcript."""

    def __init__(self, limit: int) -> None:
        self._limit = max(0, int(limit))
        self._total_len = 0
        self._exact = ""
        self._head = ""
        self._tail = ""

    @property
    def truncated(self) -> bool:
        return self._total_len > self._limit

    @property
    def total_len(self) -> int:
        return self._total_len

    @property
    def retained_len(self) -> int:
        return len(self._exact) + len(self._head) + len(self._tail)

    def append(self, value: str) -> None:
        if not value or self._limit <= 0:
            self._total_len += len(value)
            return

        if not self.truncated and self._total_len + len(value) <= self._limit:
            self._exact += value
            self._total_len += len(value)
            return

        if not self.truncated:
            existing = self._exact
            self._exact = ""
            self._append_truncated(existing)

        self._append_truncated(value)
        self._total_len += len(value)

    def text(self) -> str:
        if not self.truncated:
            return self._exact
        if self._limit <= len(_TRUNCATION_MARKER):
            return self._head[: self._limit]
        return f"{self._head}{_TRUNCATION_MARKER}{self._tail}"

    def _append_truncated(self, value: str) -> None:
        if not value:
            return
        head_limit, tail_limit = self._limits()
        if len(self._head) < head_limit:
            needed = head_limit - len(self._head)
            self._head += value[:needed]

        if tail_limit > 0:
            self._tail = (self._tail + value)[-tail_limit:]

    def _limits(self) -> tuple[int, int]:
        if self._limit <= len(_TRUNCATION_MARKER):
            return self._limit, 0
        head_limit = max(0, (self._limit - len(_TRUNCATION_MARKER)) // 2)
        tail_limit = max(0, self._limit - len(_TRUNCATION_MARKER) - head_limit)
        return head_limit, tail_limit


class ShellSessionOutputAccumulator:
    """Accumulate one shell-session read window without retaining transcripts."""

    def __init__(
        self,
        *,
        frame: PtyCommandFrame | None = None,
        parser: StreamingPtyFramingParser | None = None,
        max_output_chars: int,
    ) -> None:
        if parser is None:
            if frame is None:
                raise ValueError("frame or parser is required")
            parser = StreamingPtyFramingParser(frame)
        self._parser = parser
        self._parser.begin_output_window()
        self._max_output_chars = max_output_chars
        self._bounded = _BoundedText(max_output_chars)
        self._provider_output_truncated = False

    @property
    def truncated(self) -> bool:
        return self._provider_output_truncated or self._bounded.truncated

    def mark_provider_output_truncated(self) -> None:
        """Record upstream byte loss without fabricating public output text."""
        self._provider_output_truncated = True

    @property
    def retained_state_chars(self) -> int:
        """Return the helper-owned retained character count for safety tests."""
        return self._bounded.retained_len + self._parser.retained_state_chars

    @property
    def retained_state_limit_chars(self) -> int:
        """Return the configured upper bound for helper-owned retained text."""
        return self._max_output_chars + self._parser.retained_state_limit_chars

    def ingest(self, raw_output: str) -> ShellSessionCompletion | None:
        """Consume decoded provider output and return completion when detected."""
        if not raw_output:
            return None

        try:
            result = self._parser.ingest(raw_output)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if result.stdout:
            self._bounded.append(result.stdout)
        if result.completion is None:
            return None
        return ShellSessionCompletion(exit_code=result.completion.exit_code)

    def stdout(self) -> tuple[str, bool]:
        """Return the current bounded public stdout delta and truncation flag."""
        return self._bounded.text(), self.truncated
