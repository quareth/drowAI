"""Centralized LLMClient resolution for graph nodes.

This module provides a single entry point for obtaining LLMClients within
LangGraph nodes. All nodes should use this instead of directly importing
provider classes, ensuring consistent resolution and clean architecture.

Graph nodes receive live clients through the backend-supplied, turn-local
runtime services bag in LangGraph config. Metadata and graph context carry
only provider/model/credential references; they must not carry decrypted
provider credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Optional, cast, get_args

# Import directly from llm subpackage to avoid triggering agent.providers.__init__
# which has heavy dependencies (executor, reasoning, etc.)
from agent.providers.llm.core.base import LLMClient
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from agent.graph.utils.provider_model_resolution import (
    resolve_graph_provider_model_ref,
    resolve_graph_reasoning_provider_model_ref,
)
from core.llm import (
    ModelRoleRegistry,
    ROLE_CONVERSATION_MAIN,
    ROLE_INTENT_CLASSIFIER,
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_POST_TOOL_OBSERVATION,
    ROLE_REASONING_MAIN,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
    RoleKey,
)

if TYPE_CHECKING:
    from agent.graph.infrastructure.state_models import GraphRuntimeContext

logger = logging.getLogger(__name__)

# Default model to use if none specified
DEFAULT_MODEL = "gpt-5.2"
DEFAULT_ROLE = cast(str, ROLE_CONVERSATION_MAIN)

_ROLE_REGISTRY = ModelRoleRegistry()
_ROLE_KEYS: FrozenSet[str] = frozenset(get_args(RoleKey))


def resolve_llm_client(
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    role: str = DEFAULT_ROLE,
    default_model: str = DEFAULT_MODEL,
) -> LLMClient:
    """Resolve an LLMClient from turn-local runtime services.
    
    This is the single source of truth for LLMClient creation in graph nodes.
    It provides a consistent resolution order and clear error messages when
    required configuration is missing.
    
    Resolution Order for Model:
        1. metadata["model"]
        2. metadata["runtime_model"]
        3. context.model (if context provided)
        4. default_model parameter
    
    Args:
        metadata: Node metadata dictionary (typically from state.facts.metadata)
        context: Optional GraphRuntimeContext with runtime configuration
        default_model: Fallback model if none specified (default: "gpt-5.2")
        
    Returns:
        Configured LLMClient instance ready for use
        
    Raises:
        LLMConfigurationError: If no runtime services or selection are available
        LLMProviderNotFoundError: If the model has no registered provider
        
    Example:
        # In a graph node:
        async def my_node(state, context, config):
            interactive = InteractiveState.from_mapping(state)
            metadata = interactive.facts.safe_metadata

            llm_client = resolve_llm_client(metadata, context, config=config)
            response = await llm_client.chat("You are helpful.", "Hello!")
    """
    llm_client, _ = resolve_llm_client_with_settings(
        metadata=metadata,
        context=context,
        config=config,
        role=role,
        default_model=default_model,
    )
    return llm_client


def resolve_llm_client_with_settings(
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    role: str = DEFAULT_ROLE,
    default_model: str = DEFAULT_MODEL,
) -> tuple[LLMClient, Any]:
    """Resolve LLMClient and role call settings for invocation kwargs."""
    call_settings = _resolve_call_settings(
        metadata=metadata,
        context=context,
        role=role,
        default_model=default_model,
    )
    provider_model = ProviderModelRef(call_settings.provider, call_settings.model)

    runtime_client = _resolve_client_from_runtime_services(
        metadata=metadata,
        context=context,
        config=config,
        call_settings=call_settings,
        role=role,
    )
    if runtime_client is not None:
        return runtime_client, call_settings

    raise LLMConfigurationError(
        "LLMClient requires provider runtime services. Attach "
        "config['configurable']['runtime_services'] with "
        "config['configurable']['llm_runtime_selection']; raw API keys in "
        "metadata or graph context are not supported.",
        provider=provider_model.provider,
    )


def resolve_llm_call_settings(
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"] = None,
    *,
    role: str = DEFAULT_ROLE,
    default_model: str = DEFAULT_MODEL,
) -> Any:
    """Resolve role call settings without constructing an LLMClient."""
    return _resolve_call_settings(
        metadata=metadata,
        context=context,
        role=role,
        default_model=default_model,
    )


def _resolve_client_from_runtime_services(
    *,
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"],
    config: Optional[Mapping[str, Any]],
    call_settings: Any,
    role: str,
) -> Optional[LLMClient]:
    configurable = _configurable(config)
    runtime_services = configurable.get("runtime_services")
    if runtime_services is None:
        return None
    client_resolver = getattr(runtime_services, "client_resolver", None)
    if client_resolver is None:
        raise LLMConfigurationError("runtime_services is missing client_resolver", provider=None)

    selection = configurable.get("llm_runtime_selection")
    if selection is None:
        raise LLMConfigurationError("LLM runtime selection is missing from graph config", provider=None)

    runtime_user_id = _resolve_runtime_user_id(configurable)
    task_id = _resolve_task_id(configurable)
    return client_resolver.get_client(
        selection,
        target=call_settings,
        runtime_user_id=runtime_user_id,
        task_id=task_id,
        purpose=f"graph:{_normalize_role(role)}",
        resolution_role=_normalize_role(role),
        resolution_source=getattr(call_settings, "source", None),
    )


def _resolve_call_settings(
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"],
    *,
    role: str,
    default_model: str,
) -> Any:
    resolved_role = _normalize_role(role)
    conversation_ref = resolve_graph_provider_model_ref(
        metadata,
        context,
        default_model=default_model,
    )
    if conversation_ref is None:
        conversation_ref = ProviderModelRef(OPENAI_PROVIDER_ID, default_model)
    reasoning_ref = resolve_graph_reasoning_provider_model_ref(metadata, context)
    reasoning_effort = _resolve_reasoning_effort(metadata, context)

    return _ROLE_REGISTRY.resolve_call_settings(
        resolved_role,
        conversation_provider=conversation_ref.provider,
        conversation_model=conversation_ref.model,
        reasoning_provider=reasoning_ref.provider if reasoning_ref is not None else None,
        reasoning_model=reasoning_ref.model if reasoning_ref is not None else None,
        reasoning_effort=reasoning_effort,
    )


def _normalize_role(role: str) -> str:
    if role in _ROLE_KEYS:
        return role

    logger.error(
        "Unknown LLM resolver role '%s'; failing fast",
        role,
    )
    allowed_roles = ", ".join(sorted(_ROLE_KEYS))
    raise LLMConfigurationError(
        f"Unknown role '{role}'. Allowed roles: {allowed_roles}",
        provider=None,
    )


def _configurable(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    configurable = config.get("configurable")
    return dict(configurable) if isinstance(configurable, Mapping) else {}


def _resolve_runtime_user_id(configurable: Mapping[str, Any]) -> int:
    runtime_projection = configurable.get("runtime_projection")
    if isinstance(runtime_projection, Mapping):
        projected_user_id = runtime_projection.get("user_id")
        if isinstance(projected_user_id, int):
            return projected_user_id
        if isinstance(projected_user_id, str) and projected_user_id.isdigit():
            return int(projected_user_id)
    raise LLMConfigurationError("Runtime user id is required for provider credential resolution", provider=None)


def _resolve_task_id(configurable: Mapping[str, Any]) -> Optional[int]:
    runtime_projection = configurable.get("runtime_projection")
    if isinstance(runtime_projection, Mapping):
        projected_task_id = runtime_projection.get("task_id")
        if isinstance(projected_task_id, int):
            return projected_task_id
        if isinstance(projected_task_id, str) and projected_task_id.isdigit():
            return int(projected_task_id)
    return None


def _resolve_model(
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"],
    default_model: str,
) -> str:
    """Resolve model identifier from metadata, context, or default.
    
    Args:
        metadata: Node metadata dictionary
        context: Optional runtime context
        default_model: Fallback model name
        
    Returns:
        Model identifier string
    """
    # Priority 1: metadata["model"]
    model = metadata.get("model")
    if _is_valid_string(model):
        logger.debug(f"Model resolved from metadata['model']: {model}")
        return model
    
    # Priority 2: metadata["runtime_model"]
    model = metadata.get("runtime_model")
    if _is_valid_string(model):
        logger.debug(f"Model resolved from metadata['runtime_model']: {model}")
        return model
    
    # Priority 3: context.model
    if context is not None:
        model = getattr(context, "model", None)
        if _is_valid_string(model):
            logger.debug(f"Model resolved from context.model: {model}")
            return model
    
    # Priority 4: default
    logger.debug(f"Using default model: {default_model}")
    return default_model


def _resolve_reasoning_effort(
    metadata: Dict[str, Any],
    context: Optional["GraphRuntimeContext"],
) -> Optional[str]:
    """Resolve optional reasoning-effort override for role-aware resolution."""
    effort = metadata.get("reasoning_effort")
    if _is_valid_string(effort):
        return effort

    effort = metadata.get("runtime_reasoning_effort")
    if _is_valid_string(effort):
        return effort

    if context is not None:
        effort = getattr(context, "reasoning_effort", None)
        if _is_valid_string(effort):
            return effort

    return None


def get_llm_reasoning_effort(
    llm_client: LLMClient,
    call_settings: Any = None,
) -> Optional[str]:
    """Return resolved reasoning effort without graph nodes reading adapter internals."""
    if call_settings is not None:
        effort = getattr(call_settings, "reasoning_effort", None)
        if _is_valid_string(effort):
            return str(effort).strip()
    effort = getattr(llm_client, "_reasoning_effort", None)
    if _is_valid_string(effort):
        return str(effort).strip()
    return None


def supports_usage_aware_streaming(
    llm_client: LLMClient,
    call_settings: Any,
) -> bool:
    """Return True when the resolved model and client support final stream usage."""
    if not hasattr(llm_client, "stream_chat_messages_with_usage"):
        return False

    provider = getattr(call_settings, "provider", None)
    model = getattr(call_settings, "model", None)
    if not (_is_valid_string(provider) and _is_valid_string(model)):
        logger.debug("Missing call settings for streaming usage capability check")
        return False

    try:
        profile = require_model_profile(ProviderModelRef(str(provider), str(model)))
    except Exception as exc:
        logger.debug(
            "Could not resolve model profile for streaming usage check: provider=%s model=%s error=%s",
            provider,
            model,
            exc,
        )
        return False
    return profile.supports(LLMCapability.STREAMING_USAGE_REPORTING)


def has_llm_runtime_services(config: Optional[Mapping[str, Any]]) -> bool:
    """Return whether a graph invocation supplied the live LLM runtime boundary."""

    configurable = _configurable(config)
    return configurable.get("runtime_services") is not None


def _is_valid_string(value: Any) -> bool:
    """Check if value is a non-empty string.
    
    Args:
        value: Value to check
        
    Returns:
        True if value is a non-empty string after stripping whitespace
    """
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "resolve_llm_client",
    "resolve_llm_client_with_settings",
    "resolve_llm_call_settings",
    "get_llm_reasoning_effort",
    "has_llm_runtime_services",
    "supports_usage_aware_streaming",
    "DEFAULT_ROLE",
    "ROLE_CONVERSATION_MAIN",
    "ROLE_INTENT_CLASSIFIER",
    "ROLE_POST_TOOL_ARTICULATOR",
    "ROLE_POST_TOOL_OBSERVATION",
    "ROLE_REASONING_MAIN",
    "ROLE_TOOL_CATEGORY_SELECTOR",
    "ROLE_TOOL_OUTPUT_COMPRESSOR",
]
