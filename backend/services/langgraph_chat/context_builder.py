"""Context builder responsible for assembling runtime configuration."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
import time
import logging
import threading

from backend.config import (
    ENABLE_LANGGRAPH_DEEP_REASONING,
    ENABLE_LANGGRAPH_FORCE_SIMPLE_CHAT,
    ENABLE_LANGGRAPH_SIMPLE_TOOL,
    E2E_DETERMINISTIC_MODE,
)
from backend.database import SessionLocal
from backend.services.runtime_provider import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationService,
)

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.runtime_state import refresh_bundle_from_working_memory

from .contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
    PersistenceContext,
    ToolingContext,
)
from backend.services.langgraph_chat.intent.signals import (
    collect_intent_signals,
    embed_intent_signals,
)

logger = logging.getLogger(__name__)
_REQUIRED_RUNTIME_IDENTITY_KEYS = (
    "tenant_id",
    "graph_thread_id",
    "runtime_placement_mode",
    "workspace_id",
    "actor_type",
    "actor_id",
)


def _run_sync(coro):
    """Run a coroutine from sync context without assuming caller loop ownership."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if not loop.is_running():
        return loop.run_until_complete(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def _resolve_provider_runtime_projection(*, task_id: int, user_id: int) -> dict[str, Any]:
    """Resolve provider runtime identity and local workspace projection."""
    db = SessionLocal()
    try:
        runtime_operations = RuntimeOperationService(db)
        runtime_call_scope = (
            RuntimeCallScope.TEST
            if E2E_DETERMINISTIC_MODE
            else RuntimeCallScope.PRODUCT_TASK
        )
        context = runtime_operations.context_for_internal_task(
            task_id=task_id,
            actor_type=RuntimeActorType.SYSTEM,
            actor_id="chat_context_builder",
            user_id=user_id,
            runtime_call_scope=runtime_call_scope,
        )
        if E2E_DETERMINISTIC_MODE:
            projection = context.to_worker_payload()
            projection["task_id"] = task_id
            projection["actor_type"] = RuntimeActorType.AGENT.value
            projection["actor_id"] = "langgraph"
            return projection
        result = _run_sync(
            runtime_operations.run_for_context(
                context=context,
                operation="materialize_runtime_workspace",
                call=lambda provider, request: provider.materialize_runtime_workspace(request),
                runtime_call_scope=runtime_call_scope,
            )
        )
        delegate = result.metadata.get("delegate_result") if result.ok else None
        projection = context.to_worker_payload()
        projection["task_id"] = task_id
        projection["actor_type"] = RuntimeActorType.AGENT.value
        projection["actor_id"] = "langgraph"
        if isinstance(delegate, dict):
            workspace_path = delegate.get("workspace_path")
            if workspace_path:
                projection["workspace_path"] = str(workspace_path)
        return projection
    except Exception:
        logger.warning("Failed to resolve provider runtime projection for task %s", task_id, exc_info=True)
    finally:
        db.close()
    return {}


def _is_canonical_environment_info(value: Any) -> bool:
    """Return true for structured runtime environment info, not flat metadata."""
    if not isinstance(value, dict):
        return False
    return any(key in value for key in ("hostname", "os", "network", "routes"))


def _resolve_runtime_environment_info(*, task_id: int, user_id: int) -> dict[str, Any] | None:
    """Load canonical runtime environment info from local management-plane state.

    Reads the environment captured once at container start (persisted on the
    TASK_START runtime job for cloud placement, or the local workspace file for
    local placement). This is a synchronous, local read with no remote runner
    round-trip, so it never blocks the serving event loop during turn setup.
    """
    try:
        from backend.services.runtime_provider.environment_metadata import (
            resolve_local_runtime_environment_info,
        )

        env_info = resolve_local_runtime_environment_info(task_id=int(task_id))
    except Exception:
        logger.warning(
            "Failed to resolve runtime environment info for task %s",
            task_id,
            exc_info=True,
        )
        return None
    if _is_canonical_environment_info(env_info):
        return dict(env_info)
    return None


def _normalize_runtime_placement_mode(value: Any) -> str | None:
    """Normalize runtime placement mode from provider projection metadata."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _validate_runtime_projection_identity(*, runtime_projection: Dict[str, Any]) -> None:
    """Fail closed when required runtime identity fields are missing."""
    if not runtime_projection:
        return
    missing = [
        key for key in _REQUIRED_RUNTIME_IDENTITY_KEYS if runtime_projection.get(key) in (None, "")
    ]
    if not missing:
        return
    missing_text = ", ".join(missing)
    raise RuntimeError(
        "context_builder: runtime provider projection is missing runtime identity field(s): "
        f"{missing_text}."
    )


# Route-policy derivation table keyed by `agent_mode`.
#
# `Plan` and `Chat` are user-facing execution tiers: they must force the
# LangGraph turn onto a specific backend branch while still running the
# intent classifier for interpretation. `execution_route_policy` is the
# single durable forced-route authority introduced for those tiers — it
# is derived here (the one place where `agent_mode` enters runtime
# metadata) and consumed downstream by the classifier prompt, the facade
# branch selection, and the deep-reasoning graph-entry override.
#
# `agent` and `agent_full` are not branch selectors on their own and
# therefore have no route policy entry (`None`). Not emitting the key at
# all for those modes keeps downstream consumers' "policy present?"
# check trivial.
#
# Phase 6: `plan_mode` is a separate route-overlay input (boolean) that
# also derives the deep-reasoning forced route, but stacked on top of
# `agent` / `full_access` — see `_derive_execution_route_policy`. The
# legacy `AgentMode.PLAN` row remains for callers that predate the
# normalization at the HTTP boundary; new paths must not emit
# `AgentMode.PLAN`.
_AGENT_MODE_ROUTE_POLICY: Dict[AgentMode, Dict[str, str]] = {
    AgentMode.PLAN: {
        "agent_mode": AgentMode.PLAN.value,
        "forced_execution_mode": ExecutionMode.DEEP_REASONING.value,
        "forced_classifier_label": "plan_executor",
    },
    AgentMode.CHAT: {
        "agent_mode": AgentMode.CHAT.value,
        "forced_execution_mode": ExecutionMode.NORMAL_CHAT.value,
        "forced_classifier_label": "simple_chat",
    },
}


def _derive_execution_route_policy(
    agent_mode: AgentMode,
    *,
    plan_mode: bool = False,
) -> Optional[Dict[str, str]]:
    """Return the `execution_route_policy` metadata for the normalized inputs.

    Phase 6 derivation rules:

    - ``plan_mode=True`` always forces the deep-reasoning route and the
      ``plan_executor`` classifier label, regardless of whether
      ``agent_mode`` is ``agent`` or ``full_access``. Autonomy / tool
      approval behavior continues to key off ``agent_mode`` downstream —
      the route overlay does NOT change approval policy.
    - ``agent_mode=chat`` forces the normal-chat route.
    - ``agent_mode=plan`` still returns the same deep-reasoning policy
      (legacy passthrough — the request boundary normalizes this shape
      into ``agent_mode=agent`` + ``plan_mode=true`` before it reaches
      here; the row is kept so direct service callers / tests remain
      valid during the migration window).
    - ``agent`` and ``full_access`` without ``plan_mode`` return
      ``None`` so downstream consumers see "no forced route".

    The ``source`` marker distinguishes overlay-derived policies
    (``plan_mode``) from user-tier policies (``agent_mode``) so audit /
    debug surfaces can tell the two apart.
    """
    if plan_mode:
        return {
            "source": "plan_mode",
            "agent_mode": agent_mode.value,
            "plan_mode": True,
            "forced_execution_mode": ExecutionMode.DEEP_REASONING.value,
            "forced_classifier_label": "plan_executor",
        }
    template = _AGENT_MODE_ROUTE_POLICY.get(agent_mode)
    if template is None:
        return None
    return {
        "source": "agent_mode",
        "agent_mode": template["agent_mode"],
        "forced_execution_mode": template["forced_execution_mode"],
        "forced_classifier_label": template["forced_classifier_label"],
    }


class ConflictingExecutionRouteError(ValueError):
    """Raised when `requested_mode` and `agent_mode` disagree on the branch.

    `requested_mode` is an internal-caller override surface (currently
    unused by the public chat router) and `agent_mode` is the
    user-surface tier selector. Per the guide's recommended resolution
    (§Open Questions), we fail loudly on disagreement instead of
    silently preferring one authority — silent precedence creates a
    second hidden routing authority and hides miswired callers.
    """


def _assert_no_route_conflict(
    *,
    requested_mode: Optional[ExecutionMode],
    agent_mode: AgentMode,
    plan_mode: bool = False,
) -> None:
    """Fail closed when `requested_mode` contradicts the derived route policy.

    Task 1.4 / Phase 6: when a caller provides an explicit internal
    ``requested_mode`` alongside a route-selecting input, the two
    values must agree on the forced execution branch. Otherwise the
    downstream facade / classifier / graph-entry override would see two
    conflicting forced-route authorities and the resolution order would
    become an implementation detail of whichever consumer reads first.

    Route-selecting inputs are (a) legacy ``agent_mode`` values that
    derive a policy (``plan`` / ``chat``), or (b) ``plan_mode=True``
    stacked on top of ``agent`` / ``full_access``. Non-selecting
    ``agent_mode`` values (``agent``, ``full_access``) without
    ``plan_mode`` remain passthrough for internal callers.
    """
    if requested_mode is None:
        return
    if plan_mode:
        forced = ExecutionMode.DEEP_REASONING.value
        source_label = f"plan_mode=True (agent_mode={agent_mode.value!r})"
    else:
        template = _AGENT_MODE_ROUTE_POLICY.get(agent_mode)
        if template is None:
            return
        forced = template["forced_execution_mode"]
        source_label = f"agent_mode={agent_mode.value!r}"
    if requested_mode.value != forced:
        raise ConflictingExecutionRouteError(
            "Conflicting execution-route inputs: "
            f"{source_label} forces execution_mode={forced!r} but "
            f"requested_mode={requested_mode.value!r}. "
            "Resolve at the caller — do not pass both authorities with "
            "disagreeing values."
        )


_TOOL_CACHE_LOCK = threading.Lock()
_TOOL_IDS_CACHE: Optional[List[str]] = None
_TOOL_METADATA_CACHE: Dict[str, Dict[str, Any]] = {}


def _get_tool_ids_cached(loader: Callable[[], List[str]]) -> List[str]:
    global _TOOL_IDS_CACHE
    if _TOOL_IDS_CACHE is not None:
        return list(_TOOL_IDS_CACHE)
    with _TOOL_CACHE_LOCK:
        if _TOOL_IDS_CACHE is None:
            _TOOL_IDS_CACHE = sorted(loader())
    return list(_TOOL_IDS_CACHE)


def _get_tool_metadata_cached(
    tool_id: str,
    loader: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    cached = _TOOL_METADATA_CACHE.get(tool_id)
    if cached is not None:
        return cached
    with _TOOL_CACHE_LOCK:
        cached = _TOOL_METADATA_CACHE.get(tool_id)
        if cached is not None:
            return cached
        try:
            cached = loader(tool_id)
        except Exception:
            cached = {
                "name": tool_id,
                "description": "",
                "args_schema": {},
            }
        _TOOL_METADATA_CACHE[tool_id] = cached
        return cached


class _LazyToolCatalog(dict):
    def __init__(
        self,
        *,
        loader: Callable[[str], Dict[str, Any]],
        initial: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(initial or {})
        self._loader = loader

    def __missing__(self, key: str) -> Dict[str, Any]:
        value = self._loader(key)
        self[key] = value
        return value


class LangGraphContextBuilder:
    """Creates `LangGraphRuntimeConfig` instances used by the facade."""

    def build_runtime_config(
        self,
        *,
        chat_inputs: ChatInputs,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LangGraphRuntimeConfig:
        """Assemble a runtime configuration for the provided chat inputs."""
        start_time = time.perf_counter()

        # Phase 6 Task 6.4: reject the mutually exclusive combo
        # ``agent_mode=chat`` + ``plan_mode=true`` as a defense-in-depth
        # check for service-layer callers (the HTTP boundary already
        # rejects this combo). Raising
        # ``ConflictingExecutionRouteError`` keeps all route-input
        # contradictions on a single error class.
        if chat_inputs.agent_mode == AgentMode.CHAT and chat_inputs.plan_mode:
            raise ConflictingExecutionRouteError(
                "Conflicting execution-route inputs: agent_mode='chat' "
                "is mutually exclusive with plan_mode=True. Plan is a "
                "route overlay for agent / full_access; it cannot stack "
                "on top of chat."
            )

        # Task 1.4 / Phase 6: reject conflicting `requested_mode` vs the
        # derived route authority before doing any work. `agent_mode` is
        # the user-surface tier selector, `plan_mode` is the stacked
        # route overlay, and `requested_mode` is an internal-caller
        # override — when any two supply disagreeing forced branches we
        # raise instead of silently preferring one. Silent precedence
        # would hide miswired internal callers behind whichever consumer
        # reads first.
        _assert_no_route_conflict(
            requested_mode=chat_inputs.requested_mode,
            agent_mode=chat_inputs.agent_mode,
            plan_mode=chat_inputs.plan_mode,
        )

        persistence = PersistenceContext(anchor_sequence=chat_inputs.anchor_sequence)
        execution_mode = chat_inputs.requested_mode or ExecutionMode.NORMAL_CHAT
        merged_metadata = dict(metadata or {})

        merged_metadata["agent_mode"] = chat_inputs.agent_mode.value
        # Phase 6: surface ``plan_mode`` as durable audit metadata so
        # audit / debug consumers can distinguish a Plan-overlay turn
        # from a plain agent turn without parsing
        # ``execution_route_policy``.
        merged_metadata["plan_mode"] = bool(chat_inputs.plan_mode)
        merged_metadata["plan_review_required"] = bool(
            chat_inputs.plan_mode or chat_inputs.agent_mode == AgentMode.PLAN
        )

        # Derive route policy from the normalized `(agent_mode, plan_mode)`
        # pair. This is the single durable forced-route authority;
        # consumers downstream (classifier prompt, facade branch
        # selection, deep-reasoning graph-entry override) read this key
        # only. `agent` / `full_access` without `plan_mode` intentionally
        # do not emit the key.
        route_policy = _derive_execution_route_policy(
            chat_inputs.agent_mode,
            plan_mode=chat_inputs.plan_mode,
        )
        if route_policy is not None:
            merged_metadata["execution_route_policy"] = route_policy

        # Ensure runtime workspace exists and get provider-reported local runtime projection.
        workspace_start = time.perf_counter()
        runtime_projection = _resolve_provider_runtime_projection(
            task_id=chat_inputs.task_id,
            user_id=chat_inputs.user_id,
        )
        _validate_runtime_projection_identity(runtime_projection=runtime_projection)
        placement_mode = _normalize_runtime_placement_mode(
            runtime_projection.get("runtime_placement_mode")
        )
        if "workspace_path" in runtime_projection:
            workspace_path = runtime_projection.get("workspace_path")
            if placement_mode == "local" and workspace_path:
                merged_metadata["workspace_path"] = workspace_path
            else:
                merged_metadata.pop("workspace_path", None)
        for key in (
            "tenant_id",
            "graph_thread_id",
            "runtime_placement_mode",
            "workspace_id",
            "actor_type",
            "actor_id",
            "runner_id",
            "execution_site_id",
        ):
            if runtime_projection.get(key) is not None:
                merged_metadata[key] = runtime_projection[key]
        if runtime_projection:
            merged_metadata["runtime_provider_projection"] = runtime_projection
        logger.warning(
            "[CONTEXT] Workspace ready for task %s in %.2f ms",
            chat_inputs.task_id,
            (time.perf_counter() - workspace_start) * 1000,
        )

        environment_start = time.perf_counter()
        environment_info = _resolve_runtime_environment_info(
            task_id=chat_inputs.task_id,
            user_id=chat_inputs.user_id,
        )
        if environment_info is not None:
            merged_metadata["environment_info"] = environment_info
        logger.warning(
            "[CONTEXT] Environment info %s for task %s in %.2f ms",
            "loaded" if environment_info is not None else "unavailable",
            chat_inputs.task_id,
            (time.perf_counter() - environment_start) * 1000,
        )

        feature_flags = dict(merged_metadata.get("feature_flags") or {})
        feature_flags.setdefault(
            "deep_reasoning_enabled", ENABLE_LANGGRAPH_DEEP_REASONING
        )
        feature_flags.setdefault("simple_tool_enabled", ENABLE_LANGGRAPH_SIMPLE_TOOL)
        feature_flags.setdefault(
            "force_simple_chat_enabled", ENABLE_LANGGRAPH_FORCE_SIMPLE_CHAT
        )
        merged_metadata["feature_flags"] = feature_flags

        if (
            ENABLE_LANGGRAPH_FORCE_SIMPLE_CHAT
            and "force_simple_chat" not in merged_metadata
        ):
            merged_metadata["force_simple_chat"] = True
        if merged_metadata.get("force_simple_chat"):
            merged_metadata.setdefault("simple_chat_forced", True)

        merged_metadata.setdefault("memory_snapshot", {})
        merged_metadata.setdefault("recent_tool_summaries", [])

        # Phase 3 migration: assemble the hot-path
        # ``ConversationContextBundle`` once here so every
        # prompt-authoritative role (intent classifier first, then
        # category selector and planner in 3.2/3.3) reads the same
        # single-assembly authority via ``runtime_config.metadata``
        # instead of rebuilding its own transcript view. The legacy
        # facade_helpers.build_metadata dual-write is kept during the
        # compatibility window (removed in Phase 5).
        merged_metadata[METADATA_CONTEXT_BUNDLE_KEY] = (
            build_conversation_context_bundle(
                conversation_id=chat_inputs.conversation_id or "",
                turn_id=str(merged_metadata.get("turn_id") or ""),
                turn_sequence=int(merged_metadata.get("turn_sequence") or 0),
                messages=list(chat_inputs.history),
                current_message=chat_inputs.message,
            )
        )
        # Refresh the freshly-seeded bundle against any working memory
        # already present in metadata. At this pre-turn stage the key is
        # typically absent (no-op path), but wiring the call keeps the
        # bundle authoritative when upstream seeds working memory early.
        refresh_bundle_from_working_memory(merged_metadata)

        intent_signal_start = time.perf_counter()
        bundle = collect_intent_signals(
            message=chat_inputs.message,
            history=chat_inputs.history,
            metadata=merged_metadata,
        )
        embed_intent_signals(merged_metadata, bundle)
        logger.warning(
            "[CONTEXT] Intent signals built for task %s in %.2f ms",
            chat_inputs.task_id,
            (time.perf_counter() - intent_signal_start) * 1000,
        )

        # Task 1.3: preserve safety / global force_simple_chat precedence.
        #
        # Precedence order (guide §"Preserve Existing Safety/Force-Simple-Chat
        # Precedence"):
        #   1. safety / system forced-capability guardrails
        #   2. internal deterministic / explicit internal requested-mode
        #   3. user-surface `agent_mode` route policy for plan / chat
        #   4. classifier route label
        #   5. heuristic fallback
        #
        # `intent_signals.collect_intent_signals` sets `forced_capability`
        # in metadata when either a safety pattern is detected or the
        # global `force_simple_chat` flag is active. Those paths MUST
        # outrank a user-surface `plan`/`chat` tier selection — a user
        # cannot force `plan` past a safety guardrail, and a global
        # `force_simple_chat` deployment toggle cannot be overridden by
        # the UI tier. In either case the route policy must be dropped so
        # downstream consumers (classifier prompt, facade branch
        # selection, graph-entry override) see no forced tier and fall
        # back to the already-set `forced_capability` path.
        if route_policy is not None and merged_metadata.get("forced_capability"):
            merged_metadata.pop("execution_route_policy", None)

        tooling_start = time.perf_counter()
        tooling = self._build_tooling_context(merged_metadata)
        logger.warning(
            "[CONTEXT] Tooling context built for task %s in %.2f ms (tools=%s)",
            chat_inputs.task_id,
            (time.perf_counter() - tooling_start) * 1000,
            len(tooling.available_tools) if tooling.available_tools else 0,
        )

        config = LangGraphRuntimeConfig(
            chat_inputs=chat_inputs,
            tooling=tooling,
            persistence=persistence,
            execution_mode=execution_mode,
            metadata=merged_metadata,
            llm_runtime_selection=chat_inputs.llm_runtime_selection,
        )
        logger.warning(
            "[CONTEXT] Runtime config total for task %s in %.2f ms",
            chat_inputs.task_id,
            (time.perf_counter() - start_time) * 1000,
        )
        return config

    def _build_tooling_context(self, metadata: Dict[str, Any]) -> ToolingContext:
        """Construct a tooling context using the shared tool registry metadata."""

        default_capability = metadata.get("forced_capability") or metadata.get(
            "initial_capability"
        )

        try:
            from agent.tools.tool_registry import available_tools, get_tool_metadata
        except Exception:
            return ToolingContext(default_capability=default_capability)

        try:
            tool_ids = _get_tool_ids_cached(available_tools)
        except Exception:
            tool_ids = []

        def metadata_loader(tool_id: str) -> Dict[str, Any]:
            return _get_tool_metadata_cached(tool_id, get_tool_metadata)

        if metadata.get("tool_catalog_eager"):
            catalog: Dict[str, Dict[str, Any]] = {}
            for tool_id in tool_ids:
                catalog[tool_id] = metadata_loader(tool_id)
        else:
            catalog = _LazyToolCatalog(loader=metadata_loader)

        return ToolingContext(
            available_tools=tool_ids,
            default_capability=default_capability,
            catalog=catalog,
        )


__all__ = ["ConflictingExecutionRouteError", "LangGraphContextBuilder"]
