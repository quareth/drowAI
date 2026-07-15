"""Shared prompt infrastructure.

This package provides reusable utilities for loading/rendering prompt
templates and registering prompt builders used across the application.
"""

from .base import ChatPromptBuilder, ToolResult
from .loader import TemplateLoader
from .registry import PromptRegistry
from .schemas import PromptContext, TemplateId, TemplateRef, ToolResultContext

__all__ = [
    "ChatPromptBuilder",
    "ToolResult",
    "TemplateLoader",
    "PromptRegistry",
    "PromptContext",
    "ToolResultContext",
    "TemplateId",
    "TemplateRef",
]
