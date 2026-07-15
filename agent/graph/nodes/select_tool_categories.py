"""Node that selects relevant tool categories using LLM reasoning.

This node is a brief consumer only. Turn interpretation comes from
the classifier-derived ``working_memory.intent_brief`` payload. The category
selector does not read the shared ``ConversationContextBundle``
transcript window.

Full-history access is restricted (see
``docs/plans/intent_interpretation_wiring.md``) to two explicit seams:
the intent classifier at turn start and the deep-reasoning finalizer.
The category selector is NOT one of those seams: recent-transcript
resolution on this node's hot path is deliberately impossible.

``next_tool_hint`` remains a subordinate corrective signal emitted by
post-tool reasoning and flows through unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from backend.services.metrics.utils import safe_inc
from agent.providers.llm.core.exceptions import LLMRefusalError
from agent.tool_runtime.batch.plan_view import primary_tool_call_from_metadata

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from ..emission.reasoning_section import reasoning_section
from ..config.token_limits import LIMITS
from core.llm import (
    LLM_TIMEOUT_TOOL_CATEGORY_SELECTOR_SEC,
    wait_for_with_timeout,
)
from core.prompts.constants import (
    TOOL_CATEGORY_SELECTION_SYSTEM_PROMPT,
    build_tool_category_selection_prompt,
)
from core.tool_category_taxonomy import find_missing_descriptions
from core.llm.structured_schemas import TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT
from .post_tool_reasoning.core.retry_logic import get_retry_count
from ..utils.llm_resolver import (
    ROLE_TOOL_CATEGORY_SELECTOR,
    get_llm_reasoning_effort,
    resolve_llm_client,
)
from ..utils import iteration_memory as _iteration_memory
from .node_utils import append_usage_to_state

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

# NOTE: We intentionally do NOT append tool-category selection messages into
# legacy prose continuity buffers. Category-selection entries are
# high-frequency and low-signal, and can evict important tool-failure
# context, increasing the risk of loops.

logger = logging.getLogger(__name__)


async def select_tool_categories_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """Select relevant tool categories based on user request.

    This node uses LLM to determine which tool categories are relevant
    for the current request, enabling focused tool selection in subsequent steps.

    Flow:
        1. Extract user request and conversation history from state
        2. Get available tool categories
        3. LLM selects relevant categories
        4. Store selected categories in state metadata

    Args:
        state: Current interactive state
        context: Runtime context with API keys, model, workspace path

    Returns:
        Updated state with selected_categories in metadata
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.safe_metadata

    # Check if this is a retry attempt (reflection suggestions present)
    reflection_suggestions = metadata.get("reflection_suggestions")
    is_retry_attempt = reflection_suggestions is not None

    if is_retry_attempt:
        logger.info("[CATEGORY_SELECTOR] Retry attempt detected, applying reflection suggestions")
        return _handle_retry_tool_selection(interactive, reflection_suggestions, context)

    # Empty-message guard: the in-flight user turn is the trigger for
    # category selection; without it there is nothing to route.
    if not (facts.message or "").strip():
        logger.warning("[CATEGORY_SELECTOR] No user message, skipping category selection")
        return interactive.as_graph_update()

    # Get available categories
    try:
        from agent.tools.category_utils import (
            get_category_descriptions,
            get_tool_categories,
        )
    except ImportError:
        logger.error("[CATEGORY_SELECTOR] Failed to import category_utils")
        return interactive.as_graph_update()

    available_categories = get_tool_categories()
    if not available_categories:
        logger.error("[CATEGORY_SELECTOR] No categories available")
        return interactive.as_graph_update()

    category_descriptions = get_category_descriptions()

    # Get tool intent hint from post_tool_reasoning (if present)
    # This ensures we select categories matching the CURRENT intent, not original request
    next_tool_hint = facts.next_tool_hint or facts.metadata.get("next_tool_hint")

    if next_tool_hint:
        logger.info(
            f"[CATEGORY_SELECTOR] Using next_tool_hint for category selection: '{next_tool_hint[:60]}...'"
        )

    # Classifier-derived brief folded into working memory at graph start.
    # This is the sole source of turn interpretation for this node.
    intent_brief: Mapping[str, Any] = {}
    working_memory = facts.metadata.get("working_memory")
    if isinstance(working_memory, Mapping):
        raw_intent_brief = working_memory.get("intent_brief")
        if isinstance(raw_intent_brief, Mapping):
            intent_brief = raw_intent_brief
    turn_sequence = metadata.get("turn_sequence")
    latest_phase_memory = _iteration_memory.render_latest_phase_memory_section(
        dict(metadata),
        turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
    )

    # Build LLM prompt
    try:
        prompt = _build_category_selection_prompt(
            available_categories=available_categories,
            category_descriptions=category_descriptions,
            next_tool_hint=next_tool_hint,
            intent_brief=intent_brief,
            latest_phase_memory=latest_phase_memory,
        )
    except ValueError as exc:
        logger.error("[CATEGORY_SELECTOR] Taxonomy drift detected while building prompt: %s", exc)
        fallback_categories = ["information_gathering"]
        facts.metadata["selected_categories"] = fallback_categories
        facts.metadata["category_selection_error"] = "taxonomy_drift"
        return interactive.as_graph_update()

    # Call LLM to select categories (pass interactive for usage tracking - Phase 7)
    try:
        async with reasoning_section(
            writer,
            state=interactive,
            step="tool_category_selection",
            label="Selecting relevant tool categories.",
            config=config,
            context=context,
        ):
            selected_categories = await _call_llm_for_categories(
                prompt=prompt,
                available_categories=available_categories,
                interactive=interactive,  # Phase 7: Enable usage tracking
                context=context,
                config=config,
            )
    except LLMRefusalError:
        raise
    except Exception as exc:
        logger.error(f"[CATEGORY_SELECTOR] LLM call failed: {exc}")
        # Fallback: select information_gathering as default
        fallback_categories = ["information_gathering"]
        facts.metadata["selected_categories"] = fallback_categories
        logger.warning(f"[CATEGORY_SELECTOR] Using fallback categories due to error: {fallback_categories}")
        return interactive.as_graph_update()

    # Store selected categories in metadata
    facts.metadata["selected_categories"] = selected_categories

    # Log selection for debugging
    logger.info(
        f"[CATEGORY_SELECTOR] Selected {len(selected_categories)} categories: {selected_categories} "
        f"(from {len(available_categories)} available)"
    )

    # Add reasoning entry
    categories_text = ", ".join(selected_categories)
    reasoning_entry = f"Selected tool categories: {categories_text}"
    interactive.trace.reasoning.append(reasoning_entry)

    return interactive.as_graph_update()


def _build_category_selection_prompt(
    available_categories: List[str],
    category_descriptions: Dict[str, str],
    next_tool_hint: Optional[str] = None,
    intent_brief: Optional[Mapping[str, Any]] = None,
    latest_phase_memory: str = "",
) -> str:
    """Build prompt for LLM to select relevant tool categories.

    Args:
        available_categories: Canonical category identifiers.
        category_descriptions: Description per category id.
        next_tool_hint: Intent from post_tool_reasoning observation.
            When present, this takes priority over the resolved user
            intent for category routing.
        intent_brief: Classifier-derived turn interpretation
            folded into ``working_memory.intent_brief`` at turn start.
            Drives the prompt's Context section. This is the sole
            source of turn-interpretation context for this node.
        latest_phase_memory: Optional latest current-turn phase block.
            This is runtime steering for the immediate next action and
            intentionally not the full current-turn ledger.
    """

    missing_descriptions = find_missing_descriptions(
        available_categories,
        descriptions=category_descriptions,
    )
    if missing_descriptions:
        raise ValueError(
            "Missing category descriptions for: "
            + ", ".join(sorted(set(missing_descriptions)))
        )

    # Format categories with descriptions
    category_list = []
    for cat in available_categories:
        desc = category_descriptions[cat]
        category_list.append(f"  - {cat}: {desc}")

    categories_text = "\n".join(category_list)

    return build_tool_category_selection_prompt(
        categories_text=categories_text,
        intent_brief=intent_brief,
        next_tool_hint=next_tool_hint,
        latest_phase_memory=latest_phase_memory,
    )


async def _call_llm_for_categories(
    prompt: str,
    available_categories: List[str],
    interactive: Optional[InteractiveState] = None,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
    model: Optional[str] = None,
) -> List[str]:
    """Call LLM to select categories and parse response.

    Args:
        prompt: The category selection prompt
        model: Optional conversation model hint for resolver metadata
        available_categories: Valid category names
        interactive: Optional state for usage tracking (Phase 7)
    """
    metadata: Dict[str, Any] = {}
    if interactive is not None:
        metadata.update(interactive.facts.safe_metadata)
    if isinstance(model, str) and model.strip():
        metadata["model"] = model
    client = resolve_llm_client(
        metadata,
        context,
        config=config,
        role=ROLE_TOOL_CATEGORY_SELECTOR,
    )
    reasoning_effort = get_llm_reasoning_effort(client)

    response = ""
    usage = None
    structured_payload: Optional[Dict[str, Any]] = None
    task_id = getattr(getattr(interactive, "facts", None), "task_id", None)
    if callable(getattr(type(client), "chat_with_usage", None)):
        llm_response = await wait_for_with_timeout(
            client.chat_with_usage(
                system_prompt=TOOL_CATEGORY_SELECTION_SYSTEM_PROMPT,
                user_prompt=prompt,
                temperature=0.1,
                max_tokens=LIMITS.tool_selection,
                reasoning_effort=reasoning_effort,
                structured_output=TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT,
            ),
            timeout_sec=LLM_TIMEOUT_TOOL_CATEGORY_SELECTOR_SEC,
            component="TOOL_CATEGORY_SELECTOR",
            operation="category_selection_llm_call",
            logger=logger,
            task_id=task_id,
            outcome="category_selection_timeout",
        )
        response = llm_response.content
        usage = getattr(llm_response, "usage", None)
        maybe_structured = getattr(llm_response, "structured_output", None)
        if isinstance(maybe_structured, dict):
            structured_payload = maybe_structured
    else:  # pragma: no cover - legacy clients in tests
        response = await wait_for_with_timeout(
            client.chat(
                TOOL_CATEGORY_SELECTION_SYSTEM_PROMPT,
                prompt,
                temperature=0.1,
                max_tokens=LIMITS.tool_selection,
                reasoning_effort=reasoning_effort,
                structured_output=TOOL_CATEGORY_SELECTOR_STRUCTURED_OUTPUT,
            ),
            timeout_sec=LLM_TIMEOUT_TOOL_CATEGORY_SELECTOR_SEC,
            component="TOOL_CATEGORY_SELECTOR",
            operation="category_selection_llm_call",
            logger=logger,
            task_id=task_id,
            outcome="category_selection_timeout",
        )

    # Record usage in state if available (Phase 7)
    if interactive is not None and usage:
        try:
            append_usage_to_state(
                interactive,
                usage,
                "select_tool_categories",
                request_mode="non_streaming",
            )
        except Exception as exc:  # pragma: no cover - usage is optional
            logger.debug(f"[CATEGORY_SELECTOR] Failed to record usage: {exc}")

    # Parse JSON response
    try:
        parsed = structured_payload if structured_payload is not None else json.loads(response)
        selected = parsed.get("selected_categories", [])
        reasoning = parsed.get("reasoning", "")

        # Validate categories
        valid_categories = [
            cat for cat in selected
            if cat in available_categories
        ]

        if not valid_categories:
            logger.warning(
                f"[CATEGORY_SELECTOR] No valid categories in LLM response: {selected}. "
                f"Defaulting to information_gathering"
            )
            return ["information_gathering"]

        logger.debug(f"[CATEGORY_SELECTOR] LLM reasoning: {reasoning}")
        return valid_categories

    except json.JSONDecodeError as exc:
        logger.error(f"[CATEGORY_SELECTOR] Failed to parse LLM response as JSON: {exc}")
        logger.debug(f"[CATEGORY_SELECTOR] Raw response: {response}")
        # Fallback
        return ["information_gathering"]


def _handle_retry_tool_selection(
    interactive: InteractiveState,
    reflection_suggestions: Dict[str, Any],
    context: Optional[GraphRuntimeContext],
) -> dict:
    """Handle tool selection for retry attempts using reflection suggestions.

    This function bypasses LLM category selection and directly applies
    the failure reflection's suggested recovery strategy.

    Args:
        interactive: Current interactive state
        reflection_suggestions: Reflection output from failure_reflection node
        context: Runtime context (optional)

    Returns:
        Updated state with retry tool selection applied
    """
    facts = interactive.facts
    metadata = facts.ensure_metadata()

    retry_count = get_retry_count(metadata)

    # Extract reflection suggestions
    alternative_tool = reflection_suggestions.get("alternative_tool")
    same_tool_retry = reflection_suggestions.get("same_tool_retry", False)
    suggested_params = reflection_suggestions.get("suggested_params", {})
    reasoning = reflection_suggestions.get("reasoning", "")
    can_recover = reflection_suggestions.get("can_recover", False)
    failure_category = reflection_suggestions.get("failure_category", "unknown")
    current_call = primary_tool_call_from_metadata(metadata)
    current_tool = current_call.tool_id if current_call is not None else ""
    current_params = dict(current_call.parameters) if current_call is not None else {}

    logger.info(
        f"[CATEGORY_SELECTOR] Retry {retry_count}: "
        f"alternative_tool={alternative_tool}, same_tool_retry={same_tool_retry}, "
        f"can_recover={can_recover}, category={failure_category}"
    )

    # Validate alternative tool exists
    if alternative_tool:
        from agent.tools.tool_registry import tool_exists
        if not tool_exists(alternative_tool):
            logger.error(
                f"[CATEGORY_SELECTOR] Alternative tool {alternative_tool} does not exist, "
                "falling back to current tool"
            )
            alternative_tool = None
            same_tool_retry = True

    # Determine tool and params for retry
    if alternative_tool:
        # Use alternative tool suggested by reflection
        retry_tool = alternative_tool
        retry_params = suggested_params
        logger.info(f"[CATEGORY_SELECTOR] Using alternative tool: {retry_tool}")
        safe_inc("simple_tool_retry_alternative_tool")
    elif same_tool_retry:
        # Retry same tool with modified params
        retry_tool = current_tool
        # Merge current params with suggested modifications
        retry_params = {**current_params, **suggested_params}
        logger.info(f"[CATEGORY_SELECTOR] Retrying same tool with modified params: {retry_tool}")
        safe_inc("simple_tool_retry_same_tool")
    else:
        # Fallback: no clear suggestion, keep current tool
        retry_tool = current_tool
        retry_params = current_params
        logger.warning(
            f"[CATEGORY_SELECTOR] No clear retry suggestion, keeping current tool: {retry_tool}"
        )
        safe_inc("simple_tool_retry_fallback")

    # Handle missing current tool
    if not retry_tool:
        logger.warning("[CATEGORY_SELECTOR] No canonical tool batch available for retry context")

    retry_hint = {
        "tool": retry_tool,
        "suggested_parameters": retry_params,
        "reasoning": reasoning,
        "failure_category": failure_category,
    }
    metadata["next_tool_hint"] = (
        "Retry after tool failure using reflection guidance: "
        f"{json.dumps(retry_hint, ensure_ascii=True, default=str)}"
    )
    facts.next_tool_hint = metadata["next_tool_hint"]
    metadata.pop("planner_plan", None)
    metadata.pop("tool_plan_prepared", None)
    metadata.pop("plan_retry_corrected", None)

    # Clear reflection suggestions to prevent reuse
    metadata.pop("reflection_suggestions", None)
    facts.metadata = metadata

    # Add reasoning entry
    reasoning_entry = (
        f"Retry attempt {retry_count}: {reasoning[:100]}... "
        f"(tool={retry_tool}, category={failure_category})"
    )
    interactive.trace.reasoning.append(reasoning_entry)

    # Determine categories for retry tool (for downstream nodes)
    # This ensures tool_execution gets proper category context
    retry_categories = _get_categories_for_tool(retry_tool)
    if retry_categories:
        metadata["selected_categories"] = retry_categories
        logger.info(f"[CATEGORY_SELECTOR] Retry categories: {retry_categories}")

    return interactive.as_graph_update()


def _get_categories_for_tool(tool_id: str) -> List[str]:
    """Get categories for a specific tool.

    Used during retry to determine which categories the retry tool belongs to.

    Args:
        tool_id: Tool identifier

    Returns:
        List of category names (empty if tool not found)
    """
    try:
        # Extract category from tool_id (format: category.subcategory.tool_name)
        if "." in tool_id:
            category = tool_id.split(".")[0]
            return [category]
    except Exception as e:
        logger.warning(f"[CATEGORY_SELECTOR] Failed to get categories for {tool_id}: {e}")

    return []


__all__ = ["select_tool_categories_node"]
