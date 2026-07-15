"""Request/context builders extracted from tool-execution facade.

This module centralizes runtime context resolution, workspace fallback, and
ToolExecutionRequest/AgentConfig assembly while preserving existing behavior.

Planner continuity authority
----------------------------
The ``history`` field on the assembled ``ToolExecutionRequest`` is
populated from the shared hot-path ``ConversationContextBundle`` via the
planner projection — the same recent-transcript window every other
prompt-authoritative role observes.

Phase 5 cutover: the bundle is required. When it is missing from
metadata this module raises ``RuntimeError`` — it indicates an
upstream wiring bug rather than a soft compatibility gap.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agent.config import AgentConfig
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.projections import project_for_planner
from agent.tool_runtime import ToolExecutionRequest

from ...infrastructure.state_models import GraphRuntimeContext
from ...state import InteractiveState
from ...utils.llm_resolver import DEFAULT_MODEL

logger = logging.getLogger(__name__)

_RAW_LLM_SECRET_METADATA_KEYS = frozenset(
    {
        "api_key",
        "runtime_api_key",
        "openai_api_key",
    }
)
_RUNTIME_IDENTITY_METADATA_FIELDS = (
    "tenant_id",
    "workspace_id",
    "actor_type",
    "actor_id",
    "runner_id",
    "execution_site_id",
    "user_id",
)


def _normalize_runtime_placement_mode(value: Any) -> str | None:
    """Normalize runtime placement mode metadata to a lowercase token."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _raise_missing_runtime_identity(*, missing_fields: list[str]) -> None:
    """Raise a deterministic fail-closed runtime identity error."""
    missing_text = ", ".join(missing_fields)
    raise RuntimeError(
        "tool_execution_runtime: missing runtime identity field(s): "
        f"{missing_text}. "
        "Required runtime identity: tenant_id, runtime_placement_mode, "
        "workspace_id, actor_type, actor_id."
    )


def _sanitize_tool_request_metadata(value: Any) -> Any:
    """Return metadata safe for tool request context serialization."""
    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in _RAW_LLM_SECRET_METADATA_KEYS:
                continue
            sanitized[str(key)] = _sanitize_tool_request_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_tool_request_metadata(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_tool_request_metadata(item) for item in value)
    return value


def _sync_runtime_identity_metadata(
    *,
    metadata: Dict[str, Any],
    runtime_context: GraphRuntimeContext,
) -> None:
    """Project validated runtime identity fields to top-level request metadata."""
    runtime_placement_mode = runtime_context.normalized_runtime_placement_mode()
    if runtime_placement_mode:
        metadata["runtime_placement_mode"] = runtime_placement_mode
    for field_name in _RUNTIME_IDENTITY_METADATA_FIELDS:
        value = getattr(runtime_context, field_name, None)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        metadata[field_name] = value


def _materialize_local_runtime_identity(
    *,
    runtime_context: GraphRuntimeContext,
    metadata: Mapping[str, Any],
) -> GraphRuntimeContext:
    """Backfill local-only runtime identity for legacy local execution callers.

    Phase 1 Task 1.2 requires strict runtime identity for runner dispatch. Local
    callers that already provide a valid local workspace path keep compatibility
    by receiving deterministic local identity defaults when upstream projection
    has not yet populated those fields.
    """
    placement = runtime_context.normalized_runtime_placement_mode()
    if placement is None:
        placement = _normalize_runtime_placement_mode(metadata.get("runtime_placement_mode"))
    if placement not in {None, "local"}:
        return runtime_context
    if placement is None and _looks_like_product_runtime_context(
        runtime_context=runtime_context,
        metadata=metadata,
    ):
        return runtime_context
    if not isinstance(runtime_context.workspace_path, str) or not runtime_context.workspace_path.strip():
        return runtime_context

    tenant_id = runtime_context.tenant_id
    if tenant_id is None:
        tenant_candidate = metadata.get("tenant_id")
        if isinstance(tenant_candidate, int):
            tenant_id = tenant_candidate
        elif isinstance(tenant_candidate, str) and tenant_candidate.strip():
            try:
                tenant_id = int(tenant_candidate.strip())
            except ValueError:
                tenant_id = None
    if tenant_id is None:
        tenant_id = 0

    workspace_id_raw = runtime_context.workspace_id or metadata.get("workspace_id")
    workspace_id = str(workspace_id_raw).strip() if workspace_id_raw is not None else ""
    if not workspace_id:
        workspace_id = f"task-{runtime_context.task_id}"

    actor_type_raw = runtime_context.actor_type or metadata.get("actor_type")
    actor_type = str(actor_type_raw).strip() if actor_type_raw is not None else ""
    if not actor_type:
        actor_type = "agent" if runtime_context.user_id is not None else "system"

    actor_id_raw = runtime_context.actor_id or metadata.get("actor_id")
    actor_id = str(actor_id_raw).strip() if actor_id_raw is not None else ""
    if not actor_id:
        actor_id = "langgraph" if actor_type in {"agent", "user"} else "runtime"

    user_id = runtime_context.user_id
    if user_id is None:
        user_candidate = metadata.get("user_id")
        if isinstance(user_candidate, int):
            user_id = user_candidate
        elif isinstance(user_candidate, str) and user_candidate.strip():
            try:
                user_id = int(user_candidate.strip())
            except ValueError:
                user_id = None

    return runtime_context.model_copy(
        update={
            "tenant_id": tenant_id,
            "runtime_placement_mode": placement or "local",
            "workspace_id": workspace_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "user_id": user_id,
        }
    )


def _looks_like_product_runtime_context(
    *,
    runtime_context: GraphRuntimeContext,
    metadata: Mapping[str, Any],
) -> bool:
    """Return True when identity fields indicate product task execution."""
    product_fields = (
        "tenant_id",
        "workspace_id",
        "actor_type",
        "actor_id",
        "runner_id",
        "execution_site_id",
    )
    for field_name in product_fields:
        value = getattr(runtime_context, field_name, None)
        if value not in (None, ""):
            return True
        metadata_value = metadata.get(field_name)
        if metadata_value not in (None, ""):
            return True
    return False


def _resolve_planner_history(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the planner's cross-turn history from the shared bundle.

    Reads ``metadata[METADATA_CONTEXT_BUNDLE_KEY]``, projects it for
    the planner role, and returns the projection's
    ``transcript_window["turns"]`` verbatim. This is the same
    recent-turn window every other prompt-authoritative role observes,
    so classifier / category selector / planner share a single
    continuity surface.

    After the Phase 5 authority cutover the bundle is required. When
    it is missing the function raises ``RuntimeError`` — the facade /
    context builder are responsible for populating it once per turn.
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, dict):
        raise RuntimeError(
            "request_context: metadata[context_bundle] is missing; "
            "the hot-path ConversationContextBundle must be populated "
            "upstream (see LangGraphContextBuilder / facade_helpers.build_metadata)."
        )

    projection = project_for_planner(bundle)
    transcript_window = projection.get("transcript_window") or {}
    turns = transcript_window.get("turns") or []
    # Copy to plain list of dicts so callers cannot mutate the bundle.
    return [dict(message) for message in turns if isinstance(message, dict)]


def build_request_and_coordinator_config(
    interactive: InteractiveState,
    context: Optional[GraphRuntimeContext],
    metadata: Dict[str, Any],
) -> Tuple[ToolExecutionRequest, AgentConfig, Optional[GraphRuntimeContext], Optional[str]]:
    """Build tool request and coordinator config from state/context metadata."""
    sanitized_metadata = _sanitize_tool_request_metadata(metadata)
    metadata.clear()
    metadata.update(sanitized_metadata)

    facts = interactive.facts
    runtime_context_raw = metadata.get("graph_runtime_context") or {}
    runtime_context = (
        GraphRuntimeContext.model_validate(runtime_context_raw)
        if runtime_context_raw
        else None
    )
    if runtime_context is None and isinstance(context, GraphRuntimeContext):
        runtime_context = context
    if runtime_context is not None:
        runtime_context = _materialize_local_runtime_identity(
            runtime_context=runtime_context,
            metadata=metadata,
        )
        missing_fields = runtime_context.missing_tool_runtime_identity_fields()
        if missing_fields:
            _raise_missing_runtime_identity(missing_fields=missing_fields)
        _sync_runtime_identity_metadata(metadata=metadata, runtime_context=runtime_context)
        metadata["graph_runtime_context"] = runtime_context.model_dump()

    runtime_placement_mode = _normalize_runtime_placement_mode(
        (runtime_context.runtime_placement_mode if runtime_context else None)
        or metadata.get("runtime_placement_mode")
        or (context.runtime_placement_mode if isinstance(context, GraphRuntimeContext) else None)
    )
    if runtime_placement_mode is None:
        _raise_missing_runtime_identity(missing_fields=["runtime_placement_mode"])

    workspace_path = runtime_context.workspace_path if runtime_context else None
    if runtime_placement_mode != "local":
        workspace_path = None
    provider = (
        metadata.get("provider")
        or metadata.get("runtime_provider")
        or (runtime_context.provider if runtime_context else None)
    )
    model = metadata.get("model") or metadata.get("runtime_model") or (
        runtime_context.model if runtime_context else None
    )
    credential_ref = metadata.get("credential_ref") or (
        runtime_context.credential_ref if runtime_context else None
    )
    reasoning_effort = metadata.get("reasoning_effort") or (
        runtime_context.reasoning_effort if runtime_context else None
    )

    request = ToolExecutionRequest(
        capability=str(facts.metadata.get("intent_capability") or facts.capability or "simple_tool_execution"),
        targets=list(
            (facts.intent_hints.get("targets") if facts.intent_hints else [])
            or []
        ),
        message=facts.message,
        task_id=facts.task_id,
        conversation_id=facts.conversation_id,
        history=_resolve_planner_history(metadata),
        metadata=metadata,
        workspace_path=workspace_path,
        user_id=runtime_context.user_id if runtime_context else None,
        provider=str(provider) if provider else None,
        model=model,
        credential_ref=dict(credential_ref) if isinstance(credential_ref, Mapping) else None,
        llm_runtime_selection=(
            dict(metadata["llm_runtime_selection"])
            if isinstance(metadata.get("llm_runtime_selection"), Mapping)
            else None
        ),
        reasoning_effort=str(reasoning_effort) if reasoning_effort else None,
    )

    workspace_path = request.workspace_path or (context.workspace_path if context else None)
    if runtime_placement_mode != "local":
        workspace_path = None

    if runtime_placement_mode == "local" and not workspace_path and request.task_id is not None:
        raise RuntimeError(
            "tool_execution_runtime: local runtime workspace_path is required for local "
            "task-scoped tool execution; upstream runtime provider projection is missing workspace identity."
        )

    # Keep request + metadata workspace path consistent for downstream coordinator/executors.
    if workspace_path:
        request.workspace_path = workspace_path
        if not metadata.get("workspace_path"):
            metadata["workspace_path"] = workspace_path
            request.metadata = metadata
    elif "workspace_path" in metadata:
        metadata.pop("workspace_path", None)
        request.metadata = metadata

    coordinator_config = AgentConfig(
        task_id=str(request.task_id) if request.task_id is not None else None,
        tenant_id=runtime_context.tenant_id if runtime_context else None,
        workspace_path=workspace_path,
        model_name=request.model or (context.model if context else DEFAULT_MODEL),
    )
    coordinator_config.runtime_placement_mode = runtime_placement_mode
    return request, coordinator_config, runtime_context, workspace_path
