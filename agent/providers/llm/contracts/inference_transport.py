"""Provider-neutral asynchronous transport contract for live LLM inference.

Adapters consume decoded JSON values and incremental JSON stream events through
this boundary. Endpoint authorization, credentials, HTTP policy, and concrete
network clients remain infrastructure responsibilities outside the agent layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol


class AsyncLLMInferenceTransport(Protocol):
    """Execute bounded JSON requests and incrementally stream JSON events."""

    async def request_json(self, json_body: Mapping[str, Any]) -> Any:
        """Return one decoded JSON response without blocking the event loop."""

    def stream_json_events(
        self,
        json_body: Mapping[str, Any],
    ) -> AsyncIterator[Any]:
        """Yield decoded provider events as soon as each event is received."""


__all__ = ["AsyncLLMInferenceTransport"]
