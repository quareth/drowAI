"""LangGraph node that retrieves long-term memory context for the current turn.

This module is intentionally self-contained and fault-tolerant: it resolves a
query from working memory, performs semantic retrieval against memory tiers, and
stores a bounded summary in `facts.metadata["long_term_memory_summary"]`.
Any retrieval failure degrades gracefully to a no-op update.

Parked groundwork: the summary key is written here but is not consumed by any
LLM call in the current hot path. A future Option A plan may wire it into
prompts; do not remove this node without that follow-up plan.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

from core.memory.retrieval_summary import (
    render_memory_summary as _render_memory_summary,
    split_retrieval_limits as _split_retrieval_limits,
)
from ..infrastructure.state_models import GraphRuntimeContext
from ..memory.target_resolution import resolve_target_from_working_memory
from ..state import InteractiveState

logger = logging.getLogger(__name__)


def _read_int_env(name: str, default: int, *, min_value: int) -> int:
    """Read int env var safely and clamp to a minimum value."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(min_value, value)


MEMORY_RETRIEVAL_SUMMARY_MAX_CHARS = _read_int_env(
    "MEMORY_RETRIEVAL_SUMMARY_MAX_CHARS",
    1500,
    min_value=1,
)
MEMORY_RETRIEVAL_MAX_RESULTS = _read_int_env(
    "MEMORY_RETRIEVAL_MAX_RESULTS",
    5,
    min_value=1,
)


def _clear_long_term_memory_summary(interactive: InteractiveState) -> dict:
    """Clear long-term memory summary in metadata and return a state update."""
    metadata = interactive.facts.metadata_copy()
    metadata["long_term_memory_summary"] = ""
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


def _resolve_memory_runtime_service(config: Optional[Mapping[str, Any]]) -> Any | None:
    """Return the backend memory runtime service from invocation config."""
    configurable = _configurable(config)
    runtime_services = configurable.get("runtime_services")
    return getattr(runtime_services, "memory_runtime_service", None)


def _configurable(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    configurable = config.get("configurable")
    return configurable if isinstance(configurable, Mapping) else {}


def _resolve_runtime_user_id(configurable: Mapping[str, Any]) -> Optional[int]:
    runtime_projection = configurable.get("runtime_projection")
    if isinstance(runtime_projection, Mapping):
        return _coerce_positive_int(runtime_projection.get("user_id"))
    return None


def _coerce_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _extract_query_from_working_memory(metadata: dict) -> str:
    wm = metadata.get("working_memory")
    if not isinstance(wm, dict):
        return ""

    parts: list[str] = []

    # Reuse canonical target resolver to avoid duplicate extraction heuristics.
    target = resolve_target_from_working_memory(wm)
    if target:
        parts.append(target)

    objective = wm.get("objective")
    if isinstance(objective, dict):
        objective_text = str(objective.get("text", "")).strip()
        if objective_text and objective_text.lower() != "unknown":
            parts.append(objective_text)
    elif isinstance(objective, str):
        objective_text = objective.strip()
        if objective_text and objective_text.lower() != "unknown":
            parts.append(objective_text)

    return " ".join(part for part in parts if part)


async def memory_retrieval_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> dict:
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.metadata_copy()

    metadata_user_id = _coerce_positive_int(metadata.get("user_id"))
    context_user_id = _coerce_positive_int(getattr(context, "user_id", None))

    query = _extract_query_from_working_memory(metadata)
    if not query:
        return _clear_long_term_memory_summary(interactive)

    user_profile_max, engagement_max = _split_retrieval_limits(MEMORY_RETRIEVAL_MAX_RESULTS)
    if user_profile_max <= 0 and engagement_max <= 0:
        return _clear_long_term_memory_summary(interactive)

    max_chars = MEMORY_RETRIEVAL_SUMMARY_MAX_CHARS
    try:
        configurable = _configurable(config)
        memory_runtime_service = _resolve_memory_runtime_service(config)
        selection = configurable.get("llm_runtime_selection")
        runtime_user_id = _resolve_runtime_user_id(configurable)
        if (
            memory_runtime_service is None
            or not isinstance(selection, Mapping)
            or runtime_user_id is None
        ):
            return _clear_long_term_memory_summary(interactive)
        if metadata_user_id is not None and metadata_user_id != runtime_user_id:
            logger.warning("[MEMORY_RETRIEVAL] Metadata user id does not match runtime user id")
            return _clear_long_term_memory_summary(interactive)
        if context_user_id is not None and context_user_id != runtime_user_id:
            logger.warning("[MEMORY_RETRIEVAL] Context user id does not match runtime user id")
            return _clear_long_term_memory_summary(interactive)

        task_id = interactive.facts.task_id
        if context is not None and context.task_id:
            task_id = context.task_id

        summary = await memory_runtime_service.retrieve_summary(
            selection=selection,
            runtime_user_id=int(runtime_user_id),
            task_id=int(task_id) if task_id is not None else None,
            user_id=int(runtime_user_id),
            query=query,
            max_results=MEMORY_RETRIEVAL_MAX_RESULTS,
            max_chars=max_chars,
        )
        if not summary:
            return _clear_long_term_memory_summary(interactive)
        metadata["long_term_memory_summary"] = summary
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    except Exception:
        logger.warning("[MEMORY_RETRIEVAL] Failed, continuing without memory", exc_info=True)
        return _clear_long_term_memory_summary(interactive)


__all__ = [
    "memory_retrieval_node",
    "_extract_query_from_working_memory",
    "_split_retrieval_limits",
    "_render_memory_summary",
    "_clear_long_term_memory_summary",
    "_resolve_memory_runtime_service",
]
