"""Provider-neutral terminal stream contract helpers.

Responsibilities:
- Define the minimal async terminal stream surface used by runtime providers.
- Centralize stream object detection for managed runner terminal streams.
- Mark push streams whose frames arrive from an external channel instead of polling.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TerminalStreamClient(Protocol):
    """Async stream interface used by terminal runtime providers."""

    session_id: str

    async def send_input(self, data: str | bytes) -> None: ...

    async def read_output(self, size: int = 4096, timeout: float | None = None) -> bytes: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def close(self) -> None: ...


def terminal_stream_from_payload(payload: Mapping[str, Any]) -> TerminalStreamClient | None:
    """Return a provider-compatible terminal stream object from a runtime payload."""
    stream_client = payload.get("socket")
    if stream_client is None:
        return None
    required = ("send_input", "read_output", "resize", "close")
    if all(callable(getattr(stream_client, name, None)) for name in required):
        return stream_client
    return None


def is_push_terminal_stream(stream_client: object | None) -> bool:
    """Return true when output arrives through a channel push sink."""
    return bool(getattr(stream_client, "push_frames", False))


__all__ = [
    "TerminalStreamClient",
    "is_push_terminal_stream",
    "terminal_stream_from_payload",
]
