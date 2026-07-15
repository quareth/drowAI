"""
Typed contracts for context-compression policy, requests, and outcomes.

This module defines the shared typed payloads used by context-compression
orchestration, including strict percentage-based policy validation for dynamic
threshold computation from `max_tokens`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

CompressionPassName = Literal["pass1", "pass2", "degraded"]


class CompressionRequiredError(RuntimeError):
    """Raised when required compression cannot be completed safely."""

    def __init__(self, reason: str, detail: Optional[str] = None) -> None:
        self.reason = reason.strip() if isinstance(reason, str) and reason.strip() else "compression_required_failed"
        self.detail = detail
        message = self.reason if not detail else f"{self.reason}: {detail}"
        super().__init__(message)


@dataclass(slots=True, frozen=True)
class CompressionPolicy:
    """Percentage-based policy inputs for compression trigger/target math."""

    trigger_percent: int = 100
    target_min_percent: int = 20
    target_max_percent: int = 30

    def __post_init__(self) -> None:
        _validate_percent(self.trigger_percent, "trigger_percent")
        _validate_percent(self.target_min_percent, "target_min_percent")
        _validate_percent(self.target_max_percent, "target_max_percent")
        if self.target_min_percent > self.target_max_percent:
            raise ValueError("target_min_percent must be <= target_max_percent")
        if self.target_max_percent > self.trigger_percent:
            raise ValueError("target_max_percent must be <= trigger_percent")


@dataclass(slots=True, frozen=True)
class ContextCompressionRequest:
    """Input contract for one context-compression attempt."""

    task_id: int
    conversation_id: str
    max_tokens: int
    model: str
    conversation_history: list[dict[str, Any]]
    provider: str = "openai"
    credential_ref: Optional[dict[str, Any]] = None
    projected_user_message: Optional[str] = None
    policy: CompressionPolicy = field(default_factory=CompressionPolicy)

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be > 0")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider is required")


@dataclass(slots=True, frozen=True)
class CompressionPassResult:
    """Result contract for a single compression pass execution."""

    pass_name: CompressionPassName
    system_template_id: str
    user_template_id: str
    output_text: str
    output_tokens: int
    target_max_tokens: int
    within_target: bool

    def __post_init__(self) -> None:
        if self.output_tokens < 0:
            raise ValueError("output_tokens must be >= 0")
        if self.target_max_tokens <= 0:
            raise ValueError("target_max_tokens must be > 0")


@dataclass(slots=True, frozen=True)
class ContextCompressionOutcome:
    """Final normalized compression outcome metadata for orchestration."""

    request: ContextCompressionRequest
    original_tokens: int
    final_tokens: int
    final_text: str
    pass_results: tuple[CompressionPassResult, ...]
    pass_count: int
    degraded: bool
    fallback_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.original_tokens < 0:
            raise ValueError("original_tokens must be >= 0")
        if self.final_tokens < 0:
            raise ValueError("final_tokens must be >= 0")
        if self.pass_count <= 0:
            raise ValueError("pass_count must be > 0")
        if self.pass_count != len(self.pass_results):
            raise ValueError("pass_count must equal number of pass_results")


@dataclass(slots=True, frozen=True)
class CompressionEpochMetadata:
    """Persisted compression epoch and exact summarized-through cutoff."""

    epoch_id: str
    source_tokens: int
    through_message_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.epoch_id or not self.epoch_id.strip():
            raise ValueError("epoch_id is required")
        if self.source_tokens < 0:
            raise ValueError("source_tokens must be >= 0")
        if self.through_message_id is not None and (
            isinstance(self.through_message_id, bool)
            or not isinstance(self.through_message_id, int)
            or self.through_message_id <= 0
        ):
            raise ValueError("through_message_id must be a positive integer")


def _validate_percent(value: int, field_name: str) -> None:
    if value <= 0 or value > 100:
        raise ValueError(f"{field_name} must be within 1..100")


__all__ = [
    "CompressionRequiredError",
    "CompressionPassName",
    "CompressionPolicy",
    "ContextCompressionRequest",
    "CompressionPassResult",
    "ContextCompressionOutcome",
    "CompressionEpochMetadata",
]
