"""Concrete enhanced planner implementation.

This module contains the full ``EnhancedActionPlanner`` orchestration
logic for the direct-executor tool-planning stack. The public import
surface remains in ``agent.reasoning.enhanced_planner``, which
re-exports this implementation for backward compatibility.

Current-turn intent is sourced exclusively from the classifier-derived
``intent_brief`` (see
``backend/services/langgraph_chat/intent/briefs.py``). Tool selection
and parameter generation prompts do not read
``ConversationContextBundle`` transcript text — full-history access is
restricted to the intent classifier and deep-reasoning finalizer
(see ``docs/plans/intent_interpretation_wiring.md``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional
import logging

from agent.models import (
    Action,
    ExecutionResult,
    ActionPlan,
    ExecutionStrategy,
)
from core.llm import (
    LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
    LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC,
)
from agent.providers.llm.core.base import LLMClient
from agent.providers.llm.factory.client_factory import LLMClientFactory
from ..tools.parameter_generator import ContextualParameterGenerator
from ..tools.service_matcher import ServiceInventory, ServiceInfo
from ..tools.tool_call_specs import (
    build_function_tool_specs_for,
)
from ..tools.catalog_builder import build_full_tool_catalog
from ..tools.catalog_visibility import filter_visible_tool_ids
from ..tools.capability_surface import render_capability_surface
from ..tools.enhanced_tool_metadata import build_tool_catalog_entries
from ..tools.utils import aggregate_tool_results
from agent.tool_runtime.artifact_file_metadata import (
    build_artifact_file_metadata_for_prompt,
)
from .batch_commit import BatchCommitError, commit_tool_batch
from .batch_envelope import project_validated_envelope
from .llm_parameter_resolution import (
    PlannerToolParameterValidationError,
)
from .structured_contract_recovery import (
    StructuredContractViolationError,
    contains_retryable_llm_timeout,
    run_structured_contract_retry,
)
from .tool_selection_sentinel import (
    UNAVAILABLE_CAPABILITY_TOOL,
    selection_is_unavailable_capability,
)


def _resolve_llm_client_factory() -> type[LLMClientFactory]:
    """Resolve LLMClient factory via facade patch-point when available."""
    try:
        # Runtime lookup preserves monkeypatch compatibility on the facade module.
        from agent.reasoning import enhanced_planner as planner_facade

        patched_factory = getattr(planner_facade, "LLMClientFactory", None)
        if patched_factory is not None:
            return patched_factory
    except Exception:
        pass
    return LLMClientFactory


def _convert_usage_to_dict(usage: Any, source: str = "unknown") -> Optional[Dict[str, Any]]:
    """Convert UsageData-like payloads into ActionPlan usage-record dictionaries."""
    if usage is None:
        return None
    if hasattr(usage, "to_dict") and callable(usage.to_dict):
        try:
            result = usage.to_dict(source)
            if isinstance(result, dict):
                result["request_mode"] = "non_streaming"
            return result
        except Exception:
            pass

    def _build_payload(reader: Any) -> Dict[str, Any]:
        payload = {
            "prompt_tokens": reader("prompt_tokens", 0) or 0,
            "completion_tokens": reader("completion_tokens", 0) or 0,
            "total_tokens": reader("total_tokens", 0) or 0,
            "model": reader("model", "unknown"),
            "provider": reader("provider", "openai"),
            "cached_tokens": reader("cached_tokens", 0) or 0,
            "reasoning_tokens": reader("reasoning_tokens", 0) or 0,
            "api_surface": reader("api_surface", "unknown"),
            "cache_reporting": reader("cache_reporting", "unknown"),
            "request_mode": "non_streaming",
            "source": source,
        }
        provider_components = reader("provider_usage_components", None)
        serialized_components = _provider_usage_components_to_dict(provider_components)
        if serialized_components is not None:
            payload["provider_usage_components"] = serialized_components
        return payload

    if hasattr(usage, "prompt_tokens"):
        return _build_payload(lambda field, default: getattr(usage, field, default))
    if isinstance(usage, dict):
        return _build_payload(lambda field, default: usage.get(field, default))
    return None


def _provider_usage_components_to_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Return canonical provider usage components when present."""
    if value is None:
        return None
    if isinstance(value, dict):
        provider = value.get("provider")
        api_surface = value.get("api_surface")
        components = value.get("components")
        if (
            isinstance(provider, str)
            and isinstance(api_surface, str)
            and isinstance(components, dict)
        ):
            return {
                "provider": provider,
                "api_surface": api_surface,
                "components": dict(components),
            }
        return None
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        result = to_dict()
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def _is_retryable_planner_contract_error(exc: BaseException) -> bool:
    """Retry planner contract failures except terminal shell-parameter validation."""
    if contains_retryable_llm_timeout(exc):
        return True
    if isinstance(exc, PlannerToolParameterValidationError):
        return False
    return isinstance(exc, StructuredContractViolationError) and bool(getattr(exc, "retryable", False))


def _tool_identity_aliases(tool_id: str) -> set[str]:
    """Return exact and basename aliases for matching existing phase records."""
    normalized = str(tool_id or "").strip().lower()
    if not normalized:
        return set()
    aliases = {normalized}
    if "." in normalized:
        aliases.add(normalized.rsplit(".", 1)[-1])
    return aliases


def _unavailable_tool_aliases_from_context(context: Mapping[str, Any]) -> set[str]:
    """Derive current-turn unavailable tool aliases from runtime-owned context."""
    raw_tools = context.get("current_turn_unavailable_tools")
    if not isinstance(raw_tools, list):
        return set()

    aliases: set[str] = set()
    for tool_id in raw_tools:
        aliases.update(_tool_identity_aliases(str(tool_id or "")))
    return aliases


def _filter_unavailable_tools(
    tool_ids: List[str],
    *,
    unavailable_aliases: set[str],
) -> List[str]:
    """Remove current-turn tools known unavailable from the visible catalog."""
    if not unavailable_aliases:
        return tool_ids
    return [
        tool_id
        for tool_id in tool_ids
        if _tool_identity_aliases(tool_id).isdisjoint(unavailable_aliases)
    ]


class EnhancedActionPlanner:
    """Plan and execute actions using intelligent tool selection."""

    def __init__(self, config: Any, llm_client: LLMClient | None = None) -> None:
        self.config = config
        self.parameter_generator = ContextualParameterGenerator(config)
        # Phase 9 Task 9.1: legacy parallel tool-runner field retired — the
        # legacy `plan_and_execute_action` path uses an inline asyncio.gather
        # over `run_tool_by_name` (see that method). The runtime batch path
        # (BatchExecutor + BatchValidator) owns parallel-vs-sequential
        # admission for production tool dispatch; this planner-local fallback
        # is only exercised by direct callers of `plan_and_execute_action`.
        self.tool_execution_timeout = getattr(config, "tool_execution_timeout", None)
        self.service_inventory = ServiceInventory()
        self._log = logging.getLogger(__name__)
        # Initialize LLMClient; strict mode: no silent fallback
        self.llm_client: LLMClient | None = llm_client
        try:
            if self.llm_client is None and getattr(config, "openai_api_key", None):
                client_factory = _resolve_llm_client_factory()
                self.llm_client = client_factory.get_client(
                    api_key=config.openai_api_key,
                    model=getattr(config, "model_name", "gpt-5.2"),
                )
        except Exception:
            self.llm_client = None
        # Enforce: LLM must be available for planning (no fallback)
        if self.llm_client is None:
            raise RuntimeError("LLMClient is required for enhanced planning (no fallback mode)")
        from .llm_tool_selection import LLMToolSelector
        from .llm_parameter_resolution import LLMParameterResolver

        self._tool_selector = LLMToolSelector(self.llm_client, self.config, self._log)
        self._param_resolver = LLMParameterResolver(
            self.llm_client,
            self.config,
            self.parameter_generator,
            self._log,
        )

    @staticmethod
    def _safe_inc_metric(metric_name: str) -> None:
        try:
            from backend.services.metrics.utils import safe_inc

            safe_inc(metric_name)
        except Exception:
            pass

    async def plan_and_execute_action(
        self, action: Action, context: Dict[str, Any]
    ) -> ExecutionResult:
        """Plan and execute an action with optimal tool selection."""

        context = self._build_context(context)
        # Inject config-derived knobs into context for selectors
        try:
            max_tools = int(getattr(self.config, "max_tools_per_action", 3))
            context.setdefault("max_tools_per_action", max_tools)
        except Exception:
            context.setdefault("max_tools_per_action", 3)
        plan = await self.build_action_plan(action, context)

        # Materialize executions from the plan
        executions: List[Dict[str, Any]] = []
        tool_batch = getattr(plan, "tool_batch", None)
        tool_calls = list(getattr(tool_batch, "tool_calls", None) or [])
        if tool_calls:
            for call in tool_calls:
                executions.append(
                    {
                        "tool": call.tool_id,
                        "parameters": dict(call.parameters),
                        "action": action,
                    }
                )
        else:
            for tool_id in plan.selected_tools:
                params = dict(plan.tool_parameters.get(tool_id, {}))
                executions.append({"tool": tool_id, "parameters": params, "action": action})

        if not executions:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=f"No executable tools for action type: {action.type.value}",
                exit_code=-1,
            )

        # Execute according to strategy.
        # Phase 9 Task 9.1: legacy parallel tool-runner was retired. The
        # runtime tool-dispatch path (BatchExecutor + BatchValidator) is the
        # canonical multi-tool implementation; this planner-local helper uses
        # a small inline asyncio.gather because BatchExecutor is callback-
        # driven (mint/approval/projection per call) and would couple the
        # planner to runtime orchestrator state it does not have. The shared
        # primitive is `run_tool_by_name`.
        from ..tools.tool_registry import run_tool_by_name
        import asyncio as _asyncio

        async def _run_one(exe: Dict[str, Any]) -> Dict[str, Any]:
            try:
                coro = _asyncio.to_thread(run_tool_by_name, exe["tool"], exe["parameters"])
                if self.tool_execution_timeout and self.tool_execution_timeout > 0:
                    coro = _asyncio.wait_for(coro, timeout=self.tool_execution_timeout)
                result = await coro
                return {"tool": exe["tool"], "result": result}
            except Exception as exc:  # pragma: no cover - defensive
                return {"tool": exe["tool"], "result": exc}

        if plan.execution_strategy is ExecutionStrategy.SEQUENTIAL:
            results: List[Dict[str, Any]] = []
            for exe in executions:
                results.append(await _run_one(exe))
        else:
            results = await _asyncio.gather(*[_run_one(exe) for exe in executions])

        # Use shared aggregation; include findings for service inventory updates
        aggregated, findings = aggregate_tool_results(results, include_findings=True)
        self._update_service_inventory(findings)
        return aggregated

    async def build_action_plan(self, action: Action, context: Dict[str, Any]) -> ActionPlan:
        """Build a concrete ActionPlan using LLM-based tool selection.

        Delegates to _try_llm_action_plan for tool selection and parameter
        generation. Raises RuntimeError if LLM selection fails (strict mode,
        no fallback).
        """

        # Ensure selector respects config limits
        try:
            max_tools = int(getattr(self.config, "max_tools_per_action", 3))
            context = dict(context)
            context.setdefault("max_tools_per_action", max_tools)
        except Exception:
            pass

        # Strict mode: only LLM tool-calling path allowed.
        # Contract-invalid planner outputs are retried silently with a bounded
        # background policy.
        plan = await run_structured_contract_retry(
            operation=lambda: self._try_llm_action_plan(action, context),
            logger=self._log,
            stage="planner",
            contract="action_plan",
            max_attempts=2,
            backoff_seconds=0.25,
            is_retryable_error=_is_retryable_planner_contract_error,
        )
        if plan is None:
            raise RuntimeError("LLM tool selection failed (strict mode, no fallback)")
        return plan

    @staticmethod
    def _extract_user_message(context: Dict[str, Any]) -> str:
        """Extract the current-turn user message from planner context.

        The user-message text is still needed downstream by the tool-
        catalog resolver (artifact-exposure heuristics look at the raw
        user query). It is NOT a prompt-authority channel for the
        tool-planning prompts — those consume only
        ``intent_brief`` for current-turn interpretation.
        """
        return str(context.get("user_message", "") or "")

    @staticmethod
    def _extract_intent_brief(
        context: Dict[str, Any],
    ) -> Optional[Mapping[str, Any]]:
        """Resolve the classifier-derived ``intent_brief`` for prompts.

        Tool selection and parameter generation prompts read their
        current-turn intent from the brief exclusively. The planner
        service
        (``agent/graph/subgraphs/tool_execution_runtime/planner_service.py``)
        plumbs the brief into ``context`` as ``intent_brief``;
        missing / partial briefs are tolerated by the prompt builder,
        which renders ``"(none)"`` placeholders.
        """
        brief = context.get("intent_brief")
        if isinstance(brief, Mapping):
            return brief
        return None

    def _resolve_tool_catalog_for_llm(
        self,
        *,
        context: Dict[str, Any],
        user_message: str,
    ) -> List[str]:
        """Resolve, expose, and limit the planner tool catalog for LLM selection."""
        resolved_tools = context.get("resolved_tools", [])
        if not isinstance(resolved_tools, list):
            resolved_tools = list(resolved_tools or [])
        resolved_tools = [str(tool_id) for tool_id in resolved_tools if tool_id]
        if not resolved_tools:
            resolved_tools = build_full_tool_catalog(self.config, logger=self._log)

        if not resolved_tools:
            resolved_tools = build_full_tool_catalog(self.config, logger=self._log)
        if not resolved_tools:
            raise RuntimeError("No tools available for LLM selection")

        # Final LLM-facing visibility guard:
        # hidden tools remain implemented/callable for internal flows.
        resolved_tools = filter_visible_tool_ids(resolved_tools)
        unavailable_aliases = _unavailable_tool_aliases_from_context(context)
        if unavailable_aliases:
            before_filter = list(resolved_tools)
            resolved_tools = _filter_unavailable_tools(
                resolved_tools,
                unavailable_aliases=unavailable_aliases,
            )
            if self._log and len(resolved_tools) != len(before_filter):
                removed = [tool_id for tool_id in before_filter if tool_id not in resolved_tools]
                self._log.info(
                    "[PLANNER_CONTEXT] Excluding current-turn unavailable tools: %s",
                    removed,
                )
        if not resolved_tools:
            raise RuntimeError("No visible tools available for LLM selection")

        if context.get("selected_categories"):
            return resolved_tools
        max_tools_for_llm = int(getattr(self.config, "max_tools_exposed", 3))
        return resolved_tools[: max(1, max_tools_for_llm)]

    async def _try_llm_action_plan(self, action: Action, context: Dict[str, Any]) -> ActionPlan | None:
        """Attempt to have the LLM choose tools/count/strategy and params.

        Returns a validated ActionPlan or None if LLM is unavailable or fails.
        """
        default_strategy = getattr(self.config, "default_execution_strategy", "parallel").lower()
        default_exec = (
            ExecutionStrategy.PARALLEL if default_strategy != "sequential" else ExecutionStrategy.SEQUENTIAL
        )
        from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

        prompt_builder = ToolPlanningPromptBuilder()
        user_message = self._extract_user_message(context)
        intent_brief = self._extract_intent_brief(context)
        selection_system_prompt = prompt_builder.build_select_tools_system_prompt()
        limited_tool_list = self._resolve_tool_catalog_for_llm(context=context, user_message=user_message)
        tool_entries = build_tool_catalog_entries(limited_tool_list)
        capability_surface = render_capability_surface(limited_tool_list)
        max_tools = int(getattr(self.config, "max_tools_per_action", 3))
        max_committed = int(getattr(self.config, "max_committed_tools_per_batch", 1) or 1)
        parameter_system_prompt = prompt_builder.build_tool_parameters_system_prompt(
            max_committed_tools_per_batch=max_committed,
        )
        selection_prompt = prompt_builder.build_select_tools_prompt(
            resolved_tools=limited_tool_list,
            catalog=tool_entries,
            target=action.target,
            phase=context.get("current_phase", "enumeration"),
            constraints=context.get("constraints", {}),
            intent_brief=intent_brief,
            next_tool_hint=context.get("next_tool_hint"),
            latest_phase_memory=context.get("latest_phase_memory"),
            capability_surface=capability_surface,
            working_memory_summary=context.get("selection_working_memory_summary"),
            referenced_prior_turns=context.get("referenced_prior_turns"),
            selected_categories=list(context.get("selected_categories") or []),
            max_tools_per_action=max_tools,
            max_committed_tools_per_batch=max_committed,
        )
        selection_timeout_s = int(
            getattr(
                self.config,
                "llm_tool_selection_timeout",
                LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC,
            )
        )
        parameter_timeout_s = int(
            getattr(
                self.config,
                "tool_call_timeout",
                LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
            )
        )
        selection = await self._tool_selector.select_tools(
            system_prompt=selection_system_prompt,
            selection_prompt=selection_prompt,
            catalog=tool_entries,
            limited_tool_list=limited_tool_list,
            default_strategy=default_exec,
            max_tools=max_tools,
            timeout_s=selection_timeout_s,
        )
        if selection_is_unavailable_capability(selection.selected_tools):
            return ActionPlan(
                type=action.type,
                target=action.target,
                selected_tools=[UNAVAILABLE_CAPABILITY_TOOL],
                tool_parameters={},
                execution_strategy=selection.execution_strategy,
                reasoning=(
                    "Tool selector reported that no available tool or reasonable "
                    "substitute can satisfy the current tool intent."
                ),
                expected_outcome=(
                    "Return unavailable-capability context to post-tool reasoning."
                ),
                usage_records=[
                    record for record in [selection.usage_record] if record
                ],
                candidate_tools=[UNAVAILABLE_CAPABILITY_TOOL],
                tool_batch=None,
            )
        specs, fn_map = build_function_tool_specs_for(selection.selected_tools)
        artifact_file_metadata = build_artifact_file_metadata_for_prompt(
            selected_tools=selection.selected_tools,
            workspace_path=context.get("workspace_path"),
            artifact_refs=context.get("artifact_file_refs"),
        )
        parameters_prompt = prompt_builder.build_tool_parameters_prompt(
            selected_tools=selection.selected_tools,
            target=action.target,
            targets=list(context.get("targets") or []),
            execution_strategy=selection.execution_strategy.value,
            phase=context.get("current_phase", "enumeration"),
            constraints=context.get("constraints", {}),
            intent_brief=intent_brief,
            plan_text=context.get("plan_text"),
            current_goal=context.get("current_goal"),
            todo_list=(context.get("planner_metadata") or {}).get("todo_list")
            if isinstance(context.get("planner_metadata"), dict)
            else None,
            next_tool_hint=context.get("next_tool_hint"),
            previous_tool=context.get("previous_tool"),
            previous_tool_output_summary=context.get("previous_tool_output_summary"),
            working_memory_summary=context.get("working_memory_summary"),
            referenced_prior_turns=context.get("referenced_prior_turns"),
            max_committed_tools_per_batch=max_committed,
            artifact_file_metadata=artifact_file_metadata,
        )
        resolution = await self._param_resolver.resolve_parameters(
            system_prompt=parameter_system_prompt,
            parameters_prompt=parameters_prompt,
            selected_tools=selection.selected_tools,
            action=action,
            context=context,
            specs=specs,
            fn_map=fn_map,
            timeout_s=parameter_timeout_s,
        )
        usage_records = [record for record in [selection.usage_record] if record]
        usage_records.extend(resolution.usage_records)

        # Phase 3 Task 3.3: mint a ToolBatch from the validated commit so
        # ActionPlan.tool_batch carries the canonical batch contract.
        # selected_tools / tool_parameters are *derived projections* of the
        # batch (legacy single-tool consumers preserved until Phase 9).
        tool_batch = self._mint_tool_batch(selection, resolution, max_committed)
        legacy_tool_parameters: Dict[str, Dict[str, Any]] = {}
        for call in tool_batch.tool_calls:
            legacy_tool_parameters.setdefault(call.tool_id, dict(call.parameters))
        return ActionPlan(
            type=action.type,
            target=action.target,
            selected_tools=[c.tool_id for c in tool_batch.tool_calls],
            tool_parameters=legacy_tool_parameters,
            llm_tool_parameters=resolution.llm_tool_parameters,
            execution_strategy=tool_batch.requested_execution_strategy,
            reasoning=action.reasoning,
            expected_outcome=action.expected_outcome,
            usage_records=usage_records,
            candidate_tools=list(getattr(selection, "candidate_tools", None) or selection.selected_tools),
            tool_batch=tool_batch,
        )

    def _mint_tool_batch(self, selection, resolution, max_committed):
        """Project validated commit → ToolBatch (Phase 3 Task 3.3)."""
        execution_strategy = selection.execution_strategy
        envelope = getattr(resolution, "validated_builder_envelope", None)
        if envelope is None:
            envelope = project_validated_envelope(
                envelope=resolution.builder_envelope,
                selected_tools=selection.selected_tools,
                tool_parameters=resolution.tool_parameters,
            )
        candidates = list(getattr(selection, "candidate_tools", None) or selection.selected_tools)
        try:
            return commit_tool_batch(
                envelope,
                candidate_tool_ids=candidates,
                max_calls=max_committed,
                requested_execution_strategy=execution_strategy,
            )
        except BatchCommitError as exc:
            raise StructuredContractViolationError(
                error_code="structured_contract_semantic_validation",
                stage="batch_commit", contract="tool_batch", kind="semantic_validation_error",
                details=f"Batch commit rejected: {exc.reason}: {exc}",
                retryable=True, diagnostics={"reason": exc.reason},
            ) from exc

    def _aggregate_results(self, results: List[Dict[str, Any]], action: Action) -> ExecutionResult:
        # Retained for backward compatibility; delegate to shared helper
        aggregated, findings = aggregate_tool_results(results, include_findings=True)
        self._update_service_inventory(findings)
        return aggregated

    def _build_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich execution context with discovered services."""

        enriched = dict(context)
        if self.service_inventory.services:
            services_by_name: Dict[str, ServiceInfo] = {}
            for service in self.service_inventory.services.values():
                services_by_name.setdefault(service.name, service)
            enriched["discovered_services"] = services_by_name
            try:
                # Also provide a flat summary of open ports to guide planning
                ports = sorted({svc.port for svc in self.service_inventory.services.values() if isinstance(svc.port, int)})
                enriched["previous_open_ports_count"] = len(ports)
                enriched["previous_open_ports"] = ports[:50]
            except Exception:
                pass
        return enriched

    def _update_service_inventory(self, findings: List[Dict[str, Any]]) -> None:
        """Update service inventory based on tool findings."""

        for item in findings:
            name = item.get("service") or item.get("name")
            port = item.get("port")
            if not name or port is None:
                continue
            try:
                port = int(port)
            except (TypeError, ValueError):
                continue

            protocol = item.get("protocol", "tcp")
            service = ServiceInfo(
                name=name,
                port=port,
                protocol=protocol,
                version=item.get("version", ""),
                technology=item.get("technology", ""),
                banner=item.get("banner", ""),
            )
            self.service_inventory.add_service(service)


__all__ = ["EnhancedActionPlanner", "_convert_usage_to_dict"]
