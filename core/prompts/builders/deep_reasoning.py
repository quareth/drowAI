"""Prompt builders for plan-executor workflows backed by the deep reasoning graph."""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from core.prompts.base import ChatPromptBuilder
from core.prompts.builders._reasoning_context import compose_shared_reasoning_sections


class DeepReasoningPromptBuilder(ChatPromptBuilder):
    """Prompt builder used by plan-executor orchestrators."""

    def build_system_prompt(self, state: Mapping[str, object]) -> str:
        """Build system prompt for DR orchestration with context and budgets."""
        facts = (state.get("facts", {}) or {})
        
        current_goal = facts.get("current_goal") or "Complete the requested pentesting task"
        iteration = facts.get("iterations", 0)
        plan = facts.get("plan", [])
        
        # Extract budget information
        runtime_budgets = facts.get("runtime_budgets", {})
        remaining_iterations = runtime_budgets.get("remaining_iterations", "unlimited")
        remaining_tools = runtime_budgets.get("remaining_tool_calls", "unlimited")
        
        # Build tool reference
        tool_ids = facts.get("tool_ids", [])
        tool_count = len(tool_ids)
        
        prompt = f"""You are DrowAI's plan executor for penetration-testing workflows.

**Role**: Systematic security assessor capable of multi-step planning, tool orchestration, and iterative refinement.

**Current Status**:
- Iteration: {iteration}
- Current Goal: {current_goal}
- Remaining Iterations: {remaining_iterations}
- Remaining Tool Calls: {remaining_tools}
- Available Tools: {tool_count} pentesting tools

**Current Plan**:
{self._format_plan(plan)}

**Capabilities**:
1. Multi-step task decomposition and planning
2. Tool selection and orchestration
3. Observation-based reasoning (never hallucinate findings)
4. Iterative refinement based on results
5. Failure analysis and recovery

**Core Principles**:
- Base ALL reasoning on actual observations from tool outputs
- Update plans when new information emerges
- Recognize when goals are achieved or approaches aren't working
- Be systematic: plan → execute → observe → refine
- Stay within scope and budget constraints

**Decision Making**:
- Think more: When you need to reason about observations or refine strategy
- Call tool: When you need to gather information or test something
- Reflect: When stuck in a PATTERN (repeating same action, oscillating decisions, no strategic progress)
- Finalize: When goal achieved or budget exhausted

**Artifact Retrieval Policy**:
- If compact observations already resolve the question, do not request extra evidence.
- Artifact database lookup tools are internal and are not available for direct selection.
- When a saved workspace path is explicitly provided and a visible filesystem tool is available, read or search only the bounded slice needed to close a concrete evidence gap.

**Recovery Mechanisms**:
- Tool failures (network errors, timeouts, permissions): Handled automatically by post_tool_reasoning with immediate retry
- Stuck loops (action repetition, decision paralysis): Handled by reflect node for strategic revision
- DO NOT suggest reflect for individual tool failures - those are handled automatically

Always provide clear reasoning for your decisions."""

        return prompt
    
    def _format_plan(self, plan: list) -> str:
        """Format plan steps for display."""
        if not plan:
            return "No plan yet - needs initial planning"
        
        formatted = []
        for i, step in enumerate(plan, 1):
            formatted.append(f"{i}. {step}")
        return "\n".join(formatted)

    def build_decision_prompt(self, state: Mapping[str, object]) -> str:
        """Build decision prompt for routing to next action.

        Phase 4 narrowing: ``trace.scratchpad`` is a diagnostic rendering
        of runtime working memory and is NOT read as prompt-authority
        continuity here; routing decisions are grounded in the plan,
        todo list, recent observations, and recent tool results.
        """
        facts = (state.get("facts", {}) or {})
        trace = (state.get("trace", {}) or {})

        plan = facts.get("plan", [])
        todo_list = facts.get("todo_list", [])
        observations = trace.get("observations", [])
        executed_tools = trace.get("executed_tools", [])

        # Recent tool results
        recent_results = self._format_recent_tools(executed_tools[-2:] if executed_tools else [])

        prompt = f"""Based on current state, decide the next action in the plan executor loop.

**Current Plan**:
{self._format_plan(plan)}

**Todo List**:
{self._format_todo_list(todo_list)}

**Recent Observations**:
{self._format_observations(observations[-3:] if observations else [])}

**Recent Tool Results**:
{recent_results}

**Available Actions**:

1. **call_tool**: Execute a pentesting tool to gather information
   - Use when: Have a specific target/action identified, need data to proceed
   - Output: Tool execution results and observations
   - Note: Tool failures (network errors, timeouts, etc.) are handled AUTOMATICALLY by post_tool_reasoning

2. **think_more**: Reason further about findings before calling tools
   - Use when: New evidence requires deliberation before another tool call
   - Output: Updated reasoning trace; agent re-enters the decision loop

3. **reflect**: Analyze stuck patterns and generate strategic alternatives
   - Use when: Stuck in PATTERN (repeated same action 3+ times, oscillating between 2 decisions, no progress after multiple iterations)
   - Use when: Current STRATEGY isn't working (not individual tool failures)
   - Output: Strategic analysis and alternative approaches
   - DO NOT use for individual tool failures - those trigger automatic retry

4. **finalize**: Synthesize findings and provide final answer
   - Use when: Goal achieved, sufficient information gathered, or budget exhausted
   - Output: Final report/answer

**Decision Criteria**:
- Have I gathered enough information to answer?
- Is my current STRATEGY working? (not "did the last tool fail" - that's handled automatically)
- Am I making progress or stuck in a PATTERN?
- Do I have budget for more actions?

**Important**: Tool execution failures trigger immediate analysis and retry in post_tool_reasoning.
Only suggest "reflect" for strategic stuck loops, not individual tool failures.
When saved workspace evidence is required, use only visible filesystem tools and avoid full reads by default.

**Required Response Format**:
```json
{{
  "action": "call_tool" | "think_more" | "reflect" | "finalize",
  "reasoning": "Clear explanation of why this action is appropriate"
}}
```

Provide your decision as valid JSON."""

        return prompt
    
    def _format_todo_list(self, todo_list: list) -> str:
        """Format todo list for display."""
        if not todo_list:
            return "No items - all tasks complete or need planning"
        
        formatted = []
        for i, item in enumerate(todo_list, 1):
            if hasattr(item, "description"):
                text = item.description
            elif isinstance(item, dict) and "text" in item:
                text = item["text"]
            else:
                text = str(item)
            formatted.append(f"- {text}")
        return "\n".join(formatted)
    
    def _format_observations(self, observations: list) -> str:
        """Format recent observations."""
        if not observations:
            return "No observations yet"
        
        formatted = []
        for obs in observations:
            formatted.append(f"- {obs}")
        return "\n".join(formatted)
    
    def _format_recent_tools(self, tools: list) -> str:
        """Format recent tool executions."""
        if not tools:
            return "No tools executed yet"
        
        formatted = []
        for tool in tools:
            if isinstance(tool, dict):
                tool_id = tool.get("tool_id", "unknown")
                observation = tool.get("observation", "")
                formatted.append(f"• {tool_id}: {observation[:200]}...")
        return "\n".join(formatted)

    def build_tool_summary_prompt(self, tool_result: Mapping[str, object]) -> str:
        """Build prompt for summarizing tool outputs."""
        tool_name = tool_result.get("tool", "unknown_tool")
        compact_result = self._as_mapping(tool_result.get("compact_tool_result"))
        if not compact_result and any(
            key in tool_result for key in ("summary", "key_findings", "errors", "report_recommendations")
        ):
            compact_result = self._as_mapping(tool_result)

        summary = compact_result.get("summary") or tool_result.get("summary") or ""
        key_findings = self._as_list(compact_result.get("key_findings") or tool_result.get("key_findings"))
        errors = self._as_list(compact_result.get("errors") or tool_result.get("errors"))
        observation = tool_result.get("observation", "")
        output_text = "\n".join(key_findings) if key_findings else (summary or observation or "No output")
        if key_findings and not output_text.endswith("\n"):
            # Preserve legacy formatting parity (blank line before next section).
            output_text = f"{output_text}\n"
        error_text = "\n".join(errors) if errors else "None"
        
        prompt = f"""Summarize the following tool execution result for follow-up reasoning.

**Tool**: {tool_name}

**Output**:
{output_text}

**Errors** (if any):
{error_text}

**Task**: Extract key findings, important observations, and suggest next steps.

Provide a concise summary focusing on:
1. What was discovered
2. Notable findings or anomalies
3. Suggested follow-up actions"""

        return prompt

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        if isinstance(value, list):
            return [str(item) for item in value if item not in (None, "")]
        return []
    
    def build_think_more_prompt(
        self,
        state: Mapping[str, object],
        *,
        turn_sequence: Optional[int] = None,
        current_phase_sequence: Optional[int] = None,
        latest_recorded_phase_sequence: Optional[int] = None,
        relevant_findings: Optional[List[Mapping[str, Any]]] = None,
        capability_surface: str = "",
        environment_context: str = "",
    ) -> str:
        """Build prompt for the pure-reasoning ``think_more`` node.

        Phase 0 stripped legacy ``trace.observations`` / ``trace.executed_tools``
        prompt-authority reads. Phase 2 reprojects canonical runtime state into
        the ``think_more`` user prompt by composing existing reusable surfaces:
        ``derive_user_input_and_goal`` for User Input/User Goal,
        ``format_current_execution_context`` for the runtime turn/phase identity,
        ``render_phase_memory_section`` for the current-turn phase ledger,
        ``format_active_decision_hint`` for the advisory active decision,
        ``format_relevant_findings`` for relevant prior findings,
        ``format_environment_context`` for the container environment,
        ``extract_last_tool_sections`` for the last-tool cluster (Tool Executed,
        Tool Output Summary, Key Findings, Tool Errors, Structured Signals,
        Decision Evidence, Artifact References),
        ``format_request_contract`` for the request contract,
        ``format_plan`` / ``format_todos`` for plan/todo, and
        ``extract_scope_hint`` for scope hints. Every section except
        ``## Your Task`` is conditional and omitted when its body is empty.

        All new parameters are keyword-only and default to "off" so legacy call
        sites that pass only ``state`` continue to render the cleaned plan/todo/
        task-tail prompt. The wired ``think_more`` graph node supplies these
        kwargs; PTR composition is intentionally untouched.

        Args:
            state: Graph state mapping (or compatible view).
            turn_sequence: Canonical runtime-stamped turn ordinal supplied by
                the node; the builder never computes it.
            current_phase_sequence: Phase sequence the current think_more step
                is about to create, supplied by the node.
            latest_recorded_phase_sequence: Most recent phase already stored in
                the ledger for the active turn, supplied by the node.
            relevant_findings: Pre-selected target-scoped findings supplied by
                the node via ``build_relevant_findings_for_prompt``.
            capability_surface: Compact capability-family summary derived from
                the caller-visible tool set.
            environment_context: Preformatted environment section text supplied
                by the node via ``get_environment_full``.

        Returns:
            Composed user prompt string.
        """
        sections = compose_shared_reasoning_sections(
            state,
            turn_sequence=turn_sequence,
            current_phase_sequence=current_phase_sequence,
            latest_recorded_phase_sequence=latest_recorded_phase_sequence,
            relevant_findings=relevant_findings,
            capability_surface=capability_surface,
            environment_context=environment_context,
        )

        task_tail = """## Your Task
1. Analyze what we've learned so far
2. Determine if the plan needs updating based on new information
3. Identify the most important next step
4. Surface key observations to remember

**Guiding Questions**:
- What have we discovered?
- Does this change our approach?
- What's the logical next step?
- Are we making progress toward the goal?

**Required Response Format**:
```json
{
  "reasoning": "Your detailed analysis of the situation",
  "updated_plan": ["step 1", "step 2", ...],  // Updated plan if needed, or keep current plan
  "next_goal": "The immediate next objective",
  "key_observations": ["observation 1", "observation 2", ...]  // Key facts to remember
}
```

Provide your analysis as valid JSON."""

        sections.append(task_tail)

        intro = "Think deeply about the current situation and decide what to do next."
        return "\n\n".join([intro, *sections])


def build_deep_reasoning_prompt(state: Mapping[str, object]) -> str:
    """Convenience helper returning the system prompt."""

    return DeepReasoningPromptBuilder().build_system_prompt(state)


__all__ = ["DeepReasoningPromptBuilder", "build_deep_reasoning_prompt"]
