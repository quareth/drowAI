"""Centralized pre-execution gate decisions for executor flows.

Purpose:
- Provide one owner for scope-validation and interactive-approval gate behavior.

Owns:
- Scope validation evaluation and block-result construction.
- Approval request flow using provider-first logic with ProposalManager fallback.
- Cohesive pre-execution gate composition API for action-level and tool-level flows.
- Fail-open/fail-safe handling semantics for gate infrastructure failures.

Does not own:
- Execution transport routing and fallback policy.
- Tool-specific validation, command synthesis, or result aggregation.
- Workspace path translation and filesystem safety internals.

Invariants:
- Preserve existing rejection payloads, logging messages, and return contracts.
- Preserve security boundaries by enforcing scope checks exactly where invoked.
- Preserve zero-behavior-change semantics for provider and interactive fallback flow.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

try:
    from ..models import ExecutionResult
except ImportError:  # pragma: no cover
    from models import ExecutionResult


def evaluate_scope_gate(
    *,
    scope_validator: Any,
    command: str,
    target: str,
    logger: Any = None,
) -> Optional[ExecutionResult]:
    """Evaluate scope validation and return a blocking ExecutionResult when denied."""
    if scope_validator is None:
        return None

    validation = scope_validator.validate_proposed_action(command, target)
    if validation.is_valid:
        return None

    err = "; ".join(validation.errors)
    if logger:
        logger.log_operation("ERROR", f"Action blocked: {err}")
    return ExecutionResult(False, "", f"Blocked by scope validator: {err}", -1)


async def request_approval_gate(
    *,
    tool_name: str,
    parameters: Dict[str, Any],
    reasoning: Optional[str],
    user_interaction_provider: Any = None,
    last_action: Any = None,
    proposal_manager_cls: Any = None,
    workspace: Optional[str] = None,
    logger: Any = None,
) -> bool:
    """Evaluate approval gate with provider-first and ProposalManager fallback logic."""
    if user_interaction_provider is not None:
        if last_action is not None:
            try:
                return await user_interaction_provider.request_user_approval(last_action)
            except Exception as exc:  # pragma: no cover - defensive
                if logger:
                    logger.log_operation("WARNING", f"Interactive approval provider failed, continuing: {exc}")
                return True

    try:
        if os.getenv("AGENT_MODE", "automatic").lower() != "interactive":
            return True
        if proposal_manager_cls is None:
            # Failsafe: if manager not available, do not block execution
            if logger:
                logger.log_operation("WARNING", "Interactive mode requested but ProposalManager unavailable; continuing")
            return True

        manager_workspace = workspace or os.getenv("WORKSPACE", "/workspace")
        mgr = proposal_manager_cls(manager_workspace)

        # Ensure only one pending at a time; reuse if present
        pending = mgr.store.get_pending()
        if pending is not None:
            prop = pending
        else:
            # Emit SSE proposal step
            content = f"Requesting approval to run {tool_name}"
            meta = {
                "tool": tool_name,
                "parameters": parameters,
                "reasoning": reasoning or "",
                "status": "pending",
            }
            if logger:
                logger.log_reasoning_step("proposal", content, meta)
            prop = mgr.create_proposal(tool_name, parameters, reasoning or "")

        # Wait for approval
        status = await mgr.wait_for_approval(prop.id, poll_interval=1.0)
        if logger:
            logger.log_operation("INFO", f"Proposal {prop.id} resolved with status={status}")
        return status == "approved"
    except Exception as exc:
        # Defensive: never block execution due to gate failure
        if logger:
            logger.log_operation("WARNING", f"Interactive gate failed, continuing: {exc}")
        return True


async def evaluate_execution_gates(
    *,
    scope_validator: Any = None,
    command: str = "",
    target: str = "",
    tool_name: str = "",
    parameters: Optional[Dict[str, Any]] = None,
    reasoning: Optional[str] = None,
    user_interaction_provider: Any = None,
    last_action: Any = None,
    proposal_manager_cls: Any = None,
    workspace: Optional[str] = None,
    logger: Any = None,
    check_scope: bool = False,
    check_approval: bool = False,
) -> Tuple[Optional[ExecutionResult], bool]:
    """Evaluate centralized pre-execution gates and return `(block_result, approved)`."""
    if check_scope:
        blocked = evaluate_scope_gate(
            scope_validator=scope_validator,
            command=command,
            target=target,
            logger=logger,
        )
        if blocked is not None:
            return blocked, False

    if check_approval:
        approved = await request_approval_gate(
            tool_name=tool_name,
            parameters=parameters or {},
            reasoning=reasoning,
            user_interaction_provider=user_interaction_provider,
            last_action=last_action,
            proposal_manager_cls=proposal_manager_cls,
            workspace=workspace,
            logger=logger,
        )
        return None, approved

    return None, True
