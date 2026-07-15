"""LLM-based tool selection for the enhanced planner.

Handles the first LLM call in the planning pipeline: given a tool catalog
and action context, asks the LLM to propose candidate tools.
Parses the structured/text response, validates against the catalog,
deduplicates, enforces the configured candidate cap, and parses the
requested execution strategy. The downstream builder owns only the final
committed subset and concrete parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.llm import wait_for_with_timeout
from agent.models import ExecutionStrategy
from agent.providers.llm.core.exceptions import (
    LLMRefusalError,
    LLMResponseError,
    LLMStructuredOutputParseError,
)
from agent.providers.llm.contracts.recovery import (
    build_provider_recovery_diagnostics,
    log_provider_recovery_attempt,
)
from agent.reasoning.structured_contract_recovery import StructuredContractViolationError
from agent.reasoning.tool_selection_sentinel import (
    UNAVAILABLE_CAPABILITY_TOOL,
    is_unavailable_capability_tool,
)
from core.llm.json_extraction import extract_json_object

from .enhanced_planner import _convert_usage_to_dict


@dataclass
class ToolSelectionResult:
    """Result of the LLM tool-selection (candidate-generation) phase.

    During the migration window (Phases 2 → 3) the selector is being
    reframed as a *candidate generator*: it proposes candidate tools and
    the new builder commits a final batch. ``candidate_tools`` is the
    canonical post-migration field; ``selected_tools`` is kept as an
    alias populated identically so existing call sites compile unchanged.
    Phase 3 wires downstream consumers to ``candidate_tools``.
    """

    selected_tools: List[str]
    execution_strategy: ExecutionStrategy
    usage_record: Optional[Dict[str, Any]]
    reasoning: str = ""
    candidate_tools: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Mirror selected_tools into candidate_tools whenever the caller
        # did not pass it explicitly, so the alias is always populated.
        if self.candidate_tools is None:
            self.candidate_tools = list(self.selected_tools)


class LLMToolSelector:
    """Run and parse the planner's LLM tool-selection step."""

    def __init__(self, llm_client: Any, config: Any, logger: Optional[logging.Logger]) -> None:
        self.llm_client = llm_client
        self.config = config
        self._log = logger

    async def select_tools(
        self,
        *,
        system_prompt: str,
        selection_prompt: str,
        catalog: List[Dict[str, Any]],
        limited_tool_list: List[str],
        default_strategy: ExecutionStrategy,
        max_tools: int,
        timeout_s: int,
    ) -> ToolSelectionResult:
        """Select tools with LLM and return normalized selection output."""
        _ = (catalog, default_strategy)  # Explicit inputs for callsite/API parity.

        llm_response = await self._call_llm_with_recovery(
            system_prompt=system_prompt,
            selection_prompt=selection_prompt,
            timeout_s=timeout_s,
        )
        content = getattr(llm_response, "content", "")
        usage_record = _convert_usage_to_dict(
            getattr(llm_response, "usage", None),
            "planner_tool_selection",
        )

        plan_json = self._parse_selection_response(
            content=content,
            structured_payload=getattr(llm_response, "structured_output", None),
        )
        raw_selected_tools = plan_json.get("selected_tools") or []
        reasoning = str(plan_json.get("reasoning") or "").strip()
        execution_strategy = self._parse_execution_strategy(
            plan_json.get("execution_strategy")
        )
        if reasoning and self._log:
            self._log.debug("[PLANNER] Tool selector reasoning: %s", reasoning)

        selected_tools, filtered_out = self._validate_against_catalog(
            raw_selected_tools=raw_selected_tools,
            limited_tool_list=limited_tool_list,
        )

        if not selected_tools:
            if not limited_tool_list:
                raise RuntimeError("No tools resolved for selection. Tool catalog is empty.")
            raise StructuredContractViolationError(
                error_code="structured_contract_semantic_validation",
                stage="tool_selector",
                contract="tool_selector",
                kind="semantic_validation_error",
                details=(
                    "LLM tool selection failed: no selected tools matched catalog entries. "
                    f"Selected={raw_selected_tools!r} Filtered={filtered_out!r} "
                    f"CatalogSize={len(limited_tool_list)}"
                ),
                retryable=True,
                diagnostics={
                    "selected_tools": raw_selected_tools,
                    "filtered_out": filtered_out,
                    "catalog_size": len(limited_tool_list),
                },
            )

        selected_tools = self._deduplicate_and_trim(selected_tools, max_tools=max_tools)

        return ToolSelectionResult(
            selected_tools=selected_tools,
            execution_strategy=execution_strategy,
            usage_record=usage_record,
            reasoning=reasoning,
        )

    async def _call_llm_with_recovery(
        self,
        *,
        system_prompt: str,
        selection_prompt: str,
        timeout_s: int,
    ) -> Any:
        """Call structured-output selection; recover with plain-text fallback."""
        from core.llm.structured_schemas import TOOL_SELECTOR_STRUCTURED_OUTPUT

        try:
            return await wait_for_with_timeout(
                self.llm_client.chat_with_usage(
                    system_prompt,
                    selection_prompt,
                    temperature=0.1,
                    structured_output=TOOL_SELECTOR_STRUCTURED_OUTPUT,
                ),
                timeout_sec=timeout_s,
                component="PLANNER",
                operation="tool_selection_llm_call",
                logger=self._log or logging.getLogger(__name__),
                outcome="selection_timeout",
            )
        except LLMRefusalError:
            raise
        except (LLMStructuredOutputParseError, LLMResponseError) as structured_exc:
            if self._log:
                diagnostics = build_provider_recovery_diagnostics(structured_exc)
                log_provider_recovery_attempt(
                    structured_exc,
                    diagnostics,
                    target_logger=self._log,
                    log_prefix="PLANNER",
                )
            return await wait_for_with_timeout(
                self.llm_client.chat_with_usage(
                    system_prompt,
                    selection_prompt,
                    temperature=0.1,
                ),
                timeout_sec=timeout_s,
                component="PLANNER",
                operation="tool_selection_llm_call_fallback",
                logger=self._log or logging.getLogger(__name__),
                outcome="selection_timeout",
            )

    def _parse_selection_response(
        self,
        *,
        content: Any,
        structured_payload: Any,
    ) -> Dict[str, Any]:
        """Parse selection payload, preferring provider-validated output."""
        if isinstance(structured_payload, dict):
            return structured_payload
        try:
            raw_content = content if isinstance(content, str) else ""
            return extract_json_object(raw_content)
        except Exception as parse_exc:
            if self._log:
                self._log.warning(
                    "[PLANNER] JSON parse failed for tool selection response: %s. Content preview: %s",
                    parse_exc,
                    str(content)[:200] if content else "(empty)",
                )
            return {}

    def _parse_execution_strategy(self, raw_strategy: Any) -> ExecutionStrategy:
        """Parse selector-owned execution strategy from model output."""
        if isinstance(raw_strategy, ExecutionStrategy):
            return raw_strategy
        if not isinstance(raw_strategy, str) or not raw_strategy.strip():
            raise StructuredContractViolationError(
                error_code="structured_contract_schema_validation",
                stage="tool_selector",
                contract="tool_selector",
                kind="schema_validation_error",
                details="LLM tool selection failed: execution_strategy is required",
                retryable=True,
                diagnostics={"execution_strategy": raw_strategy},
            )

        normalized = raw_strategy.strip().lower()
        if normalized == "parallel":
            return ExecutionStrategy.PARALLEL
        if normalized == "sequential":
            return ExecutionStrategy.SEQUENTIAL

        raise StructuredContractViolationError(
            error_code="structured_contract_schema_validation",
            stage="tool_selector",
            contract="tool_selector",
            kind="schema_validation_error",
            details=(
                "LLM tool selection failed: execution_strategy must be "
                "'sequential' or 'parallel'"
            ),
            retryable=True,
            diagnostics={"execution_strategy": raw_strategy},
        )

    def _validate_against_catalog(
        self,
        *,
        raw_selected_tools: Any,
        limited_tool_list: List[str],
    ) -> Tuple[List[str], List[Any]]:
        """Validate raw selection values against catalog with robust matching."""
        if not isinstance(raw_selected_tools, list):
            return [], []

        selected_tools: List[str] = []
        filtered_out: List[Any] = []
        catalog_lookup = {tool_id.strip().lower(): tool_id for tool_id in limited_tool_list}
        sentinel_seen = False
        non_sentinel_seen = False

        for raw_tool in raw_selected_tools:
            candidate = raw_tool
            if isinstance(candidate, dict):
                candidate = candidate.get("tool_name") or candidate.get("name") or ""
            if not isinstance(candidate, str) or not candidate:
                continue

            normalized = candidate.strip().lower()
            if is_unavailable_capability_tool(normalized):
                sentinel_seen = True
                selected_tools.append(UNAVAILABLE_CAPABILITY_TOOL)
            elif normalized in catalog_lookup:
                non_sentinel_seen = True
                selected_tools.append(catalog_lookup[normalized])
            elif candidate.strip() in limited_tool_list:
                non_sentinel_seen = True
                selected_tools.append(candidate.strip())
            else:
                non_sentinel_seen = True
                filtered_out.append(raw_tool)

        if sentinel_seen and non_sentinel_seen:
            raise StructuredContractViolationError(
                error_code="structured_contract_semantic_validation",
                stage="tool_selector",
                contract="tool_selector",
                kind="semantic_validation_error",
                details=(
                    "LLM tool selection failed: unavailable_capability must be "
                    "the only selected tool when used."
                ),
                retryable=True,
                diagnostics={
                    "selected_tools": raw_selected_tools,
                    "sentinel": UNAVAILABLE_CAPABILITY_TOOL,
                },
            )

        if filtered_out and self._log:
            self._log.warning(
                "[PLANNER] Tools filtered out (not in catalog): %s. Catalog has %s tools. "
                "First 10 catalog tools: %s",
                filtered_out,
                len(limited_tool_list),
                limited_tool_list[:10],
            )

        return selected_tools, filtered_out

    def _deduplicate_and_trim(self, selected_tools: List[str], *, max_tools: int) -> List[str]:
        """Deduplicate candidate tools and enforce the configured candidate cap.

        Phase 3 Task 3.1.5 lifted the legacy single-tool trim. The cap now
        comes from ``AgentConfig.max_tools_per_action`` (forwarded by the
        planner as ``max_tools``); the selector only enforces the bound and
        does not invent its own number.
        """
        if max_tools < 1:
            raise ValueError("max_tools must be >= 1")
        deduped_tools: List[str] = []
        seen_tools: set[str] = set()

        for tool_id in selected_tools:
            if tool_id in seen_tools:
                continue
            seen_tools.add(tool_id)
            deduped_tools.append(tool_id)

        if len(deduped_tools) != len(selected_tools) and self._log:
            self._log.info(
                "[PLANNER] Deduplicated candidate tools from %s to %s",
                selected_tools,
                deduped_tools,
            )

        enforced_cap = max_tools
        if len(deduped_tools) > enforced_cap and self._log:
            self._log.info(
                "[PLANNER] Trimming candidate selection to cap=%d for this turn: %s -> %s",
                enforced_cap,
                deduped_tools,
                deduped_tools[:enforced_cap],
            )
        return deduped_tools[:enforced_cap]


__all__ = ["LLMToolSelector", "ToolSelectionResult"]
