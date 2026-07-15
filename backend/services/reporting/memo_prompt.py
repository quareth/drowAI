"""Render task closure memo prompts from bounded reporting context packets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from core.prompts.registry import PromptRegistry

from backend.services.reporting.contracts import (
    GENERATION_METADATA_PROMPT_FAMILY_KEY,
    GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY,
    GENERATION_METADATA_PROMPT_VERSION_KEY,
    TASK_CLOSURE_MEMO_CONTRACTS,
)
from backend.services.reporting.task_memo_context_builder import TaskMemoContext
from backend.services.reporting.report_tool_display import report_tool_display_name

_DEFAULT_MAX_STRING_CHARACTERS = 2_000
_STRING_LIMITS_BY_KEY = {
    "text": 1_200,
    "excerpt": 1_500,
    "summary": 2_000,
    "description": 2_000,
    "scope": 2_000,
    "body": 4_000,
}
_TRUNCATION_MARKER = "...[truncated]"


@dataclass(frozen=True, slots=True)
class RenderedTaskClosureMemoPrompt:
    """Prompt messages and safe prompt metadata for memo generation."""

    system_prompt: str
    user_prompt: str
    metadata: Mapping[str, Any]
    memo_context_json: str


class TaskClosureMemoPromptRenderer:
    """Build task closure memo prompts without touching LLM runtime clients."""

    def __init__(self, prompt_registry: PromptRegistry | None = None) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()

    def render(self, context: TaskMemoContext) -> RenderedTaskClosureMemoPrompt:
        """Return rendered system/user prompts for a bounded memo context."""

        prompt_version = self._prompt_registry.get_latest_version(
            TASK_CLOSURE_MEMO_CONTRACTS.prompt_family
        )
        system_prompt = self._prompt_registry.get_template(
            TASK_CLOSURE_MEMO_CONTRACTS.system_prompt_id,
            version=prompt_version,
        )
        user_template = self._prompt_registry.get_template(
            TASK_CLOSURE_MEMO_CONTRACTS.user_prompt_id,
            version=prompt_version,
        )
        memo_context_json = render_memo_context_json(context)
        return RenderedTaskClosureMemoPrompt(
            system_prompt=system_prompt,
            user_prompt=user_template.format(memo_context_json=memo_context_json),
            metadata={
                GENERATION_METADATA_PROMPT_FAMILY_KEY: (
                    TASK_CLOSURE_MEMO_CONTRACTS.prompt_family
                ),
                GENERATION_METADATA_PROMPT_VERSION_KEY: prompt_version,
                GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY: list(
                    TASK_CLOSURE_MEMO_CONTRACTS.prompt_template_ids
                ),
            },
            memo_context_json=memo_context_json,
        )


def render_memo_context_json(context: TaskMemoContext) -> str:
    """Serialize memo context to deterministic bounded JSON."""

    payload = {
        "task": _bounded_jsonable(context.task),
        "source_watermark": _bounded_jsonable(context.source_watermark),
        "runtime_readiness": _bounded_jsonable(context.runtime_readiness),
        "memo_mode": context.memo_mode,
        "not_preparable_reason": context.not_preparable_reason,
        "transcript_context": _bounded_jsonable(context.transcript),
        "knowledge_packet": _bounded_jsonable(context.knowledge),
        "evidence_packet": _prompt_safe_evidence_packet(context.evidence),
        "previous_memo": _bounded_jsonable(context.previous_memo),
        "allowed_evidence_refs": sorted(context.allowed_evidence_refs),
        "allowed_knowledge_refs": sorted(context.allowed_knowledge_refs),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _prompt_safe_evidence_packet(value: Any) -> dict[str, Any]:
    items = []
    for item in getattr(value, "items", ()):
        items.append(
            {
                "ref": _bounded_jsonable(getattr(item, "ref", "")),
                "evidence_type": _bounded_jsonable(
                    getattr(item, "evidence_type", "")
                ),
                "tool_display_name": report_tool_display_name(
                    getattr(item, "source_tool", "")
                ),
                "target": _bounded_jsonable(getattr(item, "target", None)),
                "observed_at": _bounded_jsonable(getattr(item, "observed_at", None)),
                "created_at": _bounded_jsonable(getattr(item, "created_at", None)),
                "linked_asset_refs": _bounded_jsonable(
                    getattr(item, "linked_asset_refs", ())
                ),
                "linked_service_refs": _bounded_jsonable(
                    getattr(item, "linked_service_refs", ())
                ),
                "linked_finding_refs": _bounded_jsonable(
                    getattr(item, "linked_finding_refs", ())
                ),
            }
        )
    return {
        "task_id": int(getattr(value, "task_id", 0) or 0),
        "items": items,
        "item_count": int(getattr(value, "item_count", len(items)) or 0),
        "truncated": bool(getattr(value, "truncated", False)),
        "max_items": int(getattr(value, "max_items", len(items)) or 0),
    }


def _bounded_jsonable(value: Any, *, key: str | None = None) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _bounded_jsonable(asdict(value), key=key)

    if isinstance(value, Mapping):
        return {
            str(item_key): _bounded_jsonable(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }

    if isinstance(value, tuple | list):
        return [_bounded_jsonable(item, key=key) for item in value]

    if isinstance(value, frozenset | set):
        return sorted(_bounded_jsonable(item, key=key) for item in value)

    if isinstance(value, str):
        return _truncate_string(value, max_characters=_string_limit_for_key(key))

    return value


def _string_limit_for_key(key: str | None) -> int:
    if key is None:
        return _DEFAULT_MAX_STRING_CHARACTERS
    return _STRING_LIMITS_BY_KEY.get(key, _DEFAULT_MAX_STRING_CHARACTERS)


def _truncate_string(value: str, *, max_characters: int) -> str:
    if len(value) <= max_characters:
        return value
    return f"{value[:max_characters]}{_TRUNCATION_MARKER}"


__all__ = [
    "RenderedTaskClosureMemoPrompt",
    "TaskClosureMemoPromptRenderer",
    "render_memo_context_json",
]
