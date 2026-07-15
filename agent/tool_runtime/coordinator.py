"""Planner + executor coordinator reused by LangGraph tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import InitVar, dataclass, field
from time import monotonic
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from agent.config import AgentConfig
from agent.graph.adapters.executor_adapter import GraphToolExecutor
from agent.logger import AgentLogger
from agent.models import ActionType, ExecutionStrategy
from agent.reasoning.enhanced_planner import (
    EnhancedActionPlanner,
    PlannerToolParameterValidationError,
)
from agent.tool_runtime.batch.plan_view import serialized_tool_calls_from_plan
from agent.tools.catalog_visibility import is_tool_visible_in_catalog
from agent.tools.tool_registry import get_catalog_metadata_snapshot
from .timeout_policy import resolve_tool_timeout_plan

_RAW_LLM_SECRET_METADATA_KEYS = frozenset(
    {
        "api_key",
        "runtime_api_key",
        "openai_api_key",
    }
)


def _serialize_tool_batch_for_plan(batch: Any) -> Dict[str, Any]:
    """Serialize a planner ToolBatch for coordinator-local plan storage."""
    return {
        "tool_batch_id": getattr(batch, "tool_batch_id", ""),
        "requested_execution_strategy": getattr(
            getattr(batch, "requested_execution_strategy", None),
            "value",
            str(getattr(batch, "requested_execution_strategy", "sequential")),
        ),
        "deferred_followups": list(getattr(batch, "deferred_followups", ()) or ()),
        "selection_rationale": str(getattr(batch, "selection_rationale", "") or ""),
        "tool_calls": [
            {
                "tool_call_id": getattr(call, "tool_call_id", ""),
                "tool_id": getattr(call, "tool_id", ""),
                "parameters": dict(getattr(call, "parameters", {}) or {}),
                "intent": str(getattr(call, "intent", "") or ""),
            }
            for call in (getattr(batch, "tool_calls", ()) or ())
        ],
    }


def _single_call_from_plan(plan: Mapping[str, Any]) -> Optional[tuple[str, Dict[str, Any]]]:
    """Return the only canonical tool call from a persisted planner plan."""
    calls = serialized_tool_calls_from_plan(plan)
    if not calls:
        return None
    if len(calls) != 1:
        raise RuntimeError(
            "ToolExecutionCoordinator requires a single-call tool_batch; "
            "multi-call batches must run through BatchExecutor"
        )
    call = calls[0]
    return call.tool_id, dict(call.parameters)


def _sanitize_request_metadata(value: Any) -> Any:
    """Return tool runtime metadata with raw LLM secret keys removed."""
    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in _RAW_LLM_SECRET_METADATA_KEYS:
                continue
            sanitized[str(key)] = _sanitize_request_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_request_metadata(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_request_metadata(item) for item in value)
    return value


@dataclass(slots=True)
class ToolCatalogEntry:
    """Lightweight metadata describing a tool for prompt construction and ranking."""
    
    tool_id: str
    name: str
    category: str
    description: str
    # Fields for ranking engine
    disqualification_reason: Optional[str] = None
    capability_aliases: List[str] = field(default_factory=list)
    risk_level: int = 3  # 1-5 scale, default medium risk
    required_privileges: List[str] = field(default_factory=list)
    minimum_budget_minutes: int = 5  # Default 5 minutes
    execution_priority: int = 5  # 1-10 scale, default medium priority


@dataclass(slots=True)
class ToolExecutionRequest:
    capability: str
    targets: Sequence[str]
    message: str
    task_id: Optional[int] = None
    conversation_id: Optional[str] = None
    history: Sequence[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    workspace_path: Optional[str] = None
    user_id: Optional[int] = None
    provider: Optional[str] = None
    api_key: InitVar[Optional[str]] = None
    model: Optional[str] = None
    credential_ref: Optional[Dict[str, Any]] = None
    llm_runtime_selection: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None

    def __post_init__(self, api_key: Optional[str]) -> None:
        """Accept legacy constructor input without storing provider secrets."""
        _ = api_key
        self.metadata = _sanitize_request_metadata(self.metadata)


@dataclass(slots=True)
class ToolExecutionOutcome:
    tool_id: Optional[str]
    parameters: Dict[str, Any]
    catalog: List[ToolCatalogEntry]
    result: Dict[str, Any]
    summary: str
    reasoning: List[str]
    duration: float

    def to_graph_metadata(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return {
            "tool": self.tool_id,
            "parameters": dict(self.parameters),
            "reasoning": list(self.reasoning),
            "catalog": [asdict(entry) for entry in self.catalog],
            "result": dict(self.result),
            "duration": self.duration,
            "summary": self.summary,
        }


class ToolExecutionCoordinator:
    """Coordinates LLM planning, catalog exposure, execution, and synthesis."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        logger: Optional[AgentLogger] = None,
        planner: Optional[EnhancedActionPlanner] = None,
        executor: Optional[GraphToolExecutor] = None,
    ) -> None:
        self._config = config

        # Set WORKSPACE env var for AgentLogger only when a local workspace is available.
        if logger is not None:
            self._logger = logger
        elif config.workspace_path:
            import os
            old_workspace = os.environ.get("WORKSPACE")
            os.environ["WORKSPACE"] = config.workspace_path
            try:
                self._logger = AgentLogger(str(config.task_id or "tool-coordinator"))
            finally:
                # Restore original WORKSPACE if it existed
                if old_workspace is not None:
                    os.environ["WORKSPACE"] = old_workspace
                else:
                    os.environ.pop("WORKSPACE", None)
        else:
            self._logger = None

        self._planner = planner
        self._executor = executor

    async def run(self, request: ToolExecutionRequest) -> ToolExecutionOutcome:
        run_started = monotonic()
        executor = self._executor or GraphToolExecutor()

        catalog_entries = self._build_catalog(
            request.capability,
            request.metadata,
            task_id=request.task_id,
            history=request.history,
            user_message=request.message,
        )
        resolved_tools = [entry.tool_id for entry in catalog_entries]

        action = self._build_action_input(request)
        cache_key = self._build_plan_cache_key(request, resolved_tools)

        persisted_plan = request.metadata.get("planner_plan") if isinstance(request.metadata, dict) else None

        planned_tool_id: Optional[str]
        planned_parameters: Dict[str, Any]
        plan_reasoning: str
        plan_expected_outcome: str
        plan_execution_strategy: ExecutionStrategy

        persisted_call = (
            _single_call_from_plan(persisted_plan)
            if isinstance(persisted_plan, Mapping)
            else None
        )
        if persisted_call is not None:
            planned_tool_id, planned_parameters = persisted_call
            plan_reasoning = str(persisted_plan.get("reasoning", ""))
            plan_expected_outcome = str(persisted_plan.get("expected_outcome", ""))
            exec_value = persisted_plan.get("execution_strategy") or "parallel"
            try:
                plan_execution_strategy = ExecutionStrategy(exec_value)
            except Exception:
                plan_execution_strategy = ExecutionStrategy.PARALLEL
            self._safe_inc_metric("langgraph_planner_plan_supplied")
        else:
            plan_cache = self._get_cached_plan(request.metadata, cache_key)
            cached_call = (
                _single_call_from_plan(plan_cache)
                if isinstance(plan_cache, Mapping)
                else None
            )
            if plan_cache is None:
                self._safe_inc_metric("langgraph_planner_cache_miss")
            elif cached_call is None:
                self._safe_inc_metric("langgraph_planner_cache_miss")
                plan_cache = None
            else:
                self._safe_inc_metric("langgraph_planner_cache_hit")

            if cached_call is not None:
                planned_tool_id, planned_parameters = cached_call
                plan_reasoning = str(plan_cache.get("reasoning", ""))
                plan_expected_outcome = str(plan_cache.get("expected_outcome", ""))
                exec_value = plan_cache.get("execution_strategy")
            else:
                planned_tool_id = None
                planned_parameters = {}
                plan_reasoning = ""
                plan_expected_outcome = ""
                exec_value = None

            if not planned_tool_id:
                planner = await self._ensure_planner(request)
                planner_context = self._planner_context(request, catalog_entries, resolved_tools)
                try:
                    plan = await planner.build_action_plan(action, planner_context)
                except PlannerToolParameterValidationError as exc:
                    if self._logger:
                        self._logger.log_operation(
                            "WARNING",
                            (
                                "Planner rejected tool parameters "
                                f"(tool={exc.tool_id}, reason={exc.reason})"
                            ),
                            metadata={
                                "tool_id": exc.tool_id,
                                "reason": exc.reason,
                                "validation_error_count": len(exc.validation_errors),
                            },
                        )
                    self._safe_inc_metric("langgraph_planner_param_validation_failures")
                    return self._build_planner_validation_outcome(
                        catalog_entries=catalog_entries,
                        exc=exc,
                        duration=monotonic() - run_started,
                    )

                tool_batch = getattr(plan, "tool_batch", None)
                if tool_batch is None:
                    raise RuntimeError("Planner produced no canonical tool_batch")
                tool_calls = list(getattr(tool_batch, "tool_calls", ()) or ())
                if len(tool_calls) != 1:
                    raise RuntimeError(
                        "ToolExecutionCoordinator requires a single-call tool_batch; "
                        "multi-call batches must run through BatchExecutor"
                    )
                planned_tool_id = str(getattr(tool_calls[0], "tool_id", "") or "")
                planned_parameters = dict(getattr(tool_calls[0], "parameters", {}) or {})
                if not planned_tool_id:
                    raise RuntimeError("Planner produced a tool_batch without a tool_id")
                plan_reasoning = plan.reasoning
                plan_expected_outcome = plan.expected_outcome
                exec_value = (
                    plan.execution_strategy.value
                    if isinstance(plan.execution_strategy, ExecutionStrategy)
                    else str(plan.execution_strategy)
                )

                self._store_cached_plan(
                    request.metadata,
                    cache_key,
                    {
                        "tool_batch": _serialize_tool_batch_for_plan(tool_batch),
                        "reasoning": plan_reasoning,
                        "expected_outcome": plan_expected_outcome,
                        "execution_strategy": exec_value,
                    },
                )
                self._safe_inc_metric("langgraph_planner_cache_store")

            # Phase 9 Task 9.1.6: persisted plan-cache compatibility shim
            # removed (see the persisted-plan branch above for the rationale).
            try:
                plan_execution_strategy = ExecutionStrategy(exec_value) if exec_value else ExecutionStrategy.PARALLEL
            except Exception:
                plan_execution_strategy = ExecutionStrategy.PARALLEL

        if not planned_tool_id:
            raise RuntimeError("Planner produced no tool selection")

        tool_id = planned_tool_id
        parameters = dict(planned_parameters)
        timeout_plan = resolve_tool_timeout_plan(
            tool_id=tool_id,
            parameters=parameters,
            config=self._config,
        )
        parameters = dict(timeout_plan.normalized_parameters)

        graph_request = {
            "tool": tool_id,
            "parameters": parameters,
            "capability": request.capability,
            "task_id": request.task_id,
            "conversation_id": request.conversation_id,
            "workspace_path": request.workspace_path,
            "tenant_id": request.metadata.get("tenant_id") if isinstance(request.metadata, dict) else None,
            "runtime_placement_mode": (
                request.metadata.get("runtime_placement_mode")
                if isinstance(request.metadata, dict)
                else None
            ),
            "workspace_id": request.metadata.get("workspace_id") if isinstance(request.metadata, dict) else None,
            "runner_id": request.metadata.get("runner_id") if isinstance(request.metadata, dict) else None,
            "execution_site_id": (
                request.metadata.get("execution_site_id")
                if isinstance(request.metadata, dict)
                else None
            ),
            "actor_type": request.metadata.get("actor_type") if isinstance(request.metadata, dict) else None,
            "actor_id": request.metadata.get("actor_id") if isinstance(request.metadata, dict) else None,
            "user_id": request.user_id,
            "targets": list(request.targets),
            "reasoning": plan_reasoning,
            "expected_outcome": plan_expected_outcome,
            "provider": request.provider,
            "model": request.model,
            "credential_ref": dict(request.credential_ref) if request.credential_ref else None,
            "llm_runtime_selection": (
                dict(request.llm_runtime_selection) if request.llm_runtime_selection else None
            ),
            "reasoning_effort": request.reasoning_effort,
            "execution_strategy": plan_execution_strategy.value,
            "interrupt_id": request.metadata.get("interrupt_id") if isinstance(request.metadata, dict) else None,
            "tool_call_id": request.metadata.get("tool_call_id") if isinstance(request.metadata, dict) else None,
            "tool_batch_id": request.metadata.get("tool_batch_id") if isinstance(request.metadata, dict) else None,
            "timeout_plan": timeout_plan.to_metadata(),
        }

        started = monotonic()
        result = await executor.execute_tool(graph_request)
        elapsed = monotonic() - started

        summary = self._summarise_result(result)

        reasoning_lines: List[str] = []
        if plan_reasoning:
            reasoning_lines.append(str(plan_reasoning))

        return ToolExecutionOutcome(
            tool_id=tool_id,
            parameters=parameters,
            catalog=catalog_entries,
            result=result,
            summary=summary,
            reasoning=reasoning_lines,
            duration=elapsed,
        )

    def _build_planner_validation_outcome(
        self,
        *,
        catalog_entries: List[ToolCatalogEntry],
        exc: PlannerToolParameterValidationError,
        duration: float,
    ) -> ToolExecutionOutcome:
        tool_id = exc.tool_id
        parameters = dict(exc.provided_parameters or {})
        validation_errors = list(exc.validation_errors or [])

        concise_error = (
            "; ".join(
                f"{err.get('field', 'arguments')}: {err.get('message') or err.get('error')}"
                for err in validation_errors
            )
            if validation_errors
            else "Invalid tool parameters"
        )
        stderr = f"Validation error: {concise_error}"
        observation = (
            f"Planner produced invalid parameters for {tool_id}. "
            "Review required fields and retry."
        )

        result = {
            "tool": tool_id,
            "success": False,
            "stdout": "",
            "stderr": stderr,
            "stdout_excerpt": "",
            "stderr_excerpt": stderr[:500],
            "exit_code": -1,
            "observation": observation,
            "approval_granted": True,
            "approval_reason": None,
            "approval_metadata": {},
            "duration": duration,
            "metadata": {
                "error_type": "validation_error",
                "validation_errors": validation_errors,
                "planner_validation_reason": exc.reason,
            },
            "validation_errors": validation_errors,
            "status": "validation_error",
            "artifacts": [],
            "command_text": None,
        }

        summary = self._summarise_result(result)
        reasoning = [f"Planner validation error ({exc.reason}) for {tool_id}"]

        return ToolExecutionOutcome(
            tool_id=tool_id,
            parameters=parameters,
            catalog=catalog_entries,
            result=result,
            summary=summary,
            reasoning=reasoning,
            duration=duration,
        )

    async def _ensure_planner(self, request: ToolExecutionRequest) -> EnhancedActionPlanner:
        if self._planner is not None:
            return self._planner

        llm_client = None
        resolver = getattr(self._config, "llm_client_resolver", None)
        if callable(resolver):
            llm_client = resolver()

        config = AgentConfig(
            task_id=str(request.task_id) if request.task_id is not None else None,
            workspace_path=request.workspace_path or self._config.workspace_path,
            model_name=request.model or self._config.model_name,
        )

        planner = EnhancedActionPlanner(config, llm_client=llm_client)
        self._planner = planner
        return planner

    def _build_action_input(self, request: ToolExecutionRequest):
        """Build Action input for planner, normalizing capability via CapabilityType.
        
        Uses the same normalization approach as GraphToolExecutor._resolve_action_type
        to ensure consistent capability → ActionType mapping.
        """
        from agent.models import Action

        capability = request.capability or "gather_info"
        
        # ActionType is only needed for Action object construction (legacy requirement)
        # It does NOT influence tool selection (that's done via CapabilityType)
        # Default to neutral GATHER_INFO to avoid biasing LLM prompts
        action_type = ActionType.GATHER_INFO
        
        # Try direct ActionType enum match only (no hardcoded mappings)
        try:
            action_type = ActionType(capability)
        except Exception:
            # Keep neutral default - tool selection happens via CapabilityType, not ActionType
            pass

        target = request.targets[0] if request.targets else ""
        reasoning = request.metadata.get("planner_reasoning", "")
        expected_outcome = request.metadata.get("expected_outcome", "")

        return Action(
            type=action_type,
            target=target,
            parameters={},
            reasoning=str(reasoning or ""),
            expected_outcome=str(expected_outcome or ""),
        )

    def _planner_context(
        self,
        request: ToolExecutionRequest,
        catalog: Iterable[ToolCatalogEntry],
        resolved_tools: Sequence[str],
    ) -> Dict[str, Any]:
        from dataclasses import asdict
        context = dict(request.metadata)
        context.setdefault("targets", list(request.targets))
        context.setdefault("catalog_snapshot", [asdict(entry) for entry in catalog])
        context.setdefault("history", list(request.history))
        context.setdefault("current_phase", context.get("current_phase", "enumeration"))
        context.setdefault("resolved_tools", list(resolved_tools))
        context.setdefault("task_id", request.task_id)
        # CRITICAL: Include user's actual message so planner knows intent
        context.setdefault("user_message", request.message)
        context.setdefault("working_memory", context.get("working_memory") or {})
        context.setdefault("working_memory_summary", context.get("working_memory_summary") or "")
        return context

    def _build_catalog(
        self,
        capability: str,
        metadata: Dict[str, Any],
        *,
        task_id: Optional[int] = None,
        history: Optional[Sequence[Dict[str, Any]]] = None,
        user_message: str = "",
    ) -> List[ToolCatalogEntry]:
        """Build tool catalog with relevance ranking, no hardcoded filtering.
        
        Uses cached metadata snapshot so first post-approval dispatch
        does not trigger full cold metadata scan.
        
        Industry Standard: Let LLM see all relevant tools and decide based on:
        - Tool schemas (rich Field descriptions)
        - User request context
        - Tool metadata
        
        No pre-vetoing by capability substring matching.
        """
        try:
            meta_snapshot = get_catalog_metadata_snapshot()
        except Exception:
            meta_snapshot = {}

        # Build catalog entries from cached metadata
        all_entries: List[ToolCatalogEntry] = []
        capability_lower = capability.lower() if capability else ""

        for tool_id, meta in meta_snapshot.items():
            if not is_tool_visible_in_catalog(tool_id):
                continue
            entry = ToolCatalogEntry(
                tool_id=tool_id,
                name=str(meta.get("name", tool_id)),
                category=str(meta.get("category", "")),
                description=str(meta.get("description", "")),
                # Populate ranking fields from metadata if available
                risk_level=meta.get("risk_level", 3),
                required_privileges=meta.get("required_privileges", []),
                minimum_budget_minutes=meta.get("estimated_runtime_minutes", 5),
                execution_priority=meta.get("execution_priority", 5),
            )
            all_entries.append(entry)

        # Artifact DB tools are hidden from LLM-facing catalogs by the shared
        # visibility policy. Do not run artifact exposure checks here; those can
        # query persisted artifact state and must remain outside catalog build.
        _ = history
        _ = task_id
        _ = user_message

        # Relevance ranking: prioritize category matches but don't exclude others
        if capability_lower:
            # Split into relevant and other
            relevant = [e for e in all_entries if capability_lower in e.category.lower()]
            other = [e for e in all_entries if capability_lower not in e.category.lower()]
            
            # Return relevant tools first, then a few from other categories
            # This gives LLM context without hardcoded filtering
            max_relevant = 10
            max_other = 5
            entries = relevant[:max_relevant] + other[:max_other]
        else:
            # No capability hint, return top N tools by some heuristic
            # For now, just limit total count
            entries = all_entries[:15]

        return entries

    def _summarise_result(self, result: Dict[str, Any]) -> str:
        status = str(
            result.get("status")
            or ("success" if result.get("success") else "error")
            or "unknown"
        ).strip().lower()
        tool_name = str(result.get("tool") or "tool").strip() or "tool"

        if status == "validation_error":
            return f"{tool_name} validation failed."
        if status == "rejected":
            return f"{tool_name} execution was rejected."
        if status == "success":
            return f"{tool_name} completed successfully."
        return f"{tool_name} execution failed."

    def _build_plan_cache_key(
        self,
        request: ToolExecutionRequest,
        resolved_tools: Sequence[str],
    ) -> str:
        metadata_snapshot = {
            "plan": request.metadata.get("plan"),
            "todo_list": request.metadata.get("todo_list"),
            "current_goal": request.metadata.get("current_goal"),
        }
        payload = {
            "capability": request.capability,
            "targets": list(request.targets),
            "message": request.message,
            "resolved_tools": list(resolved_tools),
            "metadata_snapshot": metadata_snapshot,
        }
        encoded = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _get_cached_plan(
        self,
        metadata: Dict[str, Any],
        cache_key: str,
    ) -> Optional[Dict[str, Any]]:
        cache = self._ensure_plan_cache(metadata)
        entry = cache.get(cache_key)
        if isinstance(entry, dict):
            return entry
        return None

    def _store_cached_plan(
        self,
        metadata: Dict[str, Any],
        cache_key: str,
        plan_data: Dict[str, Any],
    ) -> None:
        cache = self._ensure_plan_cache(metadata)
        cache[cache_key] = plan_data
        self._enforce_cache_capacity(cache)

    @staticmethod
    def _ensure_plan_cache(metadata: Dict[str, Any]) -> Dict[str, Any]:
        cache = metadata.get("planner_cache")
        if not isinstance(cache, dict):
            cache = {}
            metadata["planner_cache"] = cache
        return cache

    @staticmethod
    def _enforce_cache_capacity(cache: Dict[str, Any], *, max_entries: int = 8) -> None:
        while len(cache) > max_entries:
            cache.pop(next(iter(cache)))

    @staticmethod
    def _safe_inc_metric(metric_name: str) -> None:
        try:
            from backend.services.metrics.utils import safe_inc

            safe_inc(metric_name)
        except Exception:
            pass


async def run_tool_turn(
    coordinator: ToolExecutionCoordinator,
    request: ToolExecutionRequest,
) -> ToolExecutionOutcome:
    return await coordinator.run(request)


def run_tool_turn_sync(
    coordinator: ToolExecutionCoordinator,
    request: ToolExecutionRequest,
) -> ToolExecutionOutcome:
    return asyncio.run(coordinator.run(request))
