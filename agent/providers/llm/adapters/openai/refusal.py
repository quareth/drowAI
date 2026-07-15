"""Normalize documented OpenAI refusal signals into the neutral LLM contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...core.exceptions import LLMRefusalError, LLMRefusalOutcome
from ...core.identity import OPENAI_PROVIDER_ID

_CONTENT_FILTER_CATEGORY = "content_filter"


def _read(value: Any, key: str, default: Any = None) -> Any:
    """Read one field from SDK objects or mappings."""
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _text(value: Any) -> str | None:
    """Extract documented refusal text without stringifying arbitrary objects."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("refusal", "text", "value", "content"):
            candidate = _text(value.get(key))
            if candidate:
                return candidate
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [part for item in value if (part := _text(item))]
        return "".join(parts).strip() or None
    for key in ("refusal", "text", "value"):
        candidate = getattr(value, key, None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _response_id(response: Any) -> str | None:
    value = _read(response, "id") or _read(response, "response_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def raise_for_openai_chat_refusal(
    response: Any,
    *,
    model: str,
    usage: Any = None,
    partial_content: str | None = None,
) -> None:
    """Raise for Chat Completions refusal fields or content-filter finish reasons."""
    choices = _read(response, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return
    choice = choices[0]
    finish_reason = _read(choice, "finish_reason")
    normalized_finish_reason = (
        finish_reason.strip().lower() if isinstance(finish_reason, str) else ""
    )
    message = _read(choice, "message")
    explanation = _text(_read(message, "refusal")) if message is not None else None
    if explanation is None and normalized_finish_reason != "content_filter":
        return
    raise LLMRefusalError(
        "OpenAI declined the request",
        outcome=LLMRefusalOutcome(
            provider=OPENAI_PROVIDER_ID,
            model=model,
            category=_CONTENT_FILTER_CATEGORY,
            explanation=explanation,
            response_id=_response_id(response),
            usage=usage,
            partial_content=partial_content or None,
        ),
    )


def inspect_openai_chat_stream_chunk(
    chunk: Any,
    *,
    refusal_parts: list[str],
) -> bool:
    """Collect streamed refusal text and return whether the chunk is terminal."""
    choices = _read(chunk, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return False
    choice = choices[0]
    delta = _read(choice, "delta")
    raw_explanation = _read(delta, "refusal") if delta is not None else None
    explanation = (
        raw_explanation
        if isinstance(raw_explanation, str) and raw_explanation
        else _text(raw_explanation)
    )
    if explanation:
        refusal_parts.append(explanation)
    finish_reason = _read(choice, "finish_reason")
    normalized_finish_reason = (
        finish_reason.strip().lower() if isinstance(finish_reason, str) else ""
    )
    return normalized_finish_reason == "content_filter" or bool(
        refusal_parts and normalized_finish_reason
    )


def raise_for_openai_chat_stream_refusal(
    chunk: Any,
    *,
    model: str,
    refusal_parts: list[str],
    usage: Any = None,
    partial_content: str | None = None,
    refusal_detected: bool = False,
) -> None:
    """Raise a normalized refusal after a structured Chat stream signal."""
    if not refusal_detected and not inspect_openai_chat_stream_chunk(
        chunk,
        refusal_parts=refusal_parts,
    ):
        return
    raise LLMRefusalError(
        "OpenAI declined the request",
        outcome=LLMRefusalOutcome(
            provider=OPENAI_PROVIDER_ID,
            model=model,
            category=_CONTENT_FILTER_CATEGORY,
            explanation="".join(refusal_parts).strip() or None,
            response_id=_response_id(chunk),
            usage=usage,
            partial_content=partial_content or None,
        ),
    )


def _responses_refusal_explanation(response: Any) -> str | None:
    output = _read(response, "output")
    if not isinstance(output, Sequence):
        return None
    for item in output:
        if _read(item, "type") == "refusal":
            explanation = _text(_read(item, "refusal")) or _text(item)
            if explanation:
                return explanation
        content = _read(item, "content")
        if not isinstance(content, Sequence):
            continue
        for part in content:
            if _read(part, "type") != "refusal":
                continue
            explanation = _text(_read(part, "refusal")) or _text(part)
            if explanation:
                return explanation
    return None


def _is_content_filter_incomplete(response: Any) -> bool:
    details = _read(response, "incomplete_details")
    reason = _read(details, "reason") if details is not None else None
    return isinstance(reason, str) and reason.strip().lower() == "content_filter"


def raise_for_openai_responses_refusal(
    response: Any,
    *,
    model: str,
    usage: Any = None,
    partial_content: str | None = None,
    explanation: str | None = None,
) -> None:
    """Raise for Responses refusal blocks or content-filter incompletion."""
    resolved_explanation = explanation or _responses_refusal_explanation(response)
    if resolved_explanation is None and not _is_content_filter_incomplete(response):
        return
    raise LLMRefusalError(
        "OpenAI declined the request",
        outcome=LLMRefusalOutcome(
            provider=OPENAI_PROVIDER_ID,
            model=model,
            category=_CONTENT_FILTER_CATEGORY,
            explanation=resolved_explanation,
            response_id=_response_id(response),
            usage=usage,
            partial_content=partial_content or None,
        ),
    )


def collect_openai_responses_stream_refusal(
    event: Any,
    *,
    refusal_parts: list[str],
) -> tuple[bool, Any | None]:
    """Collect refusal events and return a response-bearing terminal signal."""
    event_type = _read(event, "type")
    normalized = event_type.strip().lower() if isinstance(event_type, str) else ""
    if normalized == "response.refusal.delta":
        delta = _text(_read(event, "delta"))
        if delta:
            refusal_parts.append(delta)
        return False, None
    if normalized == "response.refusal.done":
        final_text = _text(_read(event, "refusal"))
        if final_text:
            refusal_parts[:] = [final_text]
        return False, _read(event, "response")
    response = _read(event, "response")
    is_terminal_response_event = normalized in {
        "response.completed",
        "response.done",
        "response.incomplete",
    }
    if response is not None and is_terminal_response_event and (
        refusal_parts
        or _responses_refusal_explanation(response) is not None
        or _is_content_filter_incomplete(response)
    ):
        return True, response
    return False, response


def raise_for_openai_responses_stream_refusal(
    event: Any,
    *,
    model: str,
    refusal_parts: list[str],
    usage: Any = None,
    partial_content: str | None = None,
) -> None:
    """Raise when a Responses stream reaches a documented refusal boundary."""
    terminal, response = collect_openai_responses_stream_refusal(
        event,
        refusal_parts=refusal_parts,
    )
    if not terminal:
        return
    raise_for_openai_responses_refusal(
        response or event,
        model=model,
        usage=usage,
        partial_content=partial_content,
        explanation="".join(refusal_parts).strip() or None,
    )


__all__ = [
    "collect_openai_responses_stream_refusal",
    "inspect_openai_chat_stream_chunk",
    "raise_for_openai_chat_refusal",
    "raise_for_openai_chat_stream_refusal",
    "raise_for_openai_responses_refusal",
    "raise_for_openai_responses_stream_refusal",
]
