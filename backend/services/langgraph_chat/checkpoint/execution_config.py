"""Build canonical LangGraph run config for resume/retry checkpoint continuation.

Owns the construction of the ``configurable`` dict passed to ``graph.astream``
when continuing from a stored checkpoint (HITL resume + checkpoint retry).
Includes the warm/cold runtime path probe and the HITL timing log helper used
inline during config assembly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from backend.services.llm_provider.runtime_services import attach_runtime_services
from backend.services.langgraph_chat.contracts import runtime_warmup_status_from_steps
from backend.services.langgraph_chat.hitl_constants import GRAPH_RECURSION_LIMIT
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id

logger = logging.getLogger("backend.services.langgraph_chat.facade")


def log_hitl_stage(
    *,
    stage: str,
    timestamp: Optional[float],
    task_id: int,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    """Emit HITL timing stages using a consistent log schema.

    Args:
        stage: Timing stage label.
        timestamp: Captured timestamp, or ``None`` when absent.
        task_id: Task identifier for the resumed/retried run.
        interrupt_id: Optional interrupt identifier.
        tool_call_id: Optional tool call identifier.
    """
    if timestamp is None:
        return
    logger.info(
        "[HITL_TIMING] stage=%s task_id=%s interrupt_id=%s tool_call_id=%s ts=%.9f",
        stage,
        task_id,
        interrupt_id or "unknown",
        tool_call_id or "unknown",
        float(timestamp),
    )


def resolve_resume_runtime_path_label(task_id: int) -> str:
    """Resolve warm/cold runtime path label for resume metric config wiring.

    Args:
        task_id: Task identifier for runtime warmup lookup.

    Returns:
        ``"warm"``, ``"cold"``, or ``"unknown"``.
    """
    try:
        from backend.services.langgraph_chat.runtime.warmup_service import (
            get_shared_runtime_warmup_service,
        )

        raw_status = get_shared_runtime_warmup_service().get_warmup_status(task_id)
    except Exception:
        return "unknown"

    warmup_status = runtime_warmup_status_from_steps(raw_status)
    return "warm" if warmup_status.runtime_warm else "cold"


def build_checkpoint_execution_config(
    *,
    task_id: int,
    graph_name: str,
    graph_thread_id: Optional[str] = None,
    user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    runtime_placement_mode: Optional[str] = None,
    workspace_id: Optional[str] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    runner_id: Optional[str] = None,
    execution_site_id: Optional[str] = None,
    llm_runtime_selection: Optional[Mapping[str, Any]] = None,
    runtime_services: Any = None,
    checkpoint_id: Optional[int | str] = None,
    interrupt_id: Optional[str] = None,
    approval_received_at: Optional[float] = None,
    resume_worker_start_at: Optional[float] = None,
    retry_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build canonical LangGraph config for resume/retry checkpoint continuation.

    ``retry_context`` carries the canonical retry identity (``retry_attempt`` /
    ``retry_max_attempts``) plus the sanitized ``previous_failure`` projection.
    When present, those fields are attached under ``configurable`` so
    graph/prompt runtime modules can read them off the run config and avoid
    blindly replaying a failing step. Resume/HITL config (no
    ``retry_context``) is left untouched.

    Args:
        task_id: Task identifier.
        graph_name: Graph name being resumed or retried.
        checkpoint_id: Optional checkpoint id to pin continuation.
        interrupt_id: Optional interrupt id for HITL logging/config.
        approval_received_at: Optional approval timestamp.
        resume_worker_start_at: Optional worker-start timestamp.
        retry_context: Optional sanitized checkpoint-retry context.

    Returns:
        LangGraph run config dict.
    """
    thread_id = format_graph_thread_id(graph_thread_id, task_id=task_id)
    configurable: Dict[str, Any] = {"thread_id": thread_id, "graph_name": graph_name}
    if isinstance(llm_runtime_selection, Mapping):
        selection_payload = dict(llm_runtime_selection)
        configurable["llm_runtime_selection"] = selection_payload
        runtime_projection: Dict[str, Any] = {
            "task_id": task_id,
            "graph_thread_id": graph_thread_id,
            "provider": selection_payload.get("provider"),
            "model": selection_payload.get("model"),
            "credential_ref": selection_payload.get("credential_ref"),
            "reasoning_effort": selection_payload.get("reasoning_effort"),
        }
        if tenant_id is not None:
            runtime_projection["tenant_id"] = tenant_id
        if user_id is not None:
            runtime_projection["user_id"] = user_id
        if isinstance(runtime_placement_mode, str) and runtime_placement_mode.strip():
            runtime_projection["runtime_placement_mode"] = runtime_placement_mode.strip()
        if isinstance(workspace_id, str) and workspace_id.strip():
            runtime_projection["workspace_id"] = workspace_id.strip()
        if isinstance(actor_type, str) and actor_type.strip():
            runtime_projection["actor_type"] = actor_type.strip()
        if isinstance(actor_id, str) and actor_id.strip():
            runtime_projection["actor_id"] = actor_id.strip()
        if isinstance(runner_id, str) and runner_id.strip():
            runtime_projection["runner_id"] = runner_id.strip()
        if isinstance(execution_site_id, str) and execution_site_id.strip():
            runtime_projection["execution_site_id"] = execution_site_id.strip()
        configurable["runtime_projection"] = runtime_projection
    config: Dict[str, Any] = {"configurable": configurable}
    if runtime_services is not None:
        config = attach_runtime_services(config, runtime_services)
    runtime_path = resolve_resume_runtime_path_label(task_id)
    config["configurable"]["runtime_path"] = runtime_path
    if runtime_path in {"warm", "cold"}:
        config["configurable"]["runtime_warm"] = runtime_path == "warm"
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = str(checkpoint_id)
    if isinstance(interrupt_id, str) and interrupt_id.strip():
        config["configurable"]["interrupt_id"] = interrupt_id.strip()
    if approval_received_at is not None:
        config["configurable"]["approval_received_at"] = approval_received_at
        log_hitl_stage(
            stage="approval_received_at",
            timestamp=approval_received_at,
            task_id=task_id,
            interrupt_id=interrupt_id,
        )
    if resume_worker_start_at is not None:
        config["configurable"]["resume_worker_start_at"] = resume_worker_start_at
        log_hitl_stage(
            stage="resume_worker_start_at",
            timestamp=resume_worker_start_at,
            task_id=task_id,
            interrupt_id=interrupt_id,
        )
    if retry_context:
        retry_attempt = retry_context.get("retry_attempt")
        retry_max_attempts = retry_context.get("retry_max_attempts")
        previous_failure = retry_context.get("previous_failure")
        # Surface retry identity onto the run config so graph/prompt
        # runtime can read it via ``configurable.get("retry_attempt")``,
        # etc. The values are also bundled together as ``retry_context``
        # for callers that want the whole projection in one place.
        if retry_attempt is not None:
            config["configurable"]["retry_attempt"] = retry_attempt
        if retry_max_attempts is not None:
            config["configurable"]["retry_max_attempts"] = retry_max_attempts
        if isinstance(previous_failure, Mapping) and previous_failure:
            config["configurable"]["previous_failure"] = dict(previous_failure)
        config["configurable"]["retry_context"] = {
            key: (dict(value) if isinstance(value, Mapping) else value)
            for key, value in retry_context.items()
            if value is not None
        }
    config.setdefault("recursion_limit", GRAPH_RECURSION_LIMIT)
    return config
