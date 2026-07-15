"""Unified event emission infrastructure.

Provides UnifiedEventEmitter base class, SimpleEmitter and DeepReasoningEmitter
subclasses, EventEmitterFactory for capability-based routing, and EventMetadata
dataclass with validation.
"""

from agent.graph.emission.unified_emitter import (
    DeepReasoningEmitter,
    EventMetadata,
    SimpleEmitter,
    StreamWriter,
    UnifiedEventEmitter,
)
from agent.graph.emission.factory import EventEmitterFactory

__all__ = [
    "DeepReasoningEmitter",
    "EventEmitterFactory",
    "EventMetadata",
    "SimpleEmitter",
    "StreamWriter",
    "UnifiedEventEmitter",
]
