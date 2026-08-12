"""LLM decision boundary for coordinating an already-running shell session.

This module owns the bounded prompt, structured response contract, fallback
action, and normalization for choosing input, waiting, or interruption.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from agent.providers.llm.core.base import StructuredOutputSpec
from core.llm import ROLE_REASONING_MAIN
from runtime_shared.shell_session_contracts import ShellInteractionAction

from ..state import InteractiveState
from ..utils.llm_resolver import resolve_llm_client
from .tool_execution_session_state import read_shell_interaction_transcript

logger = logging.getLogger(__name__)

_INTERACTION_DECISION_STRUCTURED_OUTPUT = StructuredOutputSpec(
    name="shell_interaction_decision",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [action.value for action in ShellInteractionAction],
            },
            "chars": {"type": ["string", "null"]},
            "reasoning": {"type": "string"},
        },
        "required": ["action", "chars", "reasoning"],
        "additionalProperties": False,
    },
    strict=True,
)


async def call_shell_interaction_decision(
    *,
    interactive: InteractiveState,
    metadata: Mapping[str, Any],
    session: Mapping[str, Any],
    active: Mapping[str, Any],
    context: Any,
    config: Mapping[str, Any] | None,
    decide_fn: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    """Choose one semantic continuation action for the live session."""

    if decide_fn is not None:
        result = decide_fn(
            interactive=interactive,
            metadata=metadata,
            session=session,
            active=active,
            transcript=read_shell_interaction_transcript(str(session["sequence_id"])),
            context=context,
        )
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, Mapping) else {}

    transcript = read_shell_interaction_transcript(str(session["sequence_id"]))
    if transcript is None:
        return {"action": ShellInteractionAction.WAIT_FOR_OUTPUT.value}
    try:
        llm_client = resolve_llm_client(
            dict(metadata),
            context,
            config=config,
            role=ROLE_REASONING_MAIN,
        )
        response = await llm_client.chat_with_usage(
            _system_prompt(),
            _user_prompt(
                interactive=interactive,
                session=session,
                active=active,
                transcript=transcript,
            ),
            structured_output=_INTERACTION_DECISION_STRUCTURED_OUTPUT,
            temperature=0.1,
            max_tokens=500,
        )
    except Exception as exc:
        logger.warning(
            "Shell interaction decision model unavailable; "
            "interrupting live session: %s",
            exc,
        )
        return {
            "action": ShellInteractionAction.INTERRUPT.value,
            "coordination_failed": True,
        }
    payload = getattr(response, "structured_output", None)
    return payload if isinstance(payload, Mapping) else {}


def normalize_shell_interaction_decision(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize model or injected decisions to the supported action contract."""

    action = str(raw.get("action") or "").strip().lower()
    if action not in {item.value for item in ShellInteractionAction}:
        action = ShellInteractionAction.WAIT_FOR_OUTPUT.value
    chars = str(raw.get("chars") or raw.get("input") or "")
    if action == ShellInteractionAction.SEND_INPUT.value and not chars:
        return {"action": ShellInteractionAction.WAIT_FOR_OUTPUT.value}
    return {
        "action": action,
        "chars": chars,
        "coordination_failed": bool(raw.get("coordination_failed")),
    }


def _system_prompt() -> str:
    return (
        "You choose one semantic action for an already-running shell session.\n"
        "Valid actions are send_input, wait_for_output, and interrupt.\n"
        "Use send_input only when explicit non-empty characters should be sent "
        "to the existing session. Use wait_for_output when the program may "
        "continue producing autonomous output. Use interrupt only when controlled "
        "termination is the right next action. Never use empty input for polling."
    )


def _user_prompt(
    *,
    interactive: InteractiveState,
    session: Mapping[str, Any],
    active: Mapping[str, Any],
    transcript: Mapping[str, Any],
) -> str:
    payload = {
        "user_goal": interactive.facts.current_goal or interactive.facts.message,
        "message": interactive.facts.message,
        "originating_tool_id": session.get("originating_tool_id"),
        "session_id": active.get("session_id"),
        "process_status": active.get("process_status"),
        "stdin_available": bool(active.get("stdin_available")),
        "valid_actions": [action.value for action in ShellInteractionAction],
        "transcript": transcript,
        "output_contract": {
            "send_input": "Set action=send_input and chars to exact non-empty input.",
            "wait_for_output": "Set action=wait_for_output and chars=null.",
            "interrupt": "Set action=interrupt and chars=null.",
        },
    }
    return json.dumps(payload, sort_keys=True)
