"""Environment info loader for LangGraph nodes.

Loads runtime environment information through the provider boundary and formats
it for LLM prompts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def load_and_format_environment(task_id: Optional[int]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Load environment info and format for planner system prompt.
    
    This function:
    1. Loads environment metadata through the runtime provider
    2. Returns both raw data (for metadata storage) and formatted string (for prompt)
    
    Args:
        task_id: Task identifier. If None, returns empty results.
        
    Returns:
        Tuple of (env_info_dict, formatted_prompt_string).
        Returns (None, "") if loading fails or task_id is None.
    """
    if task_id is None:
        logger.debug("[ENV_LOADER] No task_id provided, skipping env load")
        return None, ""
    
    try:
        # Import here to avoid circular dependencies
        from backend.services.runtime_provider.environment_metadata import (
            resolve_local_runtime_environment_info,
        )
        from backend.services.workspace.environment_collector import format_environment_for_prompt
        
        env_info = resolve_local_runtime_environment_info(task_id=int(task_id))
        
        if env_info is None:
            logger.debug(f"[ENV_LOADER] No environment info found for task {task_id}")
            return None, ""
        
        formatted = format_environment_for_prompt(env_info)
        
        logger.info(
            f"[ENV_LOADER] Loaded environment info for task {task_id}: "
            f"hostname={env_info.get('hostname', 'unknown')}"
        )
        
        return env_info, formatted
        
    except ImportError as e:
        logger.warning(f"[ENV_LOADER] Backend module not available: {e}")
        return None, ""
    except Exception as e:
        logger.warning(f"[ENV_LOADER] Failed to load environment info for task {task_id}: {e}")
        return None, ""


def get_environment_compact(env_info: Optional[Dict[str, Any]]) -> str:
    """Format environment info as compact one-liner for reasoning prompts.
    
    Use this for post_tool_reasoning where we need minimal token usage.
    
    Args:
        env_info: Environment info dict from facts.metadata["environment_info"].
        
    Returns:
        Compact single-line string, or empty string if env_info is None.
    """
    if env_info is None:
        return ""
    
    try:
        from backend.services.workspace.environment_collector import format_environment_compact
        return format_environment_compact(env_info)
    except ImportError:
        logger.warning("[ENV_LOADER] Backend module not available for compact format")
        return ""
    except Exception as e:
        logger.warning(f"[ENV_LOADER] Failed to format compact environment: {e}")
        return ""


def get_environment_full(env_info: Optional[Dict[str, Any]]) -> str:
    """Format environment info as full multi-line format for prompts.
    
    Reuses the same format as the planner for consistency.
    
    Args:
        env_info: Environment info dict from facts.metadata["environment_info"].
        
    Returns:
        Full formatted environment string, or empty string if env_info is None.
    """
    if env_info is None:
        return ""
    
    try:
        from backend.services.workspace.environment_collector import format_environment_for_prompt
        return format_environment_for_prompt(env_info)
    except ImportError:
        logger.warning("[ENV_LOADER] Backend module not available for full format")
        return ""
    except Exception as e:
        logger.warning(f"[ENV_LOADER] Failed to format full environment: {e}")
        return ""


__all__ = ["load_and_format_environment", "get_environment_compact", "get_environment_full"]
