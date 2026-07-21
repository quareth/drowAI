"""Compatibility response helpers and data-only LLM dialect contracts.

This module provides helper functions for gradual migration from
string-based responses to LLMResponse objects with usage tracking.
It also defines immutable protocol policy data used to validate optional call
features without carrying transport, credential, or executable behavior.

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

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Iterable, Optional, Union

from ..core.base import LLMCallOptions, LLMResponse
from ..core.capabilities import (
    CapabilityInput,
    LLMCapability,
    freeze_capabilities,
    require_capabilities,
)
from ..core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from .structured_output_strategy import (
    freeze_structured_output_strategies,
    normalize_structured_output_strategy,
)
from .tool_contracts import freeze_tool_choice_modes, normalize_tool_choice_mode

if TYPE_CHECKING:
    from backend.services.usage_tracking.models import UsageData


_POLICY_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class LLMDialectPolicy:
    """Immutable non-secret constraints for one registered adapter dialect."""

    policy_id: str
    adapter_id: str
    api_surface: str
    capabilities: frozenset[LLMCapability]
    tool_choice_modes: frozenset[str] = frozenset()
    structured_output_strategies: frozenset[str] = frozenset()
    reasoning_efforts: frozenset[str] = frozenset()
    max_retry_attempts: int = 0

    def __post_init__(self) -> None:
        for field_name in ("policy_id", "adapter_id", "api_surface"):
            object.__setattr__(
                self,
                field_name,
                _normalize_policy_identifier(getattr(self, field_name), field_name),
            )

        capabilities = freeze_capabilities(self.capabilities)
        tool_modes = freeze_tool_choice_modes(self.tool_choice_modes)
        structured_strategies = freeze_structured_output_strategies(
            self.structured_output_strategies
        )
        reasoning_efforts = frozenset(
            _normalize_policy_identifier(effort, "reasoning_effort")
            for effort in self.reasoning_efforts
        )
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "tool_choice_modes", tool_modes)
        object.__setattr__(
            self,
            "structured_output_strategies",
            structured_strategies,
        )
        object.__setattr__(self, "reasoning_efforts", reasoning_efforts)

        if isinstance(self.max_retry_attempts, bool) or not isinstance(
            self.max_retry_attempts, int
        ):
            raise TypeError("max_retry_attempts must be an integer")
        if self.max_retry_attempts < 0:
            raise ValueError("max_retry_attempts must be non-negative")

        _validate_policy_consistency(
            capabilities=capabilities,
            tool_choice_modes=tool_modes,
            structured_output_strategies=structured_strategies,
            reasoning_efforts=reasoning_efforts,
        )

    def validate_adapter_binding(
        self,
        *,
        expected_adapter_id: str,
        allowed_capabilities: Iterable[CapabilityInput],
        allowed_tool_choice_modes: Iterable[str] = (),
        allowed_structured_output_strategies: Iterable[str] = (),
        allowed_reasoning_efforts: Iterable[str] = (),
        max_retry_attempts: int = 0,
    ) -> None:
        """Reject policy data that targets or expands another executable adapter."""

        normalized_adapter_id = _normalize_policy_identifier(
            expected_adapter_id,
            "expected_adapter_id",
        )
        if self.adapter_id != normalized_adapter_id:
            raise LLMConfigurationError(
                (
                    f"Dialect policy adapter '{self.adapter_id}' does not match "
                    f"registered adapter '{normalized_adapter_id}'"
                )
            )

        allowed = freeze_capabilities(allowed_capabilities)
        excess = sorted(
            self.capabilities - allowed,
            key=lambda capability: capability.value,
        )
        if excess:
            names = ", ".join(capability.value for capability in excess)
            raise LLMConfigurationError(
                f"Dialect policy capabilities exceed adapter registration: {names}"
            )

        _require_policy_subset(
            field_name="tool_choice_modes",
            values=self.tool_choice_modes,
            allowed=freeze_tool_choice_modes(allowed_tool_choice_modes),
        )
        _require_policy_subset(
            field_name="structured_output_strategies",
            values=self.structured_output_strategies,
            allowed=freeze_structured_output_strategies(
                allowed_structured_output_strategies
            ),
        )
        _require_policy_subset(
            field_name="reasoning_efforts",
            values=self.reasoning_efforts,
            allowed=frozenset(
                _normalize_policy_identifier(effort, "reasoning_effort")
                for effort in allowed_reasoning_efforts
            ),
        )
        if self.max_retry_attempts > max_retry_attempts:
            raise LLMConfigurationError(
                (
                    "Dialect policy retry limit exceeds adapter registration: "
                    f"{self.max_retry_attempts} > {max_retry_attempts}"
                )
            )

    def validate_call_options(
        self,
        options: LLMCallOptions,
        *,
        required_capabilities: Iterable[CapabilityInput] = (),
    ) -> None:
        """Validate one typed call against this dialect before outbound work."""

        if not isinstance(options, LLMCallOptions):
            raise TypeError("options must be LLMCallOptions")

        required = list(required_capabilities)
        if options.tool_choice_mode is not None:
            required.append(LLMCapability.TOOLS)
            mode = normalize_tool_choice_mode(options.tool_choice_mode)
            if mode not in self.tool_choice_modes:
                raise LLMCapabilityNotSupportedError(
                    f"Dialect policy does not support tool choice mode '{mode}'",
                    capability=f"tool_choice:{mode}",
                )

        if options.parallel_tool_calls is not None:
            required.append(LLMCapability.PARALLEL_TOOLS)

        if options.structured_output_strategy is not None:
            strategy = normalize_structured_output_strategy(
                options.structured_output_strategy
            )
            if strategy not in self.structured_output_strategies:
                raise LLMCapabilityNotSupportedError(
                    f"Dialect policy does not support structured strategy '{strategy}'",
                    capability=f"structured_output:{strategy}",
                )
            if strategy == "native_schema":
                required.append(LLMCapability.STRUCTURED_OUTPUT_NATIVE)
            elif strategy in {"strict_tool", "non_strict_tool"}:
                required.append(LLMCapability.STRUCTURED_OUTPUT_TOOL_FALLBACK)

        if options.include_stream_usage:
            required.append(LLMCapability.STREAMING_USAGE_REPORTING)

        if options.reasoning_effort is not None:
            required.append(LLMCapability.REASONING_EFFORT)
            if options.reasoning_effort not in self.reasoning_efforts:
                raise LLMCapabilityNotSupportedError(
                    (
                        "Dialect policy does not support reasoning effort "
                        f"'{options.reasoning_effort}'"
                    ),
                    capability=f"reasoning_effort:{options.reasoning_effort}",
                )

        if (
            options.retry_attempts is not None
            and options.retry_attempts > self.max_retry_attempts
        ):
            raise LLMConfigurationError(
                (
                    f"Requested retry_attempts={options.retry_attempts} exceeds "
                    f"dialect retry limit {self.max_retry_attempts}"
                )
            )

        require_capabilities(
            self.capabilities,
            required,
            provider=self.adapter_id,
        )


def _normalize_policy_identifier(value: str, field_name: str) -> str:
    """Normalize a safe code-owned identifier without accepting URL syntax."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip().lower()
    if not _POLICY_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must contain only lowercase identifier characters"
        )
    return normalized


def _require_policy_subset(
    *,
    field_name: str,
    values: frozenset[str],
    allowed: frozenset[str],
) -> None:
    """Reject dialect constraints not registered by executable adapter code."""

    excess = sorted(values - allowed)
    if excess:
        raise LLMConfigurationError(
            (
                f"Dialect policy {field_name} exceed adapter registration: "
                f"{', '.join(excess)}"
            )
        )


def _validate_policy_consistency(
    *,
    capabilities: frozenset[LLMCapability],
    tool_choice_modes: frozenset[str],
    structured_output_strategies: frozenset[str],
    reasoning_efforts: frozenset[str],
) -> None:
    """Reject internally contradictory dialect policy declarations."""

    if tool_choice_modes and LLMCapability.TOOLS not in capabilities:
        raise ValueError("tool_choice_modes require the tools capability")
    if LLMCapability.PARALLEL_TOOLS in capabilities and LLMCapability.TOOLS not in capabilities:
        raise ValueError("parallel_tools requires the tools capability")
    if reasoning_efforts and LLMCapability.REASONING_EFFORT not in capabilities:
        raise ValueError("reasoning_efforts require the reasoning_effort capability")
    if (
        LLMCapability.STREAMING_USAGE_REPORTING in capabilities
        and LLMCapability.STREAMING not in capabilities
    ):
        raise ValueError("streaming usage reporting requires streaming capability")
    if (
        "native_schema" in structured_output_strategies
        and LLMCapability.STRUCTURED_OUTPUT_NATIVE not in capabilities
    ):
        raise ValueError("native_schema requires structured_output_native capability")
    if (
        {"strict_tool", "non_strict_tool"} & structured_output_strategies
        and LLMCapability.STRUCTURED_OUTPUT_TOOL_FALLBACK not in capabilities
    ):
        raise ValueError(
            "tool structured strategies require structured_output_tool_fallback capability"
        )


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
    "LLMDialectPolicy",
    "extract_content",
    "extract_usage", 
    "has_usage",
]
