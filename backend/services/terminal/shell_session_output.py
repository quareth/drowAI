"""Bounded shell-session output accumulation.

This module owns bounded per-operation output projection for shell sessions.
It performs no provider I/O and does not infer process lifecycle from text.
"""

from __future__ import annotations

_TRUNCATION_MARKER = "\n[... shell output truncated ...]\n"


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
        max_output_chars: int,
    ) -> None:
        self._max_output_chars = max_output_chars
        self._bounded = _BoundedText(max_output_chars)
        self._provider_output_truncated = False
        self._stdout_ends_with_newline = False

    @property
    def truncated(self) -> bool:
        return self._provider_output_truncated or self._bounded.truncated

    @property
    def retained_state_chars(self) -> int:
        """Return the helper-owned retained character count for safety tests."""
        return self._bounded.retained_len

    @property
    def retained_state_limit_chars(self) -> int:
        """Return the configured upper bound for helper-owned retained text."""
        return self._max_output_chars

    @property
    def stdout_ends_with_newline(self) -> bool:
        """Return whether this window's visible stdout ended at a line break."""
        return self._stdout_ends_with_newline

    def ingest(
        self,
        raw_output: str,
        *,
        provider_output_truncated: bool = False,
    ) -> None:
        """Consume provider output and its transport-loss signal atomically."""
        if provider_output_truncated:
            self._provider_output_truncated = True
        if raw_output:
            self._bounded.append(raw_output)
            self._stdout_ends_with_newline = raw_output.endswith(("\n", "\r"))
        return None

    def stdout(self) -> tuple[str, bool]:
        """Return the current bounded public stdout delta and truncation flag."""
        return self._bounded.text(), self.truncated
