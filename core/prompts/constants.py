"""Centralized prompt constants and helpers.

This module is the single import surface for:
- Prompt-related numeric limits and validation constants
- Shared prompt strings and prompt-building helpers used by LangGraph nodes
- Versioned prompt templates (loaded from `core/prompts/versions/*`)

Active prompt code should import from `core.prompts.*`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from core.tool_category_taxonomy import TOOL_CATEGORY_SELECTION_GUIDANCE_TEXT

from .loader import TemplateLoader

# -----------------------------------------------------------------------------
# Versioned templates
# -----------------------------------------------------------------------------

_VERSIONS_ROOT = Path(__file__).resolve().parent / "versions"
_TEMPLATE_LOADER = TemplateLoader(_VERSIONS_ROOT)

# Keep these names to match legacy import sites.
CLASSIFIER_SYSTEM_PROMPT = _TEMPLATE_LOADER.load_latest_version(
    "intent", "intent_classifier.txt"
)
PROMPT_TEMPLATE = _TEMPLATE_LOADER.load_latest_version("intent", "prompt_template.txt")
SIMPLE_CHAT_DEFAULT_SYSTEM_PROMPT = _TEMPLATE_LOADER.load_latest_version(
    "simple_chat", "system.txt"
)


def _render_latest_template(family: str, filename: str, **context: Any) -> str:
    """Render the latest versioned template with ``str.format_map`` semantics."""

    template = _TEMPLATE_LOADER.load_latest_version(family, filename)
    safe_context: Mapping[str, str] = {
        key: "" if value is None else str(value) for key, value in context.items()
    }
    return template.format_map(safe_context)

# Shared label for the unified conversation section used by every
# prompt-authoritative role that renders transcript text via the
# shared serializer. Python-side consumers compose their own
# surrounding formatting (bare label, ``##`` header, ``**bold**``,
# etc.) around this constant. ``.txt`` templates keep the literal
# text inline because the template loader does no Python-constant
# interpolation on non-template tokens.
CONVERSATION_SECTION_LABEL = (
    "Conversation (oldest -> newest, act on the turn tagged latest=true)"
)

# -----------------------------------------------------------------------------
# Prompt Builder Limits (used in post-tool reasoning builders)
# -----------------------------------------------------------------------------

# Generic limits shared by prompt helpers.
# NOTE: Per-memory/prompt char budgets were scaled 4x (v2026-04-14) to
# keep richer tool output / memory detail available to downstream
# prompts. Adjust these knobs in one place; do not re-introduce smaller
# local truncations in prompt builders.
MAX_PARAM_CHARS = 960

MAX_SUMMARY_CHARS = 1600
MAX_PLAN_CHARS = 1600
MAX_TODO_CHARS = 2400
MAX_STDOUT_EXCERPT_CHARS = 24000

MAX_TODOS_IN_PROMPT = 10

# Post-tool prompt-specific limits.
POST_TOOL_MAX_PARAM_CHARS = 960
POST_TOOL_MAX_STDOUT_EXCERPT_CHARS = 6000
POST_TOOL_MAX_STDERR_EXCERPT_CHARS = 2000
POST_TOOL_MAX_SUMMARY_CHARS = 4000
POST_TOOL_MAX_PLAN_CHARS = 1600
POST_TOOL_MAX_TODO_CHARS = 4000
POST_TOOL_MAX_TODOS_IN_PROMPT = 10
POST_TOOL_MAX_DECISION_RATIONALE_CHARS = 660

# Tool-projection limits (compact summary carried into tool_execution_history
# and the iteration-memory ledger).
TOOL_RESULT_SUMMARY_MAX_CHARS = 600

# Compact-envelope limits (produced by agent.context.tool_processor and
# agent.graph.compression.compressor when the LLM compressor is bypassed
# or falls back to deterministic summarization).
#
# These caps shape the fields of CompactToolOutput *before* they flow into
# the downstream prompt builders. Every cap below MUST stay <=
# POST_TOOL_MAX_SUMMARY_CHARS so the prompt-layer cap never becomes the
# binding constraint on upstream deterministic content.
COMPACT_SUMMARY_MAX_CHARS = 2000
COMPACT_FINDING_MAX_CHARS = 500
COMPACT_RULE_FINDING_MAX_CHARS = 400
COMPACT_ERROR_LEAD_MAX_CHARS = 1000
COMPACT_FAILURE_FINDING_MAX_CHARS = 500
COMPACT_FAILURE_STDOUT_LINE_MAX_CHARS = 500
COMPACT_ERROR_CONTEXT_MESSAGE_MAX_CHARS = 1000
COMPACT_DECISION_EVIDENCE_MAX_CHARS = 1000
COMPACT_ERROR_ENTRY_MAX_CHARS = 2000


# -----------------------------------------------------------------------------
# History/Memory Limits (used in post_tool_reasoning.py node)
# -----------------------------------------------------------------------------

MAX_HISTORY_ENTRIES = 120  # Total entries in conversation history
MAX_HISTORY_CONTENT_CHARS = 8000  # Per-entry truncation limit (4x)
MAX_PRIOR_WORK_ENTRIES = 6  # Recent observations to show as "prior work"


# -----------------------------------------------------------------------------
# LLM Call Parameters
# -----------------------------------------------------------------------------

MAX_REASONING_TOKENS = 800


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------

VALID_POST_TOOL_ACTIONS = frozenset({"call_tool", "think_more", "reflect", "finalize"})
VALID_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "skipped"})


# -----------------------------------------------------------------------------
# Shared prompt strings and prompt builders (node-level prompts)
# -----------------------------------------------------------------------------

KALI_CONTAINER_CAPABILITIES = """You are running inside a Kali Linux container with full access to:
- Pentesting tools (nmap, masscan, etc.)
- Shell/terminal commands (you can run any command to gather information)"""


def build_planner_system_prompt(env_prompt: str) -> str:
    """Build the planner system prompt, optionally including environment info."""

    return _render_latest_template(
        "deep_reasoning_planner",
        "system.txt",
        kali_container_capabilities=KALI_CONTAINER_CAPABILITIES,
        environment_context=(
            f"{env_prompt}\n"
            "Use this network configuration to inform your planning. You know your "
            "container's network position."
            if env_prompt
            else "You do not know your network configuration until you check it."
        ),
    )


def build_planner_history_section(formatted_history: str) -> str:
    return f"""

**{CONVERSATION_SECTION_LABEL}**:
{formatted_history}

Use this conversation context to understand the latest user request and how earlier turns shape it."""


def build_planner_tools_constraint(tools_list: str) -> str:
    return f"""

**Available Tools**: {tools_list}
**Important**: Only plan steps that can be executed with the available tools listed above.
If a tool is not in the available tools list, do not include it in your plan."""


def build_scope_boundary_warnings(boundaries: Sequence[str]) -> str:
    if not boundaries:
        return ""

    warnings = "\n\n**CRITICAL RESTRICTIONS - DO NOT VIOLATE**:\n"
    for boundary in boundaries:
        if boundary == "no_exploitation":
            warnings += "- DO NOT include exploitation, attack, penetration, or payload steps\n"
            warnings += "- DO NOT use tools like Metasploit for exploitation\n"
        elif boundary == "no_brute_force":
            warnings += "- DO NOT include brute force, password cracking, or dictionary attacks\n"
        elif boundary == "no_dos":
            warnings += "- DO NOT include DoS attacks, flooding, or service overload\n"
        elif boundary == "no_data_modification":
            warnings += "- DO NOT include data modification, deletion, or writing steps\n"
    return warnings


def build_planner_scope_constraints(
    *,
    goals_str: str,
    boundaries_str: str,
    conditional_str: str,
    explicit_tools_str: str,
    boundary_warnings: str,
) -> str:
    return f"""

**Scope Constraints**:
- User explicitly requested: {goals_str}
- User explicitly forbade: {boundaries_str}
- Conditional targets: {conditional_str}
- Explicit tools mentioned: {explicit_tools_str}{boundary_warnings}

**IMPORTANT**: Your plan must:
- Address all requested goals ({goals_str})
- NEVER include forbidden actions listed above
- Use explicit tools if mentioned ({explicit_tools_str})
- Consider conditional targets for fallback scenarios ({conditional_str})"""


def _render_dr_planner_brief_block(brief: Mapping[str, Any]) -> str:
    """Render the ``intent_brief`` as a compact structured block.

    The block is authored as the sole classifier-derived context input
    for the deep-reasoning planner. It replaces the previous
    recent-transcript dependence in ``build_planning_prompt`` and
    intentionally omits tool ids, execution strategy, and parameter
    payloads (those are owned by downstream execution roles). The
    planner also keeps its own authority over plan / todo_list /
    current_goal / clarify-gate lifecycle — the brief is intent/
    direction/constraints/target context, not planner output.

    An empty / missing brief still renders a valid block — every field
    falls back to ``"(none)"`` so the prompt shape stays stable.
    """
    readiness = _render_brief_value(brief.get("execution_readiness"))
    blocking_reason = _render_brief_value(brief.get("blocking_reason"))
    readiness_line = f"- Execution readiness: {readiness}"
    if readiness == "blocked" and blocking_reason != _BRIEF_NONE_MARKER:
        readiness_line += f" (blocked: {blocking_reason})"

    request_contract = brief.get("request_contract") or {}
    if not isinstance(request_contract, Mapping):
        request_contract = {}

    lines: List[str] = [
        "DR Planner Input Brief (classifier-derived; transcript access is not available here):",
        f"- Resolved user intent: {_render_brief_value(brief.get('resolved_user_intent'))}",
        f"- Overall goal: {_render_brief_value(brief.get('overall_goal'))}",
        f"- Continuation mode: {_render_brief_value(brief.get('continuation_mode'))}",
        f"- Resolved step title: {_render_brief_value(brief.get('resolved_step_title'))}",
        f"- Resolved step detail: {_render_brief_value(brief.get('resolved_step_detail'))}",
        f"- Next operational goal: {_render_brief_value(brief.get('next_operational_goal'))}",
        f"- Success condition: {_render_brief_value(brief.get('success_condition'))}",
        readiness_line,
        f"- Resolved target: {_render_brief_value(_resolve_brief_target_field(brief, 'resolved_target'))}",
        f"- Target status: {_render_brief_value(_resolve_brief_target_field(brief, 'target_status'))}",
        f"- Target source: {_render_brief_value(_resolve_brief_target_field(brief, 'target_source'))}",
        "- Explicit constraints:",
        _render_brief_bullets(brief.get("explicit_constraints")),
        "- Relevant memory fragments:",
        _render_brief_bullets(brief.get("relevant_memory_fragments")),
        "- Retrieval hints:",
        _render_brief_bullets(brief.get("retrieval_hints")),
        "- Request contract:",
        f"  - question_type: {_render_brief_value(request_contract.get('question_type'))}",
        f"  - answer_style: {_render_brief_value(request_contract.get('answer_style'))}",
        f"  - terminal_when: {_render_brief_value(request_contract.get('terminal_when'))}",
    ]
    return "\n".join(lines)


def build_planner_brief_section(
    intent_brief: Optional[Mapping[str, Any]] = None,
) -> str:
    """Wrap the DR planner brief block in a planner-prompt section."""
    block = _render_dr_planner_brief_block(intent_brief or {})
    return f"\n\n{block}\n\nUse this classifier-derived brief to decide what to plan."


def build_planning_prompt(
    *,
    targets_str: str,
    network_discovery_section: str,
    tools_constraint: str,
    scope_constraints: str,
    intent_brief: Optional[Mapping[str, Any]] = None,
    clarified_inputs_section: str = "",
    planner_environment_section: str = "",
) -> str:
    """Build the deep-reasoning planner user prompt.

    The prompt body is sourced from ``intent_brief`` — the
    deterministic classifier-derived payload written by the intent
    classifier at turn start (see
    ``backend/services/langgraph_chat/intent/briefs.py``). Recent
    transcript is intentionally NOT rendered here: the DR planner is no
    longer a full-history consumer. The pre-cutover ``history_section``
    kwarg has been removed from this builder's signature; callers that
    still pass it will raise ``TypeError`` on the unknown kwarg, which
    is the post-cutover regression guardrail.

    ``clarified_inputs_section`` carries planner-owned clarifier answers
    and is NOT transcript — it is preserved as-is so downstream clarify
    lifecycle remains unchanged. Scope constraints, environment data,
    and available-tools constraints are also preserved intact.
    """
    brief_section = build_planner_brief_section(intent_brief)

    return _render_latest_template(
        "deep_reasoning_planner",
        "user.txt",
        targets_str=targets_str,
        brief_section=brief_section,
        clarified_inputs_section=clarified_inputs_section,
        planner_environment_section=planner_environment_section,
        network_discovery_section=network_discovery_section,
        tools_constraint=tools_constraint,
        scope_constraints=scope_constraints,
    )


SYNTHESIS_SYSTEM_PROMPT = """You are a self-aware AI pentesting agent that has detected you're in a reasoning loop.

Your task is to provide a graceful, honest response that:
1. Acknowledges you got stuck in a loop (be transparent and professional)
2. Summarizes what you discovered before getting stuck
3. Explains what you were trying to accomplish
4. Provides any partial findings or observations you made
5. Suggests alternative approaches the user could try

Guidelines:
- Be honest and professional (users appreciate transparency)
- Focus on value: even partial findings are useful
- Be specific about what worked vs. what didn't
- Offer concrete suggestions for how to proceed differently
- Use a helpful, apologetic tone without being overly verbose

Remember: Getting stuck is okay if you handle it gracefully and provide value."""


TOOL_CATEGORY_SELECTION_SYSTEM_PROMPT = (
    "You are a tool category selector. Respond only with valid JSON."
)

TOOL_CATEGORY_SELECTION_HINT_DIRECTIVE_TEMPLATE = """
**CRITICAL - CURRENT INTENT (HIGHEST PRIORITY):**
The agent's current decision is: \"{next_tool_hint}\"
Select categories that match THIS INTENT, not the resolved user intent.
For example: if intent mentions \"PostgreSQL\" or \"database\", select \"database_assessment\".
"""


_BRIEF_NONE_MARKER = "(none)"


def _render_brief_bullets(values: Any) -> str:
    """Render a list-of-strings brief field as newline-joined bullets.

    Returns ``"  (none)"`` when the field is empty or not a list. Keeps
    rendering deterministic so brief-driven prompts stay diffable.
    """
    if isinstance(values, list):
        cleaned: List[str] = [
            item.strip() for item in values if isinstance(item, str) and item.strip()
        ]
        if cleaned:
            return "\n".join(f"  - {item}" for item in cleaned)
    return f"  - {_BRIEF_NONE_MARKER}"


def _render_brief_value(value: Any) -> str:
    """Render a scalar brief field as a stripped string or the none marker."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return _BRIEF_NONE_MARKER


def _resolve_brief_target_field(brief: Mapping[str, Any], field_name: str) -> Any:
    """Resolve target fields from the flat brief contract with nested fallback."""
    if field_name in brief:
        return brief.get(field_name)
    target = brief.get("target")
    if isinstance(target, Mapping):
        return target.get(field_name)
    return None


def _render_brief_block(brief: Mapping[str, Any]) -> str:
    """Render the ``intent_brief`` as a compact structured block.

    The block is authored as the sole classifier-derived context input
    for the category selector. It replaces the previous recent-transcript
    dependency and intentionally omits tool ids, execution strategy, and
    parameter payloads (those are owned by downstream execution roles).

    An empty / missing brief still renders a valid block — every field
    falls back to ``"(none)"`` so the prompt shape stays stable.
    """
    readiness = _render_brief_value(brief.get("execution_readiness"))
    blocking_reason = _render_brief_value(brief.get("blocking_reason"))
    readiness_line = f"- Execution readiness: {readiness}"
    if readiness == "blocked" and blocking_reason != _BRIEF_NONE_MARKER:
        readiness_line += f" (blocked: {blocking_reason})"

    request_contract = brief.get("request_contract") or {}
    if not isinstance(request_contract, Mapping):
        request_contract = {}

    lines: List[str] = [
        "Turn Execution Brief (classifier-derived; transcript access is not available here):",
        f"- Resolved user intent: {_render_brief_value(brief.get('resolved_user_intent'))}",
        f"- Overall goal: {_render_brief_value(brief.get('overall_goal'))}",
        f"- Continuation mode: {_render_brief_value(brief.get('continuation_mode'))}",
        f"- Resolved step title: {_render_brief_value(brief.get('resolved_step_title'))}",
        f"- Resolved step detail: {_render_brief_value(brief.get('resolved_step_detail'))}",
        f"- Next operational goal: {_render_brief_value(brief.get('next_operational_goal'))}",
        f"- Success condition: {_render_brief_value(brief.get('success_condition'))}",
        readiness_line,
        f"- Resolved target: {_render_brief_value(_resolve_brief_target_field(brief, 'resolved_target'))}",
        f"- Target status: {_render_brief_value(_resolve_brief_target_field(brief, 'target_status'))}",
        f"- Target source: {_render_brief_value(_resolve_brief_target_field(brief, 'target_source'))}",
        "- Explicit constraints:",
        _render_brief_bullets(brief.get("explicit_constraints")),
        "- Relevant memory fragments:",
        _render_brief_bullets(brief.get("relevant_memory_fragments")),
        "- Suggested category focus:",
        _render_brief_bullets(brief.get("suggested_category_focus")),
        "- Retrieval hints:",
        _render_brief_bullets(brief.get("retrieval_hints")),
        "- Request contract:",
        f"  - question_type: {_render_brief_value(request_contract.get('question_type'))}",
        f"  - answer_style: {_render_brief_value(request_contract.get('answer_style'))}",
        f"  - terminal_when: {_render_brief_value(request_contract.get('terminal_when'))}",
    ]
    return "\n".join(lines)


def render_intent_brief_block(
    brief: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render the prompt-facing turn-execution brief with stable placeholders."""
    return _render_brief_block(brief or {})


def build_tool_category_selection_prompt(
    *,
    categories_text: str,
    intent_brief: Optional[Mapping[str, Any]] = None,
    next_tool_hint: Optional[str] = None,
    latest_phase_memory: str = "",
) -> str:
    """Build the category selector user prompt from the classifier brief.

    The prompt body is sourced from ``intent_brief`` — the
    deterministic classifier-derived payload written by the intent
    classifier at turn start (see
    ``backend/services/langgraph_chat/intent/briefs.py``). Recent
    transcript is intentionally NOT rendered here: the category selector
    is no longer a full-history consumer.

    The ``history_text`` parameter is gone from this builder's signature:
    no wired caller passes transcript text anymore and any reintroduction
    must be explicit. Callers that still pass ``history_text=...`` will
    ``TypeError`` on the unknown kwarg — this is the post-cutover
    regression guardrail.

    ``next_tool_hint`` remains supported as a subordinate override
    signal emitted by the post-tool reasoning node. ``latest_phase_memory``
    optionally carries the newest runtime phase record for the active turn;
    callers must pass only the latest phase, not the full phase ledger.
    """
    brief_block = render_intent_brief_block(intent_brief)

    hint_directive = ""
    if next_tool_hint:
        hint_directive = TOOL_CATEGORY_SELECTION_HINT_DIRECTIVE_TEMPLATE.format(
            next_tool_hint=next_tool_hint
        )

    latest_phase_block = ""
    task_basis = "the Turn Execution Brief above"
    if latest_phase_memory and latest_phase_memory.strip():
        latest_phase_block = f"""Latest Current-Turn Phase (fresh runtime steering for the CURRENT action):
{latest_phase_memory.strip()}

Precedence:
- Latest Current-Turn Phase is the freshest runtime steering signal for the immediate next action.
- Turn Execution Brief remains authoritative for the original user goal, explicit constraints, and success condition.
- If they conflict on the immediate next action, route by the latest phase while preserving all non-conflicting user constraints.
"""
        task_basis = "the Latest Current-Turn Phase above, then the Turn Execution Brief"
        if hint_directive:
            hint_directive = hint_directive.replace(
                "**CRITICAL - CURRENT INTENT (HIGHEST PRIORITY):**",
                "**CURRENT INTENT (advisory; subordinate to Latest Current-Turn Phase when present):**",
            )

    optional_runtime_context = "\n".join(
        part for part in (latest_phase_block.strip(), hint_directive.strip()) if part
    )
    if optional_runtime_context:
        optional_runtime_context += "\n\n"

    return f"""You are a penetration testing assistant selecting relevant tool categories.

{optional_runtime_context}{brief_block}

Available Tool Categories:
{categories_text}

Your Task:
Select categories only for capabilities directly required by the current action, next operational goal, or success condition in {task_basis}. Do not select categories for possible future steps, speculative troubleshooting, theoretical support work, or categories that might become useful after this action completes. If a category cannot be traced to one of those explicit requirements, leave it out.

Guidelines:
{TOOL_CATEGORY_SELECTION_GUIDANCE_TEXT}

Return ONLY valid JSON with this format:
{{
  \"selected_categories\": [\"category1\", \"category2\"],
  \"reasoning\": \"Brief explanation of why these categories were selected\"
}}"""


TOOL_ARTICULATION_SYSTEM_PROMPT = "You are explaining tool execution intent."


def build_tool_articulation_prompt(
    *,
    selected_tool: str,
    tool_params: str,
    intent_brief: Optional[Mapping[str, Any]] = None,
    runtime_state: str = "",
) -> str:
    """Build the user-facing tool articulation prompt from the classifier brief.

    The prompt body is sourced from ``intent_brief`` — the
    deterministic classifier-derived payload written by the intent
    classifier at turn start (see
    ``backend/services/langgraph_chat/intent/briefs.py``). Recent
    transcript is intentionally NOT rendered here: tool articulation is
    no longer a full-history consumer and must not re-infer user intent
    from conversation replay.

    ``runtime_state`` remains supported as a compact runtime-state
    slice (active_target / current_decision) when still needed for
    active-target wording; it does not carry transcript text. The
    wired node callsite
    (``agent/graph/nodes/tool_articulation.py``) does not supply one —
    the brief already carries resolved intent / next operational goal
    / success condition / target.
    """
    brief_block = render_intent_brief_block(intent_brief)

    runtime_state_block = ""
    if runtime_state.strip():
        runtime_state_block = f"\nRuntime State:\n{runtime_state.strip()}\n"

    return f"""YOUR TASK is to explain your next action in 1-2 sentences, grounded in the Turn Execution Brief below.

{brief_block}
{runtime_state_block}
Your decision:
- Tool: {selected_tool}
- Parameters: {tool_params}

Explain what you're about to do in natural language.

Guidelines:
- Start with \"To [achieve user's goal], I will...\"
- Be specific about the tool and its purpose
- Keep it to 1-2 sentences
- Use technical terminology appropriately

Example: \"To scan the network for open ports, I will execute nmap with a SYN scan on ports 5000-6000 to identify running services.\"

ANSWER:"""


__all__ = [
    # Prompt limits
    "MAX_STDOUT_EXCERPT_CHARS",
    "MAX_PARAM_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_PLAN_CHARS",
    "MAX_TODO_CHARS",
    "MAX_TODOS_IN_PROMPT",
    "POST_TOOL_MAX_PARAM_CHARS",
    "POST_TOOL_MAX_STDOUT_EXCERPT_CHARS",
    "POST_TOOL_MAX_STDERR_EXCERPT_CHARS",
    "POST_TOOL_MAX_SUMMARY_CHARS",
    "POST_TOOL_MAX_PLAN_CHARS",
    "POST_TOOL_MAX_TODO_CHARS",
    "POST_TOOL_MAX_TODOS_IN_PROMPT",
    "POST_TOOL_MAX_DECISION_RATIONALE_CHARS",
    "TOOL_RESULT_SUMMARY_MAX_CHARS",
    "COMPACT_SUMMARY_MAX_CHARS",
    "COMPACT_FINDING_MAX_CHARS",
    "COMPACT_RULE_FINDING_MAX_CHARS",
    "COMPACT_ERROR_LEAD_MAX_CHARS",
    "COMPACT_FAILURE_FINDING_MAX_CHARS",
    "COMPACT_FAILURE_STDOUT_LINE_MAX_CHARS",
    "COMPACT_ERROR_CONTEXT_MESSAGE_MAX_CHARS",
    "COMPACT_DECISION_EVIDENCE_MAX_CHARS",
    "COMPACT_ERROR_ENTRY_MAX_CHARS",
    # History limits
    "MAX_HISTORY_ENTRIES",
    "MAX_HISTORY_CONTENT_CHARS",
    "MAX_PRIOR_WORK_ENTRIES",
    # LLM params
    "MAX_REASONING_TOKENS",
    # Intent prompts (versioned templates)
    "CLASSIFIER_SYSTEM_PROMPT",
    "PROMPT_TEMPLATE",
    # Validation
    "VALID_POST_TOOL_ACTIONS",
    "VALID_TODO_STATUSES",
    # Node prompt strings/builders
    "CONVERSATION_SECTION_LABEL",
    "SIMPLE_CHAT_DEFAULT_SYSTEM_PROMPT",
    "KALI_CONTAINER_CAPABILITIES",
    "build_planner_system_prompt",
    "build_planner_history_section",
    "build_planner_tools_constraint",
    "build_scope_boundary_warnings",
    "build_planner_scope_constraints",
    "build_planner_brief_section",
    "build_planning_prompt",
    "SYNTHESIS_SYSTEM_PROMPT",
    "TOOL_CATEGORY_SELECTION_SYSTEM_PROMPT",
    "build_tool_category_selection_prompt",
    "TOOL_ARTICULATION_SYSTEM_PROMPT",
    "build_tool_articulation_prompt",
]
