"""Helper functions for Human-in-the-Loop interrupt handling."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from langgraph.types import interrupt

from backend.config import ENABLE_HITL_INTERRUPTS

logger = logging.getLogger(__name__)


def build_interrupt_id() -> str:
    """Return a stable identifier for a single interrupt instance."""
    return f"intr-{uuid.uuid4().hex}"


def _extract_turn_context(metadata: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[str], Optional[int]]:
    if not metadata:
        return None, None, None
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        turn_sequence = None
    turn_id = metadata.get("turn_id")
    if not isinstance(turn_id, str):
        turn_id = None
    reserved_message_id = metadata.get("reserved_message_id")
    if not isinstance(reserved_message_id, int):
        reserved_message_id = None
    return turn_sequence, turn_id, reserved_message_id


def should_require_approval(metadata: Dict[str, Any]) -> bool:
    """Check if current execution mode requires tool approval.

    Phase 6 invariant: tool approval keys off ``agent_mode`` only — it
    never looks at ``plan_mode`` or ``execution_route_policy``. Plan is
    a route overlay that stacks on top of ``agent`` / ``full_access``;
    the primary mode alone decides autonomy / HITL behavior.

    Behavioral table:

    - ``agent_mode=full_access``                -> no approval (False)
    - ``agent_mode=full_access`` + ``plan_mode=True`` -> no approval (False)
    - ``agent_mode=agent``                      -> approval required
    - ``agent_mode=agent`` + ``plan_mode=True``  -> approval required
    - ``agent_mode=plan`` (legacy, pre-normalization) -> approval required
    - ``agent_mode=chat``                       -> no approval (no tools)

    Args:
        metadata: Facts metadata containing agent_mode.

    Returns:
        True if approval is required before tool execution.
    """
    if not ENABLE_HITL_INTERRUPTS:
        logger.debug("[HITL] ENABLE_HITL_INTERRUPTS is False, skipping approval")
        return False

    agent_mode = metadata.get("agent_mode", "full_access")
    # ``plan_mode`` is read only for logging / audit here — the decision
    # itself intentionally ignores it so the route overlay cannot change
    # autonomy semantics (single-authority: agent_mode).
    plan_mode = bool(metadata.get("plan_mode", False))
    requires_approval = agent_mode in ("agent", "plan")
    logger.info(
        "[HITL] should_require_approval: agent_mode=%s, plan_mode=%s, "
        "requires_approval=%s, ENABLE_HITL_INTERRUPTS=%s, metadata_keys=%s",
        agent_mode,
        plan_mode,
        requires_approval,
        ENABLE_HITL_INTERRUPTS,
        list(metadata.keys())[:20],
    )
    return requires_approval


def should_require_plan_approval(metadata: Dict[str, Any]) -> bool:
    """Check if this turn requires a user-visible plan review interrupt.

    Plan review is controlled by explicit profile metadata, not by Deep
    Reasoning graph identity. During migration, older checkpoints that do
    not carry ``plan_review_required`` fall back to ``plan_mode``.

    Args:
        metadata: Facts metadata (may contain agent_mode for full_access check).

    Returns:
        True if plan approval is required before plan execution.
    """
    if not ENABLE_HITL_INTERRUPTS:
        logger.debug("[HITL] ENABLE_HITL_INTERRUPTS is False, skipping plan approval")
        return False

    if "plan_review_required" in metadata:
        requires_plan_approval = metadata.get("plan_review_required") is True
    else:
        requires_plan_approval = bool(metadata.get("plan_mode"))

    logger.info(
        "[HITL] should_require_plan_approval: plan_review_required=%s, "
        "plan_mode=%s, requires_plan_approval=%s, metadata_keys=%s",
        metadata.get("plan_review_required"),
        metadata.get("plan_mode"),
        requires_plan_approval,
        list(metadata.keys())[:20],
    )
    return requires_plan_approval


def build_plan_review_payload(
    *,
    goal: str,
    plan_steps: List[str],
    todo_list: List[str],
    reasoning: Optional[str] = None,
    targets: Optional[List[str]] = None,
    run_id: Optional[int] = None,
    plan_version: Optional[int] = None,
    turn_sequence: Optional[int] = None,
    turn_id: Optional[str] = None,
    reserved_message_id: Optional[int] = None,
    interrupt_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build payload for plan review interrupt.

    Args:
        goal: Primary objective / first goal.
        plan_steps: Ordered list of plan steps.
        todo_list: List of todo item strings (converted to TodoItemPayload).
        reasoning: LLM's reasoning for this plan.
        targets: Target IPs/hostnames.
        run_id: Turn sequence for multi-run tracking.

    Returns:
        Dict ready to pass to interrupt().
    """
    todo_items = [
        {"id": str(uuid.uuid4())[:8], "text": text, "status": "pending"}
        for text in todo_list
    ]

    payload: Dict[str, Any] = {
        "type": "plan_review",
        "interrupt_id": interrupt_id or build_interrupt_id(),
        "goal": goal,
        "plan_steps": plan_steps,
        "todo_list": todo_items,
        "reasoning": reasoning,
        "targets": targets or [],
        "run_id": run_id,
        "plan_version": plan_version,
    }
    if turn_sequence is not None:
        payload["turn_sequence"] = turn_sequence
    if turn_id:
        payload["turn_id"] = turn_id
    if reserved_message_id is not None:
        payload["reserved_message_id"] = reserved_message_id
    return payload


def request_plan_approval(
    *,
    goal: str,
    plan_steps: List[str],
    todo_list: List[str],
    reasoning: Optional[str] = None,
    targets: Optional[List[str]] = None,
    run_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    turn_sequence: Optional[int] = None,
    turn_id: Optional[str] = None,
    reserved_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Request user approval for execution plan.

    Calls interrupt() with plan review payload. When user responds,
    this function returns with their response.

    Args:
        goal: Primary objective.
        plan_steps: Ordered plan steps.
        todo_list: Todo items.
        reasoning: LLM reasoning.
        targets: Target hosts.
        run_id: Turn sequence.
        payload: Pre-built payload for the interrupt, if already constructed.

    Returns:
        User response dict with action and optional edited fields.
    """
    if payload is None:
        meta_sequence, meta_turn_id, meta_reserved_id = _extract_turn_context(metadata)
        payload = build_plan_review_payload(
            goal=goal,
            plan_steps=plan_steps,
            todo_list=todo_list,
            reasoning=reasoning,
            targets=targets,
            run_id=run_id,
            turn_sequence=turn_sequence or meta_sequence,
            turn_id=turn_id or meta_turn_id,
            reserved_message_id=reserved_message_id or meta_reserved_id,
        )
    else:
        payload = dict(payload)
        candidate_id = payload.get("interrupt_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            payload["interrupt_id"] = build_interrupt_id()

    logger.info(
        "[HITL] Requesting plan approval: %d steps, %d todos",
        len(plan_steps),
        len(todo_list),
    )
    user_response = interrupt(payload)
    action = user_response.get("action", "unknown") if isinstance(user_response, dict) else "unknown"
    logger.info("[HITL] Received plan response: %s", action)
    return user_response if isinstance(user_response, dict) else {"action": "approve"}


def build_tool_approval_payload(
    *,
    tool_id: str,
    tool_name: str,
    parameters: Dict[str, Any],
    description: Optional[str] = None,
    risk_level: Optional[str] = None,
    turn_sequence: Optional[int] = None,
    turn_id: Optional[str] = None,
    reserved_message_id: Optional[int] = None,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build payload for tool approval interrupt.

    Phase 7 Task 7.1: the payload now carries an ``items`` list so multi-
    call batches can present every committed call in one approval surface.
    Single-call callers may still pass the legacy ``tool_id`` / ``tool_name``
    / ``parameters`` keyword args; the helper synthesizes a one-element
    ``items`` list and populates the legacy top-level fields from
    ``items[0]`` for the in-flight frontend that hasn't picked up the new
    shape yet.

    Args:
        tool_id: Tool identifier (legacy single-tool field).
        tool_name: Human-readable name (legacy single-tool field).
        parameters: Tool parameters to be approved (legacy single-tool field).
        description: What the tool will do.
        risk_level: Risk assessment (low/medium/high).
        tool_call_id: Stable id for the single call (when known).
        tool_batch_id: Stable id for the batch the call belongs to.
        items: Pre-built list of approval items (multi-call path). Each
            item is a dict with ``tool_id``, ``tool_name``, ``parameters``,
            and optional ``tool_call_id``/``description``/``risk_level``.

    Returns:
        Dict ready to pass to interrupt().
    """
    if items is None or not items:
        items = [
            {
                "tool_call_id": tool_call_id or "",
                "tool_id": tool_id,
                "tool_name": tool_name,
                "parameters": parameters,
                "description": description or f"Execute {tool_name}",
                "risk_level": risk_level,
            }
        ]
    primary = items[0]
    payload: Dict[str, Any] = {
        "type": "tool_approval",
        "interrupt_id": interrupt_id or build_interrupt_id(),
        # Legacy single-tool fields populated from items[0] for the
        # migration window — frontend that hasn't picked up `items` yet
        # still renders the first call.
        "tool_id": primary.get("tool_id", tool_id),
        "tool_name": primary.get("tool_name", tool_name),
        "parameters": primary.get("parameters", parameters),
        "description": primary.get("description") or f"Execute {primary.get('tool_name', tool_name)}",
        "risk_level": primary.get("risk_level", risk_level),
        # Phase 7 batch-aware fields.
        "items": list(items),
        "tool_batch_id": tool_batch_id or "",
    }
    if turn_sequence is not None:
        payload["turn_sequence"] = turn_sequence
    if turn_id:
        payload["turn_id"] = turn_id
    if reserved_message_id is not None:
        payload["reserved_message_id"] = reserved_message_id
    return payload


def request_tool_approval(
    *,
    tool_id: str,
    tool_name: str,
    parameters: Dict[str, Any],
    description: Optional[str] = None,
    risk_level: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    turn_sequence: Optional[int] = None,
    turn_id: Optional[str] = None,
    reserved_message_id: Optional[int] = None,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Request user approval for tool execution.

    Calls interrupt() with tool approval payload. When user responds,
    this function returns with their response.

    Args:
        tool_id: Tool identifier.
        tool_name: Human-readable name.
        parameters: Tool parameters.
        description: What the tool will do.
        risk_level: Risk assessment (low/medium/high).

    Returns:
        User response dict with action and optional edited_parameters.
    """
    meta_sequence, meta_turn_id, meta_reserved_id = _extract_turn_context(metadata)
    payload = build_tool_approval_payload(
        tool_id=tool_id,
        tool_name=tool_name,
        parameters=parameters,
        description=description,
        risk_level=risk_level,
        turn_sequence=turn_sequence or meta_sequence,
        turn_id=turn_id or meta_turn_id,
        reserved_message_id=reserved_message_id or meta_reserved_id,
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        items=items,
    )

    logger.info("[HITL] Requesting approval for %s", tool_id)
    user_response = interrupt(payload)
    action = user_response.get("action", "unknown") if isinstance(user_response, dict) else "unknown"
    logger.info("[HITL] Received response: %s", action)
    return user_response if isinstance(user_response, dict) else {"action": "approve"}


def normalize_tool_approval_response(response: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize tool approval responses into a safe, typed action payload."""
    if not isinstance(response, dict):
        return {"action": "approve"}
    normalized = dict(response)
    action = normalized.get("action")
    if not isinstance(action, str):
        action = "approve"
    action = action.strip().lower() or "approve"
    if action not in {"approve", "skip", "edit"}:
        action = "approve"
    normalized["action"] = action
    if action == "edit" and not isinstance(normalized.get("edited_parameters"), dict):
        normalized["edited_parameters"] = {}
    return normalized


def normalize_required_blockers(
    required_blockers: Any,
    *,
    max_questions: int = 2,
) -> List[Dict[str, Any]]:
    """Normalize required blocker payloads for clarify flow consumers."""
    if not isinstance(required_blockers, list):
        return []

    normalized: List[Dict[str, Any]] = []
    seen_slots: set[str] = set()
    for blocker in required_blockers:
        if len(normalized) >= max(0, max_questions):
            break
        if not isinstance(blocker, dict):
            continue

        slot = str(blocker.get("slot") or "").strip()
        question = str(blocker.get("question") or "").strip()
        if not slot or not question or slot in seen_slots:
            continue

        input_type = str(blocker.get("input_type") or "").strip().lower()
        if input_type != "select":
            continue

        raw_options = blocker.get("options")
        if not isinstance(raw_options, list):
            continue

        options: List[str] = []
        seen_options: set[str] = set()
        invalid_options = False
        for option in raw_options:
            value = str(option).strip()
            if not value or value in seen_options:
                invalid_options = True
                break
            seen_options.add(value)
            options.append(value)

        if invalid_options or not options or len(options) > 4:
            continue

        normalized.append(
            {
                "slot": slot,
                "question": question,
                "input_type": "select",
                "options": options,
            }
        )
        seen_slots.add(slot)
    return normalized


def build_clarify_request_payload(
    *,
    required_blockers: List[Dict[str, Any]],
    context_metadata: Optional[Dict[str, Any]] = None,
    turn_sequence: Optional[int] = None,
    turn_id: Optional[str] = None,
    reserved_message_id: Optional[int] = None,
    interrupt_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build payload for clarify request interrupt."""
    questions: List[Dict[str, Any]] = []
    for blocker in normalize_required_blockers(required_blockers, max_questions=2):
        question_payload: Dict[str, Any] = {
            "question_id": blocker["slot"],
            "input_type": "select",
            "label": blocker["question"],
            "options": blocker.get("options", []),
            "required": True,
        }
        questions.append(question_payload)

    payload: Dict[str, Any] = {
        "type": "clarify_request",
        "interrupt_id": interrupt_id or build_interrupt_id(),
        "questions": questions,
        "context_metadata": context_metadata or {},
    }
    if turn_sequence is not None:
        payload["turn_sequence"] = turn_sequence
    if turn_id:
        payload["turn_id"] = turn_id
    if reserved_message_id is not None:
        payload["reserved_message_id"] = reserved_message_id
    return payload


def request_clarify_answers(
    *,
    required_blockers: List[Dict[str, Any]],
    context_metadata: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    turn_sequence: Optional[int] = None,
    turn_id: Optional[str] = None,
    reserved_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Request user answers for mandatory clarify blockers."""
    if payload is None:
        meta_sequence, meta_turn_id, meta_reserved_id = _extract_turn_context(metadata)
        payload = build_clarify_request_payload(
            required_blockers=required_blockers,
            context_metadata=context_metadata,
            turn_sequence=turn_sequence or meta_sequence,
            turn_id=turn_id or meta_turn_id,
            reserved_message_id=reserved_message_id or meta_reserved_id,
        )
    else:
        payload = dict(payload)
        candidate_id = payload.get("interrupt_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            payload["interrupt_id"] = build_interrupt_id()

    logger.info(
        "[HITL] Requesting clarify answers: %d question(s)",
        len(payload.get("questions", [])) if isinstance(payload.get("questions"), list) else 0,
    )
    user_response = interrupt(payload)
    return normalize_clarify_response(user_response if isinstance(user_response, dict) else None)


def normalize_clarify_response(response: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize clarify responses into a safe, typed answer payload."""
    if not isinstance(response, dict):
        return {"action": "answer", "answers": {}}

    normalized = dict(response)
    action = normalized.get("action")
    if not isinstance(action, str):
        action = "answer"
    action = action.strip().lower() or "answer"
    if action != "answer":
        action = "answer"
    normalized["action"] = action

    answers = normalized.get("answers")
    if not isinstance(answers, dict):
        normalized["answers"] = {}
    else:
        normalized["answers"] = {
            str(key): str(value)
            for key, value in answers.items()
            if str(key).strip()
        }
    return normalized


__all__ = [
    "build_interrupt_id",
    "build_clarify_request_payload",
    "build_plan_review_payload",
    "build_tool_approval_payload",
    "normalize_clarify_response",
    "normalize_required_blockers",
    "normalize_tool_approval_response",
    "request_clarify_answers",
    "request_plan_approval",
    "request_tool_approval",
    "should_require_plan_approval",
    "should_require_approval",
]
