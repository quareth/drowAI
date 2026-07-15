"""Context management package."""

from .tool_processor import UniversalToolProcessor, ProcessedOutput
from .token_manager import TokenManager
from .metrics import ContextMetrics
from .config import ContextConfig

# Token counting utilities
try:
    from .token_utils import TokenCounter, count_tokens, count_tokens_json, get_token_counter
    from .token_counter_registry import (
        TokenEstimate,
        estimate_json_tokens,
        estimate_text_tokens,
        get_token_counter_for_model,
    )
    __all__ = [
        "ProcessedOutput",
        "UniversalToolProcessor",
        "TokenManager",
        "ContextMetrics",
        "ContextConfig",
        "TokenCounter",
        "count_tokens",
        "count_tokens_json",
        "get_token_counter",
        "TokenEstimate",
        "estimate_json_tokens",
        "estimate_text_tokens",
        "get_token_counter_for_model",
    ]
except ImportError:
    __all__ = [
        "ProcessedOutput",
        "UniversalToolProcessor",
        "TokenManager",
        "ContextMetrics",
        "ContextConfig",
    ]
