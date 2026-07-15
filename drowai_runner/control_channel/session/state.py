"""Connection-lifetime mutable state for one cloud channel websocket session.

Owns in-memory ACK/idempotency/tool/artifact state only; no I/O, no websocket
sends, no validation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import queue

from drowai_runner.control_channel.artifacts.models import _PendingArtifactUploadContext
from drowai_runner.control_channel.tool_commands.models import (
    _ToolCommandInflightEntry,
    _ToolCommandCacheEntry,
    _ToolCommandDispatchCompleted,
    _ToolCommandDispatchFailed,
)


@dataclass(slots=True)
class ConnectionSessionState:
    """Mutable state scoped to one connected cloud channel websocket session."""

    ack_decisions_by_message_id: dict[str, tuple[str, str | None]] = field(
        default_factory=dict
    )
    processed_runtime_messages: set[str] = field(default_factory=set)
    assigned_runtime_jobs: dict[str, int | None] = field(default_factory=dict)
    cached_tool_command_results: dict[tuple[str, str], _ToolCommandCacheEntry] = (
        field(default_factory=dict)
    )
    inflight_tool_commands: dict[tuple[str, str], _ToolCommandInflightEntry] = (
        field(default_factory=dict)
    )
    pending_upload_contexts: dict[tuple[str, str], _PendingArtifactUploadContext] = field(
        default_factory=dict
    )
    tool_command_dispatch_events: queue.SimpleQueue[
        _ToolCommandDispatchCompleted | _ToolCommandDispatchFailed
    ] = field(default_factory=queue.SimpleQueue)
