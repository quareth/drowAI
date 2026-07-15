"""Chat-domain services: messages, transcripts, turns, observations, and event builders."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ChatMessageService",
    "ChatTranscriptItem",
    "ChatTranscriptPage",
    "ChatTranscriptQueryService",
    "ChatTurnEventService",
    "ChatTurnOrchestrator",
    "ConversationHistoryReader",
    "SYSTEM_SUMMARY_MESSAGE_TYPE",
    "ToolCallRepository",
    "TranscriptCursor",
    "TurnNumberService",
    "attach_conversation_ids",
    "build_assistant_final_event",
    "build_interrupt_event",
    "build_turn_boundary_completion_events",
    "build_user_message_event",
    "get_turn_number_service",
    "merge_observation_tokens",
    "parse_observation_sections",
    "reset_turn_number_service",
    "resolve_turn_identity_from_reserved_message",
    "resolve_turn_identity_from_reserved_message_best_effort",
]

_EXPORTS = {
    "ChatMessageService": ("message_service", "ChatMessageService"),
    "ChatTranscriptItem": ("transcript_query_service", "ChatTranscriptItem"),
    "ChatTranscriptPage": ("transcript_query_service", "ChatTranscriptPage"),
    "ChatTranscriptQueryService": ("transcript_query_service", "ChatTranscriptQueryService"),
    "ChatTurnEventService": ("turn_event_service", "ChatTurnEventService"),
    "ChatTurnOrchestrator": ("turn_orchestrator", "ChatTurnOrchestrator"),
    "ConversationHistoryReader": ("conversation_history_reader", "ConversationHistoryReader"),
    "SYSTEM_SUMMARY_MESSAGE_TYPE": ("conversation_history_reader", "SYSTEM_SUMMARY_MESSAGE_TYPE"),
    "ToolCallRepository": ("tool_call_repository", "ToolCallRepository"),
    "TranscriptCursor": ("transcript_query_service", "TranscriptCursor"),
    "TurnNumberService": ("turn_number_service", "TurnNumberService"),
    "attach_conversation_ids": ("event_builders", "attach_conversation_ids"),
    "build_assistant_final_event": ("event_builders", "build_assistant_final_event"),
    "build_interrupt_event": ("event_builders", "build_interrupt_event"),
    "build_turn_boundary_completion_events": (
        "event_builders",
        "build_turn_boundary_completion_events",
    ),
    "build_user_message_event": ("event_builders", "build_user_message_event"),
    "get_turn_number_service": ("turn_number_service", "get_turn_number_service"),
    "merge_observation_tokens": ("observation_sections", "merge_observation_tokens"),
    "parse_observation_sections": ("observation_sections", "parse_observation_sections"),
    "reset_turn_number_service": ("turn_number_service", "reset_turn_number_service"),
    "resolve_turn_identity_from_reserved_message": (
        "turn_identity_resolver",
        "resolve_turn_identity_from_reserved_message",
    ),
    "resolve_turn_identity_from_reserved_message_best_effort": (
        "turn_identity_resolver",
        "resolve_turn_identity_from_reserved_message_best_effort",
    ),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(f"{__name__}.{module_name}")
    return getattr(module, attr_name)
