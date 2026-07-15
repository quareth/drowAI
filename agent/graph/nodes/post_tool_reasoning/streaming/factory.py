"""Factory for creating streaming adapters.

This module provides a factory that creates the appropriate streaming
adapter based on the capability type.
"""

from __future__ import annotations

import logging

from .base import StreamingAdapter
from .dr_adapter import DRStreamingAdapter
from .simple_adapter import SimpleStreamingAdapter

logger = logging.getLogger(__name__)


class StreamingAdapterFactory:
    """Factory for creating capability-appropriate streaming adapters.
    
    This factory encapsulates the logic for selecting the right streaming
    adapter based on the capability, keeping capability checks out of the
    core node logic.
    """
    
    @staticmethod
    def create(capability: str) -> StreamingAdapter:
        """Create streaming adapter for given capability.
        
        Args:
            capability: Capability type (e.g., "deep_reasoning", "simple_tool_execution")
            
        Returns:
            Appropriate StreamingAdapter instance
            
        Raises:
            ValueError: If capability is not supported
        """
        capability_lower = capability.lower()
        
        if capability_lower == "deep_reasoning":
            logger.debug("[ADAPTER_FACTORY] Creating DRStreamingAdapter")
            return DRStreamingAdapter()
        elif capability_lower == "simple_tool_execution":
            logger.debug("[ADAPTER_FACTORY] Creating SimpleStreamingAdapter")
            return SimpleStreamingAdapter()
        else:
            msg = f"Unsupported capability: {capability}"
            logger.error(f"[ADAPTER_FACTORY] {msg}")
            raise ValueError(msg)


__all__ = ["StreamingAdapterFactory"]

