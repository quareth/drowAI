"""Baseline tests for guarded provider health and lifecycle side paths."""

from __future__ import annotations

from typing import Any

from backend.services.llm_provider.conversation_lifecycle_service import (
    LLMConversationLifecycleService,
)
from backend.services.llm_provider.health_service import LLMProviderHealthService
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionOperation,
)

PHASE_1_GUARDED_EGRESS_SIDE_PATH = "phase_1_guarded_egress_side_path"


class _Transport:
    """Recording guarded transport used by the historical side-path baseline."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def execute(self, operation: Any, **kwargs: Any) -> GuardedHTTPResponse:
        self.calls.append((operation, kwargs))
        body = (
            b'{"id":"conv-created"}'
            if operation == LLMConnectionOperation.LIFECYCLE_CREATE
            else b'{"data":[{}]}'
        )
        return GuardedHTTPResponse(
            status_code=200,
            body=body,
            audit_id="baseline-audit",
        )


def test_openai_health_check_uses_guarded_models_operation() -> None:
    """The historical OpenAI health side path now traverses guarded egress."""

    transport = _Transport()
    service = object.__new__(LLMProviderHealthService)
    service._guarded_transport = transport  # type: ignore[attr-defined]

    result = service._test_openai_key("sk-health")

    assert result.status == "success"
    assert transport.calls[0][0] == LLMConnectionOperation.HEALTH
    assert PHASE_1_GUARDED_EGRESS_SIDE_PATH == "phase_1_guarded_egress_side_path"


def test_anthropic_health_check_uses_guarded_models_operation() -> None:
    """The historical Anthropic health side path now traverses guarded egress."""

    transport = _Transport()
    service = object.__new__(LLMProviderHealthService)
    service._guarded_transport = transport  # type: ignore[attr-defined]

    result = service._test_anthropic_key("sk-ant-health")

    assert result.status == "success"
    assert transport.calls[0][0] == LLMConnectionOperation.HEALTH
    assert PHASE_1_GUARDED_EGRESS_SIDE_PATH == "phase_1_guarded_egress_side_path"


def test_openai_conversation_lifecycle_uses_guarded_create_and_delete() -> None:
    """The historical lifecycle side path now uses registered operations."""

    transport = _Transport()
    service = object.__new__(LLMConversationLifecycleService)
    service._guarded_transport = transport  # type: ignore[attr-defined]

    created_id = service._create_openai_conversation("sk-life")
    service._delete_openai_conversation("sk-life", created_id)

    assert created_id == "conv-created"
    assert [call[0] for call in transport.calls] == [
        LLMConnectionOperation.LIFECYCLE_CREATE,
        LLMConnectionOperation.LIFECYCLE_DELETE,
    ]
    assert PHASE_1_GUARDED_EGRESS_SIDE_PATH == "phase_1_guarded_egress_side_path"
