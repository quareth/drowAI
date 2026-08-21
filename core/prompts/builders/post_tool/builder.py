"""Post-tool reasoning prompt builder orchestration.

This module assembles post-tool prompts from structured runtime state
while delegating formatting and section rendering to focused helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, List, Mapping, Optional

from agent.graph.memory.findings import format_relevant_findings
from agent.graph.utils import iteration_memory as _iteration_memory
from agent.graph.utils.todo_stall_guard import render_todo_stall_prompt_section
from core.prompts.builders._text import derive_user_input_and_goal
from core.prompts.route_labels import llm_facing_route_label
from core.prompts.builders.subagent_catalog import render_subagent_catalog_section
from core.skills.contracts import SubagentSkillCatalog

from ._formatting import (
    as_mapping,
    as_sequence,
    format_parameters,
    format_plan,
    format_sequence,
    format_todos,
    get_field,
)
from .evidence import EvidenceView
from .last_tool import extract_last_tool_sections, iter_renderable_last_tool_sections
from .sections import (
    extract_scope_hint,
    format_active_decision_hint,
    format_active_handoff_runs_context,
    format_current_execution_context,
    format_environment_context,
    format_handoff_batch_context,
    format_handoff_coordination_contract,
    format_intent_contract,
    format_request_contract,
    is_tool_visible,
)
from .templates import (
    CVE_LOOKUP_GUIDANCE_TEXT,
    DIRECT_EXECUTOR_POLICY_TEXT,
    SYSTEM_PROMPT,
    TASK_INSTRUCTION_PROMPT,
)

DIRECT_TOOL_OUTCOME_SOURCE = "direct_tool"
SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE = "subagent_handoff_batch"


class PostToolReasoningPromptBuilder:
    """Build prompts for post-tool decision output.

    This builder gathers current-turn context from state (user goal, tool
    output, and runtime phase memory) and formats it into prompts for
    post-tool reasoning.
    """

    def build_system_prompt(self) -> str:
        """Return the system prompt for post-tool reasoning.

        Returns:
            The system prompt string that defines the LLM's role and output format.
        """
        return SYSTEM_PROMPT

    def build_user_prompt(
        self,
        interactive: Any,
        synthesized: Mapping[str, Any],
        *,
        relevant_findings: Optional[List[Mapping[str, Any]]] = None,
        failure_context: Optional[Mapping[str, Any]] = None,
        environment_context: str = "",
        capability_surface: str = "",
        outcome_source: str = DIRECT_TOOL_OUTCOME_SOURCE,
        turn_sequence: Optional[int] = None,
        current_ptr_phase_sequence: Optional[int] = None,
        latest_recorded_phase_sequence: Optional[int] = None,
        evidence: Optional[EvidenceView] = None,
        subagent_catalog: Sequence[Mapping[str, Any]] = (),
        skill_catalogs: Sequence[SubagentSkillCatalog] = (),
    ) -> str:
        """Build the user prompt with full context.

        Args:
            interactive: State-like object (mapping or object with `facts`/`trace`).
            synthesized: Synthesized tool output from tool_synthesizer node.
            Expected keys: tool, summary, key_findings.
            failure_context: Precomputed failure metadata from caller node.
            environment_context: Preformatted environment section text.
            capability_surface: Compact capability-family summary derived from
                the caller-visible tool set.
            turn_sequence: Runtime-stamped canonical turn ordinal for the
                current request lifecycle. Supplied by the PTR node; the
                builder never computes it.
            current_ptr_phase_sequence: Phase sequence that this PTR step is
                about to create. Supplied by the PTR node via
                :func:`iteration_memory.peek_next_phase_sequence`; the
                builder never computes it.
            latest_recorded_phase_sequence: Most recent phase already stored
                in the ledger for the active turn. Supplied by the PTR node
                via :func:`iteration_memory.latest_recorded_phase_sequence`;
                the builder never computes it.
            outcome_source: Deterministic source metadata resolved by the PTR/PAR
                node. The default preserves direct-tool prompt compatibility.
            evidence: Caller-selected compact evidence for this reasoning turn.
                Passing it prevents the builder from independently restoring
                transient rows from the same-process runtime batch.

        Returns:
            Formatted user prompt string.
        """
        facts = as_mapping(get_field(interactive, "facts", {}))
        metadata = as_mapping(get_field(facts, "metadata", {}))
        is_handoff_batch = outcome_source == SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE

        user_input, derived_user_goal = derive_user_input_and_goal(facts)
        current_goal = str(get_field(facts, "current_goal", "") or "")
        raw_capability = str(
            get_field(facts, "capability", "simple_tool_execution")
            or "simple_tool_execution"
        ).lower()
        capability = llm_facing_route_label(raw_capability)

        tool_sections = extract_last_tool_sections(
            metadata,
            facts,
            synthesized,
            prefer_runtime_evidence=True,
            evidence_override=evidence,
        )

        failure_ctx = as_mapping(failure_context or {})
        failure_detected = bool(failure_ctx.get("failure_detected", False))
        failure_category = str(failure_ctx.get("failure_category", "unknown") or "unknown")
        retry_count = int(failure_ctx.get("retry_count", 0) or 0)
        can_retry = bool(failure_ctx.get("can_retry", False))
        max_retries = int(failure_ctx.get("max_retries", 0) or 0)

        plan_summary = format_plan(as_sequence(get_field(facts, "plan", [])))
        todo_summary = format_todos(as_sequence(get_field(facts, "todo_list", [])))
        scope_hint = extract_scope_hint(metadata)
        active_decision_hint = format_active_decision_hint(metadata)

        prompt_sections: List[str] = []
        if user_input:
            prompt_sections.append(f"## User Input\n{user_input}")
        if derived_user_goal:
            prompt_sections.append(f"## User Goal\n{derived_user_goal}")
        prompt_sections.append(f"## Execution Capability\n{capability}")

        execution_context_section = format_current_execution_context(
            turn_sequence=turn_sequence,
            current_phase_sequence=current_ptr_phase_sequence,
            latest_recorded_phase_sequence=latest_recorded_phase_sequence,
        )
        if execution_context_section:
            prompt_sections.append(
                f"## Current Execution Context\n{execution_context_section}"
            )
        prompt_sections.append(f"## Outcome Source\n{outcome_source}")

        phase_memory_section = _iteration_memory.render_phase_memory_section(
            dict(metadata),
            turn_sequence=turn_sequence,
        )
        if phase_memory_section:
            prompt_sections.append(phase_memory_section)

        if current_goal:
            prompt_sections.append(f"## Current Focus\n{current_goal}")
        if active_decision_hint:
            prompt_sections.append(f"## Prior Active Decision (Advisory)\n{active_decision_hint}")
        relevant_findings_text = format_relevant_findings(relevant_findings)
        if relevant_findings_text:
            prompt_sections.append(f"## Relevant Prior Findings\n{relevant_findings_text}")
        capability_surface_text = str(capability_surface or "").strip()
        if capability_surface_text:
            prompt_sections.append(
                f"## Agent Operational Capability Surface\n{capability_surface_text}"
            )

        if raw_capability == "simple_tool_execution" and not is_handoff_batch:
            prompt_sections.append(
                "## Direct Executor Policy\n"
                f"{DIRECT_EXECUTOR_POLICY_TEXT}"
            )

        env_context = format_environment_context(environment_context)
        if env_context:
            prompt_sections.append(f"## Container Environment\n{env_context}")

        if is_handoff_batch:
            completed_handoffs = format_handoff_batch_context(metadata)
            if completed_handoffs:
                prompt_sections.append(
                    f"## Completed Bounded Subagent Results\n{completed_handoffs}"
                )
            active_handoffs = format_active_handoff_runs_context(metadata)
            if active_handoffs:
                prompt_sections.append(
                    f"## Relevant Active Subagent Assignments\n{active_handoffs}"
                )
            prompt_sections.append(
                "## Parent Handoff Coordination Contract\n"
                f"{format_handoff_coordination_contract()}"
            )
        else:
            for heading, section_body in iter_renderable_last_tool_sections(
                tool_sections,
                keys=("tool_executed",),
            ):
                prompt_sections.append(f"## {heading}\n{section_body}")

        intent_contract = metadata.get("intent_contract_evaluation")
        intent_contract_section = format_intent_contract(intent_contract)
        if intent_contract_section:
            prompt_sections.append(f"## Intent Contract Check\n{intent_contract_section}")
        request_contract_section = format_request_contract(metadata.get("request_contract"))
        if request_contract_section:
            prompt_sections.append(f"## Request Contract\n{request_contract_section}")

        if not is_handoff_batch:
            for heading, section_body in iter_renderable_last_tool_sections(
                tool_sections,
                keys=(
                    "tool_output_summary",
                    "batch_tool_results",
                    "key_findings",
                    "tool_errors",
                    "structured_signals",
                    "decision_evidence",
                    "compression_lossiness",
                    "artifact_refs",
                ),
            ):
                prompt_sections.append(f"## {heading}\n{section_body}")

        if not is_handoff_batch and is_tool_visible(metadata, "knowledge.cve_lookup"):
            prompt_sections.append(
                "## Selective CVE Lookup Guidance\n"
                f"{CVE_LOOKUP_GUIDANCE_TEXT}"
            )

        if failure_detected and not is_handoff_batch:
            failure_section = "## Failure Detected\n"
            failure_section += f"Tool execution failed with category: {failure_category}\n"
            failure_section += f"Retry attempts: {retry_count} of {max_retries}\n"
            if can_retry:
                failure_section += "Retry budget available - you may suggest retry with corrected approach.\n"
            else:
                failure_section += "Retry budget exhausted - consider reflect or finalize.\n"
            prompt_sections.append(failure_section)

        if not is_handoff_batch:
            for heading, section_body in iter_renderable_last_tool_sections(
                tool_sections,
                keys=("output_info",),
            ):
                prompt_sections.append(f"## {heading}\n{section_body}")

        if plan_summary:
            prompt_sections.append(f"## Current Plan\n{plan_summary}")

        if todo_summary:
            prompt_sections.append(f"## Todo List\n{todo_summary}")

        todo_stall_section = render_todo_stall_prompt_section(metadata)
        if todo_stall_section:
            prompt_sections.append(f"## Active Todo Stall Guard\n{todo_stall_section}")

        if scope_hint:
            prompt_sections.append(f"## Scope Hints\n{scope_hint}")

        if subagent_catalog:
            prompt_sections.append(
                "## Available Subagent Handoffs\n"
                + render_subagent_catalog_section(subagent_catalog, skill_catalogs)
            )

        prompt_sections.append("## Your Task\n" + TASK_INSTRUCTION_PROMPT)
        return "\n\n".join(section for section in prompt_sections if section.strip())

    def _format_parameters(self, params: Mapping[str, Any]) -> str:
        return format_parameters(params)

    def _format_sequence(self, values: List[Any]) -> str:
        return format_sequence(values)

    def _format_todos(self, todo_list: List[Any]) -> str:
        return format_todos(todo_list)


__all__ = ["PostToolReasoningPromptBuilder"]
