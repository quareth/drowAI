"""Runtime configuration contracts for the LangGraph chat facade."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
)

if TYPE_CHECKING:
    from agent.graph.state import InteractiveState
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer


class ExecutionMode(str, Enum):
    """High-level execution mode for a chat turn."""

    NORMAL_CHAT = "normal_chat"
    DEEP_REASONING = "deep_reasoning"
    SIMPLE_TOOL = "simple_tool_execution"


class AgentMode(str, Enum):
    """Agent execution mode controlling approval requirements."""

    FULL_ACCESS = "full_access"
    """Current behavior - autonomous execution, no interrupts."""

    AGENT = "agent"
    """Requires approval before each tool execution."""

    PLAN = "plan"
    """Future: Plan review + tool approval."""

    CHAT = "chat"
    """Chat only, no tool access."""

@dataclass(slots=True)
class ChatInputs:
    """Normalized inputs required to process a single chat turn."""

    task_id: int
    user_id: int
    message: str
    conversation_id: Optional[str]
    history: Sequence[Dict[str, Any]]
    # Backend-only positional sidecar; never copied into prompt or graph state.
    history_source_message_ids: Sequence[int] = ()
    api_key: InitVar[Optional[str]] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    credential_ref: Optional[Dict[str, Any]] = None
    llm_runtime_selection: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    anchor_sequence: Optional[int] = None
    requested_mode: Optional[ExecutionMode] = None
    agent_mode: AgentMode = AgentMode.FULL_ACCESS
    # Phase 6: ``plan_mode`` is a route overlay that can be stacked on top
    # of ``agent_mode=agent`` or ``agent_mode=full_access`` to force the
    # deep-reasoning branch while keeping the underlying autonomy / tool
    # approval semantics of the primary mode. It is NOT a third autonomy
    # mode — tool approval keys off ``agent_mode`` alone. The request
    # boundary normalizes legacy ``agent_mode=plan`` into
    # ``agent_mode=agent`` + ``plan_mode=True`` so downstream code reads
    # a single shape.
    plan_mode: bool = False


@dataclass(slots=True)
class ToolingContext:
    """Placeholder for tool availability/configuration."""

    available_tools: List[str] = field(default_factory=list)
    default_capability: Optional[str] = None
    catalog: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class PersistenceContext:
    """Controls persistence behaviour for the current turn.

    The optional ``state_container`` field carries the turn-scoped
    ``ChatStateContainer`` through the runtime without placing a live
    Python object into ``runtime_config.metadata`` (which is copied
    into initial graph state and must remain serialization-safe).
    """

    should_persist: bool = True
    anchor_sequence: Optional[int] = None
    state_container: Optional["ChatStateContainer"] = None


@dataclass(slots=True)
class LangGraphRuntimeConfig:
    """Aggregated runtime configuration passed to graph builders."""

    chat_inputs: ChatInputs
    tooling: ToolingContext = field(default_factory=ToolingContext)
    persistence: PersistenceContext = field(default_factory=PersistenceContext)
    execution_mode: ExecutionMode = ExecutionMode.NORMAL_CHAT
    metadata: Dict[str, Any] = field(default_factory=dict)
    llm_runtime_selection: Optional[Dict[str, Any]] = None
    runtime_services: Any = None


@dataclass(slots=True)
class RuntimeWarmupStatus:
    """Compact readiness summary for per-task runtime warmup state."""

    checkpointer_ready: bool = False
    tool_catalog_ready: bool = False
    pty_session_ready: bool = False
    runtime_warm: bool = False
    pty_warmup_required: bool = False


def runtime_warmup_status_from_steps(raw_status: Any) -> RuntimeWarmupStatus:
    """Build compact warmup readiness flags from step-level warmup status."""
    checkpointer_step = raw_status.get("checkpointer", {}) if isinstance(raw_status, dict) else {}
    tool_step = raw_status.get("tool_catalog", {}) if isinstance(raw_status, dict) else {}
    pty_step = raw_status.get("pty_session", {}) if isinstance(raw_status, dict) else {}

    checkpointer_ready = bool(checkpointer_step.get("ready"))
    tool_catalog_ready = bool(tool_step.get("ready"))
    pty_session_ready = bool(pty_step.get("ready"))
    pty_warmup_required = not bool(pty_step.get("skipped"))
    runtime_warm = checkpointer_ready and tool_catalog_ready and (
        pty_session_ready or not pty_warmup_required
    )

    return RuntimeWarmupStatus(
        checkpointer_ready=checkpointer_ready,
        tool_catalog_ready=tool_catalog_ready,
        pty_session_ready=pty_session_ready,
        runtime_warm=runtime_warm,
        pty_warmup_required=pty_warmup_required,
    )


async def _empty_async_iterator() -> AsyncIterator[Dict[str, Any]]:
    if False:  # pragma: no cover - structural placeholder
        yield {}
    return  # type: ignore[misc]


@dataclass(slots=True)
class LangGraphChatResult:
    """Result container returned by the LangGraph facade."""

    final_text: Optional[str]
    conversation_id: Optional[str]
    interactive_state: Optional["InteractiveState"] = None  # type: ignore[name-defined]
    metadata: Dict[str, Any] = field(default_factory=dict)
    _event_iterator: Callable[[], AsyncIterator[Dict[str, Any]]] = field(
        default=_empty_async_iterator
    )
    # Token usage from LLM calls during this turn.
    #
    # Each item is either a plain ``UsageData`` (legacy / lightweight paths)
    # or a ``UsageRecordWithMetadata`` envelope carrying the canonical
    # ``UsageRecordMetadata`` for that call. The envelope shape is what
    # lets handlers stop collapsing rich ``trace.usage_records`` dicts
    # down to bare ``UsageData`` before persistence (Task 1.2).
    usage: Optional[List[Any]] = None
    # Flag indicating handler already persisted the turn (ChatMessage update)
    persistence_handled: bool = False

    def iter_events(self) -> AsyncIterator[Dict[str, Any]]:
        """Return a fresh async iterator for downstream streaming."""
        return self._event_iterator()

    def __aiter__(self) -> AsyncIterator[Dict[str, Any]]:  # pragma: no cover - trivial
        return self.iter_events()

    @property
    def total_tokens(self) -> int:
        """Get total tokens used across all LLM calls in this turn."""
        if not self.usage:
            return 0
        total = 0
        for entry in self.usage:
            # Unwrap the metadata envelope transparently so callers
            # continue to see a single integer regardless of whether the
            # handler emitted plain ``UsageData`` or the new envelope.
            usage_data = getattr(entry, "usage", entry)
            total += getattr(usage_data, "total_tokens", 0) or 0
        return total


__all__ = [
    "AgentMode",
    "ChatInputs",
    "ExecutionMode",
    "LangGraphChatResult",
    "LangGraphRuntimeConfig",
    "PersistenceContext",
    "RuntimeWarmupStatus",
    "runtime_warmup_status_from_steps",
    "ToolingContext",
]
