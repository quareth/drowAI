"""Post-tool reasoning prompt builder orchestration.

This module assembles post-tool prompts from structured runtime state
while delegating formatting and section rendering to focused helpers.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from agent.graph.memory.findings import format_relevant_findings
from agent.graph.utils import iteration_memory as _iteration_memory
from agent.graph.utils.todo_stall_guard import render_todo_stall_prompt_section
from core.prompts.builders._text import derive_user_input_and_goal
from core.prompts.route_labels import llm_facing_route_label

from ._formatting import (
    as_mapping,
    as_sequence,
    format_parameters,
    format_plan,
    format_sequence,
    format_todos,
    get_field,
)
from .last_tool import extract_last_tool_sections, iter_renderable_last_tool_sections
from .sections import (
    extract_scope_hint,
    format_active_decision_hint,
    format_current_execution_context,
    format_environment_context,
    format_intent_contract,
    format_request_contract,
    is_tool_visible,
)
from .templates import (
    ARTICULATION_SYSTEM_PROMPT,
    CVE_LOOKUP_GUIDANCE_TEXT,
    DIRECT_EXECUTOR_POLICY_TEXT,
    SYSTEM_PROMPT,
    TASK_INSTRUCTION_PROMPT,
)


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
        turn_sequence: Optional[int] = None,
        current_ptr_phase_sequence: Optional[int] = None,
        latest_recorded_phase_sequence: Optional[int] = None,
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

        Returns:
            Formatted user prompt string.
        """
        facts = as_mapping(get_field(interactive, "facts", {}))
        metadata = as_mapping(get_field(facts, "metadata", {}))

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

        if raw_capability == "simple_tool_execution":
            prompt_sections.append(
                "## Direct Executor Policy\n"
                f"{DIRECT_EXECUTOR_POLICY_TEXT}"
            )

        env_context = format_environment_context(environment_context)
        if env_context:
            prompt_sections.append(f"## Container Environment\n{env_context}")

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

        if is_tool_visible(metadata, "knowledge.cve_lookup"):
            prompt_sections.append(
                "## Selective CVE Lookup Guidance\n"
                f"{CVE_LOOKUP_GUIDANCE_TEXT}"
            )

        if failure_detected:
            failure_section = "## Failure Detected\n"
            failure_section += f"Tool execution failed with category: {failure_category}\n"
            failure_section += f"Retry attempts: {retry_count} of {max_retries}\n"
            if can_retry:
                failure_section += "Retry budget available - you may suggest retry with corrected approach.\n"
            else:
                failure_section += "Retry budget exhausted - consider reflect or finalize.\n"
            prompt_sections.append(failure_section)

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

        prompt_sections.append("## Your Task\n" + TASK_INSTRUCTION_PROMPT)
        return "\n\n".join(section for section in prompt_sections if section.strip())

    def build_articulation_system_prompt(self) -> str:
        """Return the system prompt for articulation text generation."""
        return ARTICULATION_SYSTEM_PROMPT

    def build_articulation_user_prompt(
        self,
        interactive: Any,
        synthesized: Mapping[str, Any],
        decision_output: Mapping[str, Any],
        *,
        relevant_findings: Optional[List[Mapping[str, Any]]] = None,
        environment_context: str = "",
    ) -> str:
        """Build user prompt for plain-text observation generation."""
        facts = as_mapping(get_field(interactive, "facts", {}))
        metadata = as_mapping(get_field(facts, "metadata", {}))

        user_input, derived_user_goal = derive_user_input_and_goal(facts)
        current_goal = str(get_field(facts, "current_goal", "") or "")
        tool_sections = extract_last_tool_sections(
            metadata,
            facts,
            synthesized,
            prefer_runtime_evidence=True,
        )

        next_action = decision_output.get("next_action", "unknown")
        action_reasoning = decision_output.get("action_reasoning", "No decision reasoning provided.")
        effective_next_goal = decision_output.get("effective_next_goal")
        user_goal_achieved = bool(decision_output.get("user_goal_achieved", False))
        failure_detected = bool(decision_output.get("failure_detected", False))
        retry_suggested = bool(decision_output.get("retry_suggested", False))
        failure_category = str(decision_output.get("failure_category", "unknown") or "unknown")
        tool_intent = as_mapping(decision_output.get("tool_intent"))
        tool_intent_lines: List[str] = []
        if tool_intent:
            description = str(tool_intent.get("description") or "").strip()
            target = tool_intent.get("target")
            focus = tool_intent.get("focus")
            if description:
                tool_intent_lines.append(f"tool_intent.description: {description}")
            if target not in (None, ""):
                tool_intent_lines.append(f"tool_intent.target: {target}")
            if focus not in (None, ""):
                tool_intent_lines.append(f"tool_intent.focus: {focus}")

        prompt_sections: List[str] = []
        if user_input:
            prompt_sections.append(f"## User Input\n{user_input}")
        if derived_user_goal:
            prompt_sections.append(f"## User Goal\n{derived_user_goal}")
        if current_goal:
            prompt_sections.append(f"## Current Focus\n{current_goal}")
        relevant_findings_text = format_relevant_findings(relevant_findings)
        if relevant_findings_text:
            prompt_sections.append(f"## Relevant Prior Findings\n{relevant_findings_text}")

        for heading, section_body in iter_renderable_last_tool_sections(
            tool_sections,
            keys=(
                "tool_executed",
                "tool_output_summary",
                "batch_tool_results",
                "key_findings",
                "tool_errors",
                "structured_signals",
                "decision_evidence",
                "compression_lossiness",
                "artifact_refs",
                "output_info",
            ),
        ):
            prompt_sections.append(f"## {heading}\n{section_body}")

        decision_context_lines = [
            f"next_action: {next_action}",
            f"action_reasoning: {action_reasoning}",
            *tool_intent_lines,
            f"user_goal_achieved: {user_goal_achieved}",
            f"failure_detected: {failure_detected}",
            f"failure_category: {failure_category}",
            f"retry_suggested: {retry_suggested}",
            f"effective_next_goal: {effective_next_goal or 'none'}",
        ]
        prompt_sections.append(
            "## Decision Context\n" + "\n".join(decision_context_lines)
        )

        env_context = format_environment_context(environment_context)
        if env_context:
            prompt_sections.append(f"## Container Environment\n{env_context}")

        prompt_sections.append(
            "## Task\n"
            "Generate a 2-4 sentence plain-text observation in first person.\n"
            "It should summarize what was learned and connect it to the chosen action."
        )
        return "\n\n".join(section for section in prompt_sections if section.strip())

    def _format_parameters(self, params: Mapping[str, Any]) -> str:
        return format_parameters(params)

    def _format_sequence(self, values: List[Any]) -> str:
        return format_sequence(values)

    def _format_todos(self, todo_list: List[Any]) -> str:
        return format_todos(todo_list)


__all__ = ["PostToolReasoningPromptBuilder"]
