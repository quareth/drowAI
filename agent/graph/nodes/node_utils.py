"""Shared utilities for graph nodes to promote code reuse (DRY)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import InteractiveState

logger = logging.getLogger(__name__)


# =============================================================================
# Token Usage Helpers (Phase 7)
# =============================================================================

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
    if callable(to_dict):
        try:
            result = to_dict()
        except Exception:
            return None
        return result if isinstance(result, dict) else None
    return None


def _string_usage_field(value: Any, default: str = "unknown") -> str:
    """Return a string usage metadata field with a stable default."""
    return value if isinstance(value, str) and value else default


def _normalized_request_mode(value: Any) -> Optional[str]:
    """Return a supported request-mode label when the caller knows it."""
    if value in {"streaming", "non_streaming"}:
        return value
    return None


def _apply_request_mode(payload: Dict[str, Any], request_mode: Any = None) -> None:
    """Attach request mode to a usage payload when explicitly known."""
    normalized = _normalized_request_mode(request_mode)
    if normalized is None:
        normalized = _normalized_request_mode(payload.get("request_mode"))
    if normalized is not None:
        payload["request_mode"] = normalized


def _usage_to_dict(
    usage: Any,
    source: str = "unknown",
    *,
    request_mode: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Convert UsageData to dict for storage in state.
    
    This helper converts UsageData objects from LLM responses into dict format
    suitable for storage in trace.usage_records. It delegates to UsageData.to_dict()
    when available for consistency.
    
    Args:
        usage: UsageData instance, dict, or None
        source: Identifier for the call site (e.g., "planner", "reflect", "intent_classifier")
        request_mode: Optional call mode label when known
            (``"streaming"`` or ``"non_streaming"``)
        
    Returns:
        Dict representation with source tag, or None if usage is None/invalid
        
    Example:
        response = await client.chat_with_usage(system, user)
        usage_dict = _usage_to_dict(response.usage, "planner")
        # {"prompt_tokens": 100, "completion_tokens": 50, ..., "source": "planner"}
    """
    if usage is None:
        return None
    
    # Prefer UsageData.to_dict() if available (canonical implementation)
    if hasattr(usage, 'to_dict') and callable(usage.to_dict):
        try:
            result = usage.to_dict(source)
            if isinstance(result, dict):
                _apply_request_mode(result, request_mode)
            return result
        except Exception:
            pass  # Fall through to manual extraction
    
    # Handle UsageData objects without to_dict (backward compatibility)
    if hasattr(usage, 'prompt_tokens'):
        result = {
            "prompt_tokens": getattr(usage, 'prompt_tokens', 0) or 0,
            "completion_tokens": getattr(usage, 'completion_tokens', 0) or 0,
            "total_tokens": getattr(usage, 'total_tokens', 0) or 0,
            "model": getattr(usage, 'model', 'unknown'),
            "provider": getattr(usage, 'provider', 'openai'),
            "cached_tokens": getattr(usage, 'cached_tokens', 0) or 0,
            "reasoning_tokens": getattr(usage, 'reasoning_tokens', 0) or 0,
            "api_surface": _string_usage_field(
                getattr(usage, 'api_surface', 'unknown')
            ),
            "cache_reporting": _string_usage_field(
                getattr(usage, 'cache_reporting', 'unknown')
            ),
            "source": source,
        }
        components = _provider_usage_components_to_dict(
            getattr(usage, "provider_usage_components", None)
        )
        if components is not None:
            result["provider_usage_components"] = components
        _apply_request_mode(result, request_mode)
        return result
    
    # Handle dict-like objects (for state restoration scenarios)
    if isinstance(usage, dict):
        result = {
            "prompt_tokens": usage.get('prompt_tokens', 0) or 0,
            "completion_tokens": usage.get('completion_tokens', 0) or 0,
            "total_tokens": usage.get('total_tokens', 0) or 0,
            "model": usage.get('model', 'unknown'),
            "provider": usage.get('provider', 'openai'),
            "cached_tokens": usage.get('cached_tokens', 0) or 0,
            "reasoning_tokens": usage.get('reasoning_tokens', 0) or 0,
            "api_surface": _string_usage_field(
                usage.get('api_surface', 'unknown')
            ),
            "cache_reporting": _string_usage_field(
                usage.get('cache_reporting', 'unknown')
            ),
            "source": source,
        }
        components = _provider_usage_components_to_dict(
            usage.get("provider_usage_components")
        )
        if components is not None:
            result["provider_usage_components"] = components
        _apply_request_mode(result, request_mode)
        return result
    
    logger.debug(f"[USAGE] Could not convert usage to dict: type={type(usage)}")
    return None


def append_usage_to_state(
    interactive: "InteractiveState",
    usage: Any,
    source: str,
    *,
    request_mode: str | None = None,
) -> None:
    """Append usage record to interactive state's trace.usage_records.
    
    This helper safely appends token usage from an LLM call to the state,
    which will be aggregated at the handler level for persistence.
    
    Args:
        interactive: InteractiveState to update
        usage: UsageData from LLM call (or None)
        source: Source identifier for this call (e.g., "planner", "select_tool_categories")
        request_mode: Optional call mode label when known
            (``"streaming"`` or ``"non_streaming"``)
        
    Example:
        response = await client.chat_with_usage(system, user)
        append_usage_to_state(interactive, response.usage, "planner")
    """
    if usage is None:
        return
    
    usage_dict = _usage_to_dict(usage, source, request_mode=request_mode)
    if usage_dict is None:
        return
    
    # Ensure trace.usage_records exists and is a list
    if not hasattr(interactive.trace, 'usage_records'):
        logger.warning("[USAGE] InteractiveState.trace has no usage_records attribute")
        return
    
    if interactive.trace.usage_records is None:
        interactive.trace.usage_records = []
    
    interactive.trace.usage_records.append(usage_dict)
    
    logger.debug(
        f"[USAGE] Recorded {usage_dict['total_tokens']} tokens from {source} "
        f"(prompt={usage_dict['prompt_tokens']}, completion={usage_dict['completion_tokens']})"
    )


def format_plan(plan: List[str]) -> str:
    """Format plan steps for display in prompts.
    
    Args:
        plan: List of plan step strings
    
    Returns:
        Numbered, newline-separated plan steps
    """
    if not plan:
        return "No plan"
    return "\n".join([f"{i}. {step}" for i, step in enumerate(plan, 1)])


def format_list(items: List[Any]) -> str:
    """Format list items for display in prompts.
    
    Args:
        items: List of item strings
    
    Returns:
        Bulleted, newline-separated items
    """
    if not items:
        return "None"
    rendered = []
    for item in items:
        if hasattr(item, "description"):
            text = item.description
        elif isinstance(item, dict) and "text" in item:
            text = item["text"]
        else:
            text = str(item)
        rendered.append(f"- {text}")
    return "\n".join(rendered)


def format_tool_attempts(executed_tools: List[Dict[str, Any]], limit: int = 5) -> str:
    """Format tool execution attempts for display in prompts.
    
    Args:
        executed_tools: List of tool execution records (dict or ToolExecutionRecord)
        limit: Maximum number of recent tools to show
    
    Returns:
        Formatted string showing tool attempts with success/failure indicators
    """
    if not executed_tools:
        return "No tools were successfully executed"
    
    attempts = []
    for tool in executed_tools[-limit:]:  # Show last N attempts
        if isinstance(tool, dict):
            tool_id = tool.get("tool_id", "unknown")
            # Check for success in various possible fields
            success = tool.get("success")
            if success is None:
                # Some tools might not have 'success' field, infer from observation
                observation = tool.get("observation", "")
                success = bool(observation and "error" not in observation.lower())
            
            status = "✓" if success else "✗"
            attempts.append(f"  {status} {tool_id}")
        else:
            # Handle ToolExecutionRecord objects
            tool_id = getattr(tool, "tool_id", "unknown")
            # ToolExecutionRecord uses 'status' field: "success" or "error"
            tool_status = getattr(tool, "status", "success")
            success = tool_status == "success"
            
            status = "✓" if success else "✗"
            attempts.append(f"  {status} {tool_id}")
    
    return "\n".join(attempts) if attempts else "No tool attempts recorded"


def format_observations(observations: List[str], limit: int = 5) -> str:
    """Format observations for display in prompts.
    
    Args:
        observations: List of observation strings
        limit: Maximum number of recent observations to show
    
    Returns:
        Bulleted list of recent observations
    """
    if not observations:
        return "No observations recorded"
    
    recent = observations[-limit:] if len(observations) > limit else observations
    return "\n".join([f"- {obs}" for obs in recent])


def extract_recent_tool_summary(executed_tools: List[Dict[str, Any]]) -> str:
    """Extract summary of most recent tool execution.
    
    Args:
        executed_tools: List of tool execution records
    
    Returns:
        Formatted summary of last tool execution, or empty string if none
    """
    if not executed_tools:
        return ""
    
    last_tool = executed_tools[-1]
    if not isinstance(last_tool, dict):
        return ""
    
    tool_id = last_tool.get("tool_id", "unknown")
    observation = last_tool.get("observation", "")
    
    if observation:
        # Truncate long observations
        obs_truncated = observation[:300] + "..." if len(observation) > 300 else observation
        return f"\n**Last Tool Result**: {tool_id}\n{obs_truncated}"
    
    return f"\n**Last Tool**: {tool_id} (no observation)"


def determine_post_reflect_action(todo_list: List[str]) -> str:
    """Select the next action after reflection based on todo list.

    Args:
        todo_list: Current todo items maintained in the interactive state.

    Returns:
        Recommended action name aligned with VALID_ACTIONS used by routing.
    """

    if todo_list:
        return "call_tool"
    return "think_more"


def normalize_stream_chunk(chunk: object) -> str:
    """Normalize streamed LLM chunks into plain text strings."""
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, list):
        return "".join(str(part) for part in chunk if part)
    return str(chunk or "")


__all__ = [
    # Formatting helpers
    "format_plan",
    "format_list",
    "format_tool_attempts",
    "format_observations",
    "extract_recent_tool_summary",
    # State helpers
    "determine_post_reflect_action",
    "normalize_stream_chunk",
    # Token usage helpers (Phase 7)
    "_usage_to_dict",
    "append_usage_to_state",
]
