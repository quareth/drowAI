"""Transport schemas and pure response mappers for chat router endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MAX_MESSAGE_LEN = 4000
CHAT_HISTORY_CONTRACT_VERSION = "2026-03-01.chat-history.v2"


class ChatDeploymentRef(BaseModel):
    deployment_id: str
    expected_revision: int


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    deployment_ref: Optional[ChatDeploymentRef] = None
    stream: Optional[bool] = True
    mode: Optional[str] = None
    reasoning_effort: Optional[str] = None
    deterministic: Optional[bool] = None
    agent_mode: Optional[str] = None
    plan_mode: Optional[bool] = None
    client_message_id: Optional[str] = None


class ChatPrewarmResponse(BaseModel):
    task_id: int
    conversation_id: str
    checkpointer_ready: bool
    tool_catalog_ready: bool = False
    pty_session_ready: bool = False
    runtime_warm: bool = False
    pty_warmup_required: bool = False


class ChatCancelRequest(BaseModel):
    turn_id: Optional[str] = None
    reason: Optional[str] = None


class ChatReadyResponse(BaseModel):
    task_id: int
    conversation_id: Optional[str]
    checkpointer_ready: bool
    tool_catalog_ready: bool = False
    pty_session_ready: bool = False
    runtime_warm: bool = False
    pty_warmup_required: bool = False
    task_running: bool
    sse_connected: bool
    chat_ready: bool


class ChatHistoryStartupPayload(BaseModel):
    """Startup readiness metadata for initial chat history hydration."""

    task_id: int
    conversation_id: Optional[str]
    checkpointer_ready: bool
    tool_catalog_ready: bool = False
    pty_session_ready: bool = False
    runtime_warm: bool = False
    pty_warmup_required: bool = False
    task_running: bool
    sse_connected: bool
    chat_ready: bool


class ChatTranscriptItem(BaseModel):
    """Compact transcript item contract for chat history startup and paging."""

    id: str
    kind: Literal["user", "assistant", "reasoning", "tool", "observation"]
    turn_number: int
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatHistoryResponse(BaseModel):
    """Versioned startup/paging contract for chat transcript history."""

    contractVersion: Literal["2026-03-01.chat-history.v2"] = CHAT_HISTORY_CONTRACT_VERSION
    items: List[ChatTranscriptItem] = Field(default_factory=list)
    nextBeforeTurn: Optional[int] = None
    hasMoreOlder: bool = False
    startup: Optional[ChatHistoryStartupPayload] = None


class ChatContextWindowResponse(BaseModel):
    """Chat-scoped context-window snapshot for one task conversation."""

    task_id: int
    conversation_id: str
    max_tokens: int
    used_tokens: int
    remaining_tokens: int
    ratio: float
    ceiling_reached: bool
    recommended_next_action: Literal["none", "compress"]
    compression_candidate: bool
    turn_sequence: Optional[int]
    revision: int
    snapshot_kind: Literal["measured", "bootstrap_estimate"]


def _to_response_transcript_items(items: List[Any]) -> List[ChatTranscriptItem]:
    """Map transcript service items into response-model transcript items."""
    return [
        ChatTranscriptItem(
            id=item.id,
            kind=item.kind,
            turn_number=item.turn_number,
            content=item.content,
            metadata=item.metadata,
        )
        for item in items
    ]


def _build_chat_history_response(
    *,
    items: List[Any],
    has_more_older: bool,
    next_before_turn: Optional[int],
    startup: Optional[ChatHistoryStartupPayload],
) -> ChatHistoryResponse:
    """Construct the stable `/chat/history` response envelope."""
    return ChatHistoryResponse(
        contractVersion=CHAT_HISTORY_CONTRACT_VERSION,
        items=_to_response_transcript_items(items),
        nextBeforeTurn=next_before_turn,
        hasMoreOlder=has_more_older,
        startup=startup,
    )


try:
    ChatRequest.model_rebuild()
except Exception:
    pass


__all__ = [
    "CHAT_HISTORY_CONTRACT_VERSION",
    "MAX_MESSAGE_LEN",
    "ChatCancelRequest",
    "ChatContextWindowResponse",
    "ChatHistoryResponse",
    "ChatHistoryStartupPayload",
    "ChatPrewarmResponse",
    "ChatReadyResponse",
    "ChatRequest",
    "ChatTranscriptItem",
    "_build_chat_history_response",
    "_to_response_transcript_items",
]
