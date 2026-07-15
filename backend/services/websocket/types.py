"""Typed WebSocket control payload shapes used by `/ws` channel handlers.

Scope:
- Static typing contracts for agent-multi subscribe/unsubscribe requests.
- Static typing contracts for control responses sent to clients.

Boundary:
- No runtime validation, authentication, routing, or websocket I/O logic.
- No ownership checks or channel business rules.
"""

from typing import Literal, NotRequired, TypedDict


class AgentMultiSubscribeRequest(TypedDict):
    """Client -> server subscribe request for multiplex reasoning stream."""

    action: Literal["subscribe"]
    channel: Literal["agent"]
    taskId: int
    last_seen_sequence: NotRequired[int]


class AgentMultiUnsubscribeRequest(TypedDict):
    """Client -> server unsubscribe request for multiplex reasoning stream."""

    action: Literal["unsubscribe"]
    channel: Literal["agent"]
    taskId: int


class AgentMultiControlResponse(TypedDict):
    """Server -> client control or diagnostic response envelope."""

    type: Literal["subscribed", "unsubscribed", "error"]
    taskId: NotRequired[int]
    message: NotRequired[str]
