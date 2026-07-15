"""LLM Provider abstraction layer.

This module provides a unified interface for interacting with various LLM providers
(OpenAI, Anthropic, etc.) through a common abstraction. Graph nodes should use
this module rather than importing provider classes directly.

Usage:
    from agent.providers.llm import LLMClientFactory, LLMClient, ProviderModelRef
    
    client = LLMClientFactory.get_client(
        provider_model=ProviderModelRef("openai", "gpt-5.2"),
        api_key="sk-...",
    )

    # Both providers implement the same interface:
    response = await client.chat("You are helpful.", "Hello!")
    
    # Streaming
    async for chunk in client.stream_chat_messages(messages):
        print(chunk, end="")
    
    # Tool calling
    result = await client.chat_with_tools(system, user, tools)
    if result.tool_calls:
        for tool_call in result.tool_calls:
            print(f"Call {tool_call.name} with {tool_call.arguments}")

Token Usage Tracking:
    For accurate token usage tracking, use the *_with_usage() methods:
    
    # Non-streaming with usage
    response = await client.chat_messages_with_usage(messages)
    print(response.content)
    if response.usage:
        print(f"Used {response.usage.total_tokens} tokens")
    
    # Streaming with usage
    stream = await client.stream_chat_messages_with_usage(messages)
    async for chunk in stream.content_iterator:
        print(chunk, end="")
    usage = stream.get_final_usage()  # Call after consuming all chunks

Provider Registration:
    Providers are registered at module import time. To add a new provider:
    
    1. Create a class implementing LLMClient
    2. Add provider/model profiles and capabilities
    3. Register an adapter resolver with LLMClientFactory.register_provider(...)
    
    See docs/architecture/LLM_PROVIDERS.md for detailed guide.

Registered Providers:
    - "openai" -> OpenAIChatClient and OpenAIResponsesClient
    - "anthropic" -> AnthropicMessagesClient
    - Legacy model-only prefixes remain registered only for compatibility

Exceptions:
    All exceptions inherit from LLMProviderError. Catch specific exceptions
    for fine-grained error handling:
    
    - LLMConfigurationError: Invalid configuration (missing API key, etc.)
    - LLMAPIError: API call failures (network, auth, rate limits)
    - LLMResponseError: Response parsing failures
    - LLMProviderNotFoundError: No provider for requested model
"""

from .core.base import (
    ChatMessage,
    LLMClient,
    LLMResponse,
    LLMStreamingResponse,
    StructuredOutputSpec,
    ToolChoiceInput,
    ToolCall,
    ToolCallResult,
    ToolSpecInput,
)
from .adapters.anthropic.client import AnthropicMessagesClient
from .core.capabilities import (
    LLMCapability,
    freeze_capabilities,
    has_capability,
    normalize_capability,
)
from .contracts.compat import extract_content, extract_usage, has_usage
from .core.exceptions import (
    LLMAPIError,
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMProviderError,
    LLMProviderNotFoundError,
    LLMProfileNotFoundError,
    LLMRefusalError,
    LLMRefusalOutcome,
    LLMResponseError,
)
from .factory import LLMClientFactory
from .core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    ProviderModelResolution,
    get_openai_legacy_compatibility_family,
    is_openai_legacy_compatible_model,
    normalize_model_id,
    normalize_provider_id,
    resolve_legacy_openai_model_ref,
)
from .adapters.openai.chat import OpenAIChatClient
from .adapters.openai.responses.client import OpenAIResponsesClient
from .profiles import (
    ANTHROPIC_API_SURFACE_MESSAGES,
    ANTHROPIC_EXACT_MODEL_IDS,
    ANTHROPIC_LISTABLE_MODEL_IDS,
    ANTHROPIC_NON_LISTABLE_MODEL_IDS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    MODEL_PROFILE_REGISTRY,
    OPENAI_DEFAULT_MODEL_ID,
    OPENAI_EXACT_MODEL_IDS,
    OPENAI_LEGACY_CHAT_MODEL_IDS,
    OPENAI_LISTABLE_MODEL_IDS,
    OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS,
    ModelProfile,
    ModelProfileRegistry,
    ProviderProfile,
    get_default_model_ref,
    list_catalog_model_profiles,
    list_model_profiles,
    require_model_capability,
    require_model_profile,
    require_provider_capability,
    require_provider_profile,
    resolve_context_window_tokens,
    resolve_max_output_tokens,
    supports_model,
    supports_provider,
)
from .contracts.structured_output_strategy import (
    STRUCTURED_OUTPUT_STRATEGIES,
    StructuredOutputFallbackPolicy,
    StructuredOutputStrategy,
    StructuredOutputStrategySelection,
    freeze_structured_output_strategies,
    normalize_structured_output_strategy,
    select_structured_output_strategy,
)
from .contracts.tool_contracts import (
    FunctionToolSpec,
    TOOL_CHOICE_MODES,
    ToolChoice,
    ToolChoiceMode,
    freeze_tool_choice_modes,
    function_tool_spec_from_openai_dict,
    normalize_tool_choice_mode,
)

__all__ = [
    # Core interface
    "LLMClient",
    "LLMClientFactory",
    "ChatMessage",
    # Built-in providers
    "AnthropicMessagesClient",
    "OpenAIChatClient",
    "OpenAIResponsesClient",
    # Response types with usage
    "LLMResponse",
    "LLMStreamingResponse",
    "StructuredOutputSpec",
    "ToolSpecInput",
    "ToolChoiceInput",
    # Provider identity, capabilities, and profiles
    "ANTHROPIC_PROVIDER_ID",
    "OPENAI_PROVIDER_ID",
    "ProviderModelRef",
    "ProviderModelResolution",
    "normalize_model_id",
    "normalize_provider_id",
    "resolve_legacy_openai_model_ref",
    "get_openai_legacy_compatibility_family",
    "is_openai_legacy_compatible_model",
    "LLMCapability",
    "freeze_capabilities",
    "has_capability",
    "normalize_capability",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ANTHROPIC_API_SURFACE_MESSAGES",
    "ANTHROPIC_EXACT_MODEL_IDS",
    "ANTHROPIC_LISTABLE_MODEL_IDS",
    "ANTHROPIC_NON_LISTABLE_MODEL_IDS",
    "MODEL_PROFILE_REGISTRY",
    "OPENAI_DEFAULT_MODEL_ID",
    "OPENAI_EXACT_MODEL_IDS",
    "OPENAI_LEGACY_CHAT_MODEL_IDS",
    "OPENAI_LISTABLE_MODEL_IDS",
    "OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS",
    "ProviderProfile",
    "ModelProfile",
    "ModelProfileRegistry",
    "get_default_model_ref",
    "list_catalog_model_profiles",
    "list_model_profiles",
    "require_provider_profile",
    "require_model_profile",
    "supports_provider",
    "supports_model",
    "require_provider_capability",
    "require_model_capability",
    "resolve_context_window_tokens",
    "resolve_max_output_tokens",
    "FunctionToolSpec",
    "TOOL_CHOICE_MODES",
    "ToolChoice",
    "ToolChoiceMode",
    "freeze_tool_choice_modes",
    "function_tool_spec_from_openai_dict",
    "normalize_tool_choice_mode",
    "STRUCTURED_OUTPUT_STRATEGIES",
    "StructuredOutputFallbackPolicy",
    "StructuredOutputStrategy",
    "StructuredOutputStrategySelection",
    "freeze_structured_output_strategies",
    "normalize_structured_output_strategy",
    "select_structured_output_strategy",
    # Legacy data types
    "ToolCall",
    "ToolCallResult",
    # Backward compatibility helpers
    "extract_content",
    "extract_usage",
    "has_usage",
    # Exceptions
    "LLMProviderError",
    "LLMConfigurationError",
    "LLMAPIError",
    "LLMResponseError",
    "LLMRefusalError",
    "LLMRefusalOutcome",
    "LLMProviderNotFoundError",
    "LLMProfileNotFoundError",
    "LLMCapabilityNotSupportedError",
]
