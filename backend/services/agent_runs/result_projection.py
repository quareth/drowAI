"""Best-effort same-process subagent result projection.

This module projects completed process-local subagent results into the next main
LangGraph turn context. It intentionally owns no persistence, lease, recovery,
or exactly-once delivery guarantee; the process-local registry remains the sole
runtime source for live same-process handoff.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY

from .contracts import AgentResultProjection
from .registry import ProcessLocalAgentRunRegistry


logger = logging.getLogger(__name__)

COMPLETED_AGENT_RESULTS_KEY = "completed_agent_results"

DEFAULT_MAX_RESULTS = 3
DEFAULT_MAX_LIST_ITEMS = 8
DEFAULT_MAX_TEXT_CHARS = 800
DEFAULT_MAX_EVIDENCE_REFS = 8
DEFAULT_MAX_EVIDENCE_VALUE_CHARS = 300

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)=\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)


@dataclass(frozen=True, slots=True)
class CompletedAgentResultHandoff:
    """Projected result payload plus registry IDs to mark consumed after use."""

    results: tuple[dict[str, Any], ...]
    agent_run_ids: tuple[str, ...]


class AgentRunResultProjector:
    """Build bounded main-context projections from the process-local registry."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_evidence_refs: int = DEFAULT_MAX_EVIDENCE_REFS,
        max_evidence_value_chars: int = DEFAULT_MAX_EVIDENCE_VALUE_CHARS,
    ) -> None:
        self._registry = registry
        self._max_results = max(0, max_results)
        self._max_list_items = max(0, max_list_items)
        self._max_text_chars = max(0, max_text_chars)
        self._max_evidence_refs = max(0, max_evidence_refs)
        self._max_evidence_value_chars = max(0, max_evidence_value_chars)

    async def collect_for_context(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str,
    ) -> CompletedAgentResultHandoff:
        """Return completed, unconsumed subagent results without mutating state."""
        if self._max_results <= 0 or not conversation_id:
            return CompletedAgentResultHandoff(results=(), agent_run_ids=())

        entries = await self._registry.list_task_runs(
            tenant_id=tenant_id,
            task_id=task_id,
        )
        candidates = sorted(
            (
                entry
                for entry in entries
                if entry.conversation_id == conversation_id
                and entry.status == "completed"
                and entry.result is not None
                and not entry.result_consumed
            ),
            key=lambda entry: (
                _datetime_sort_key(entry.completed_at),
                _datetime_sort_key(entry.created_at),
                entry.agent_run_id,
            ),
        )

        results: list[dict[str, Any]] = []
        agent_run_ids: list[str] = []
        for entry in candidates[: self._max_results]:
            assert entry.result is not None
            results.append(self._bounded_projection(entry.result))
            agent_run_ids.append(entry.agent_run_id)

        return CompletedAgentResultHandoff(
            results=tuple(results),
            agent_run_ids=tuple(agent_run_ids),
        )

    async def mark_consumed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        handoff: CompletedAgentResultHandoff,
    ) -> None:
        """Mark projected result IDs consumed after a main turn accepts them."""
        for agent_run_id in handoff.agent_run_ids:
            await self._registry.consume_result(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )

    def project_result(self, result: Any) -> dict[str, Any]:
        """Return the same bounded projection used by next-turn collection."""
        return self._bounded_projection(result)

    def _bounded_projection(self, result: Any) -> dict[str, Any]:
        projection = AgentResultProjection.from_result(result).model_dump(mode="json")
        return {
            "agent_run_id": _bounded_text(
                projection["agent_run_id"], max_chars=self._max_evidence_value_chars
            ),
            "agent_id": projection["agent_id"],
            "agent_kind": projection["agent_kind"],
            "agent_display_name": projection["agent_display_name"],
            "outcome": projection["outcome"],
            "summary": _bounded_text(
                projection["summary"], max_chars=self._max_text_chars
            ),
            "key_findings": _bounded_string_list(
                projection.get("key_findings"),
                max_items=self._max_list_items,
                max_chars=self._max_text_chars,
            ),
            "evidence_refs": _bounded_evidence_refs(
                projection.get("evidence_refs"),
                max_items=self._max_evidence_refs,
                max_chars=self._max_evidence_value_chars,
            ),
            "tools_used": _bounded_string_list(
                projection.get("tools_used"),
                max_items=self._max_list_items,
                max_chars=self._max_evidence_value_chars,
            ),
            "limitations": _bounded_string_list(
                projection.get("limitations"),
                max_items=self._max_list_items,
                max_chars=self._max_text_chars,
            ),
            "recommended_next_steps": _bounded_string_list(
                projection.get("recommended_next_steps"),
                max_items=self._max_list_items,
                max_chars=self._max_text_chars,
            ),
            "final_checkpoint_id": _optional_bounded_text(
                projection.get("final_checkpoint_id"),
                max_chars=self._max_evidence_value_chars,
            ),
        }


def attach_completed_agent_results_to_context(
    metadata: dict[str, Any],
    handoff: CompletedAgentResultHandoff,
) -> None:
    """Attach projected results to metadata and the hot-path context bundle."""
    if not handoff.results:
        return
    results = [dict(item) for item in handoff.results]
    metadata[COMPLETED_AGENT_RESULTS_KEY] = results

    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if isinstance(bundle, dict):
        bundle[COMPLETED_AGENT_RESULTS_KEY] = [dict(item) for item in results]


def _bounded_string_list(
    value: Any,
    *,
    max_items: int,
    max_chars: int,
) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [
        _bounded_text(item, max_chars=max_chars)
        for item in value[:max_items]
        if str(item).strip()
    ]


def _bounded_evidence_refs(
    value: Any,
    *,
    max_items: int,
    max_chars: int,
) -> list[dict[str, str]]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[dict[str, str]] = []
    for item in value[:max_items]:
        if not isinstance(item, Mapping):
            continue
        normalized: dict[str, str] = {}
        for key, raw in item.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            normalized[normalized_key] = _bounded_text(raw, max_chars=max_chars)
        if normalized:
            refs.append(normalized)
    return refs


def _optional_bounded_text(value: Any, *, max_chars: int) -> str | None:
    if value in (None, ""):
        return None
    text = _bounded_text(value, max_chars=max_chars)
    return text or None


def _bounded_text(value: Any, *, max_chars: int) -> str:
    text = str(value).strip()
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("<REDACTED>", text)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3].rstrip() + "..."


def _datetime_sort_key(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


__all__ = [
    "COMPLETED_AGENT_RESULTS_KEY",
    "AgentRunResultProjector",
    "CompletedAgentResultHandoff",
    "attach_completed_agent_results_to_context",
]
