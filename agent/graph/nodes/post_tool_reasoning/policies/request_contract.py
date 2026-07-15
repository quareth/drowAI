"""Request contract terminal policy for short and binary user asks. Overrides PTR output to finalize when the contract determines the request is terminally answerable."""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from ....state import InteractiveState
from ..models import PostToolReasoningOutput

REQUEST_CONTRACT_TERMINAL_KEY = "request_contract_terminal"

logger = logging.getLogger(__name__)


def _normalize_request_contract(raw_contract: Any) -> Dict[str, str]:
    """Normalize request contract into known enum-like values."""
    if not isinstance(raw_contract, Mapping):
        return {}
    contract: Dict[str, str] = {}
    question_type = str(raw_contract.get("question_type") or "").strip().lower()
    answer_style = str(raw_contract.get("answer_style") or "").strip().lower()
    terminal_when = str(raw_contract.get("terminal_when") or "").strip().lower()

    if question_type in {"binary_check", "multi_step", "open_ended"}:
        contract["question_type"] = question_type
    if answer_style in {"short", "normal"}:
        contract["answer_style"] = answer_style
    if terminal_when in {"determined", "all_steps_done"}:
        contract["terminal_when"] = terminal_when
    return contract


def _should_finalize_from_request_contract(
    interactive: InteractiveState,
    output: PostToolReasoningOutput,
    contract: Mapping[str, str],
) -> bool:
    """Return True when contract says this request is terminally answerable."""
    if contract.get("terminal_when") != "determined":
        return False
    if output.failure_detected:
        return False
    if output.user_goal_achieved or output.next_action == "finalize":
        return True

    # Determined should only be forced when the execution state is terminal.
    # A single completed todo can represent partial progress in multi-step flows,
    # and must not auto-finalize while other todos remain pending/in-progress.
    todo_list = interactive.facts.safe_todo_list
    if todo_list:
        all_terminal = True
        for todo in todo_list:
            status = getattr(todo, "status", None)
            status_value = str(getattr(status, "value", status or "")).strip().lower()
            if status_value in {"pending", "in_progress"}:
                all_terminal = False
                break
        if all_terminal:
            return True

    return False


def _apply_request_contract_policy(
    interactive: InteractiveState,
    output: PostToolReasoningOutput,
) -> None:
    """Apply request contract terminal policy for short/binary asks."""
    metadata = interactive.facts.ensure_metadata()
    contract = _normalize_request_contract(metadata.get("request_contract"))
    if not contract:
        metadata.pop(REQUEST_CONTRACT_TERMINAL_KEY, None)
        return

    metadata["request_contract"] = contract
    if not _should_finalize_from_request_contract(interactive, output, contract):
        metadata.pop(REQUEST_CONTRACT_TERMINAL_KEY, None)
        return

    metadata[REQUEST_CONTRACT_TERMINAL_KEY] = True
    if output.next_action != "finalize" or not output.user_goal_achieved:
        logger.info(
            "[POST_TOOL_REASONING] Request contract terminal override applied: "
            "terminal_when=determined"
        )
        output.next_action = "finalize"
        output.user_goal_achieved = True
        output.retry_suggested = False
        output.tool_intent = None
        output.action_reasoning = (
            "(Override: request contract terminal_when=determined) "
            + output.action_reasoning
        )

