"""Unit tests for context-compression typed contracts and policy validation."""

from __future__ import annotations

import pytest

from backend.services.langgraph_chat.compression.context_models import (
    CompressionEpochMetadata,
    CompressionPassResult,
    CompressionPolicy,
    ContextCompressionOutcome,
    ContextCompressionRequest,
)


def test_compression_policy_valid_defaults() -> None:
    policy = CompressionPolicy()
    assert policy.trigger_percent == 100
    assert policy.target_min_percent == 20
    assert policy.target_max_percent == 30


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        ({"trigger_percent": 0}, "trigger_percent"),
        ({"target_min_percent": 0}, "target_min_percent"),
        ({"target_max_percent": 101}, "target_max_percent"),
        ({"target_min_percent": 40, "target_max_percent": 30}, "target_min_percent"),
        ({"trigger_percent": 50, "target_max_percent": 60}, "target_max_percent"),
    ],
)
def test_compression_policy_rejects_invalid_ranges(kwargs: dict, expected_error: str) -> None:
    with pytest.raises(ValueError, match=expected_error):
        CompressionPolicy(**kwargs)


def test_context_compression_request_requires_positive_max_tokens() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        ContextCompressionRequest(
            task_id=1,
            conversation_id="conv-1",
            max_tokens=0,
            model="gpt-4o-mini",
            conversation_history=[],
        )


def test_context_compression_request_defaults_provider_to_openai() -> None:
    request = ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=128_000,
        model="gpt-5.2",
        conversation_history=[],
    )

    assert request.provider == "openai"


def test_context_compression_outcome_validates_pass_count_consistency() -> None:
    request = ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=128_000,
        model="gpt-4o-mini",
        conversation_history=[{"role": "user", "content": "hello"}],
    )
    pass1 = CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text="Facts: ...",
        output_tokens=200,
        target_max_tokens=300,
        within_target=True,
    )

    with pytest.raises(ValueError, match="pass_count"):
        ContextCompressionOutcome(
            request=request,
            original_tokens=1200,
            final_tokens=200,
            final_text="Facts: ...",
            pass_results=(pass1,),
            pass_count=2,
            degraded=False,
        )


def test_compression_epoch_metadata_validates_fields() -> None:
    metadata = CompressionEpochMetadata(
        epoch_id="epoch-1",
        source_tokens=100,
        through_message_id=77,
    )
    assert metadata.epoch_id == "epoch-1"
    assert metadata.source_tokens == 100
    assert metadata.through_message_id == 77

    with pytest.raises(ValueError, match="epoch_id"):
        CompressionEpochMetadata(epoch_id="", source_tokens=100)

    with pytest.raises(ValueError, match="source_tokens"):
        CompressionEpochMetadata(epoch_id="epoch-2", source_tokens=-1)

    with pytest.raises(ValueError, match="through_message_id"):
        CompressionEpochMetadata(
            epoch_id="epoch-2",
            source_tokens=100,
            through_message_id=0,
        )
