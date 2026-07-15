"""Backward compatibility utilities for LLM provider responses.

This module provides helper functions for gradual migration from
string-based responses to LLMResponse objects with usage tracking.

Existing code that expects strings can use these helpers to work
with both legacy string responses and new LLMResponse objects.

Example:
    from agent.providers.llm.contracts.compat import extract_content, extract_usage
    
    # Works with both old (str) and new (LLMResponse) return types
    response = await client.chat_messages_with_usage(messages)
    content = extract_content(response)
    usage = extract_usage(response)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

from ..core.base import LLMResponse

if TYPE_CHECKING:
    from backend.services.usage_tracking.models import UsageData


def extract_content(response: Union[str, LLMResponse]) -> str:
    """Extract content string from response.
    
    Enables backward-compatible code that works with both:
    - Legacy string responses from chat_messages()
    - New LLMResponse objects from chat_messages_with_usage()
    
    Args:
        response: Either a string or LLMResponse object
        
    Returns:
        The content string
        
    Example:
        # Works with either:
        content = extract_content(await client.chat_messages(messages))
        content = extract_content(await client.chat_messages_with_usage(messages))
    """
    if isinstance(response, str):
        return response
    return response.content


def extract_usage(response: Union[str, LLMResponse]) -> Optional["UsageData"]:
    """Extract usage data from response if available.
    
    Args:
        response: Either a string or LLMResponse object
        
    Returns:
        UsageData if response is LLMResponse with usage, None otherwise
        
    Example:
        response = await client.chat_messages_with_usage(messages)
        usage = extract_usage(response)
        if usage:
            print(f"Used {usage.total_tokens} tokens")
    """
    if isinstance(response, LLMResponse):
        return response.usage
    return None


def has_usage(response: Union[str, LLMResponse]) -> bool:
    """Check if response contains usage data.
    
    Args:
        response: Either a string or LLMResponse object
        
    Returns:
        True if response has usage data, False otherwise
    """
    if isinstance(response, LLMResponse):
        return response.usage is not None
    return False


__all__ = [
    "extract_content",
    "extract_usage", 
    "has_usage",
]
