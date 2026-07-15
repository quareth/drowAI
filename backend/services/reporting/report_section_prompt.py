"""Render bounded section-generation prompts for engagement reports."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any

from core.prompts.registry import PromptRegistry

from backend.services.reporting.contracts import (
    ENGAGEMENT_REPORT_GENERATION_CONTRACTS,
    GENERATION_METADATA_PROMPT_FAMILY_KEY,
    GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY,
    GENERATION_METADATA_PROMPT_VERSION_KEY,
    validate_report_type,
)
from backend.services.reporting.report_context_builder import (
    ReportCandidateFindingPolicy,
    ReportContext,
)
from backend.services.reporting.report_section_plan import ReportSectionPlanItem
from backend.services.reporting.report_tool_display import report_tool_display_name

_SYSTEM_PROMPT_ID = "engagement_report_section_system"
_USER_PROMPT_ID = "engagement_report_section_user"
_PROMPT_TEMPLATE_IDS = (_SYSTEM_PROMPT_ID, _USER_PROMPT_ID)
_CONTEXT_SCHEMA_VERSION = "engagement_report_section_prompt_context.v1"
_REPAIR_CONTEXT_SCHEMA_VERSION = "engagement_report_section_repair_context.v1"
_DEFAULT_MAX_STRING_CHARACTERS = 2_000
_DEFAULT_MAX_SEQUENCE_ITEMS = 100
_MAX_SOURCE_REF_ITEMS = 500
_TRUNCATION_MARKER = "...[truncated]"
_STRING_LIMITS_BY_KEY = MappingProxyType(
    {
        "description": 2_000,
        "excerpt": 1_500,
        "prompt_purpose": 1_000,
        "scope": 2_000,
        "summary": 2_000,
        "target": 1_000,
        "title": 1_000,
    }
)


@dataclass(frozen=True, slots=True)
class RenderedReportSectionPrompt:
    """Prompt messages and safe prompt metadata for one report section."""

    system_prompt: str
    user_prompt: str
    metadata: Mapping[str, Any]
    report_context_json: str
    section_plan_json: str


class ReportSectionPromptRenderer:
    """Build one section prompt without touching LLM runtime clients."""

    def __init__(self, prompt_registry: PromptRegistry | None = None) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()

    def render(
        self,
        *,
        context: ReportContext,
        section_plan_item: ReportSectionPlanItem | Mapping[str, Any],
        report_type: str,
        candidate_policy: ReportCandidateFindingPolicy | None,
        section_schema_name: str,
        section_schema_version: str,
    ) -> RenderedReportSectionPrompt:
        """Return rendered prompts for one fixed section plan item."""

        validated_report_type = validate_report_type(report_type)
        if context.report_type != validated_report_type:
            raise ValueError("report type does not match report context")

        effective_candidate_policy = candidate_policy or context.candidate_policy
        if (
            effective_candidate_policy.include_candidate_findings
            != context.candidate_policy.include_candidate_findings
        ):
            raise ValueError("candidate policy does not match report context")

        schema_name = str(section_schema_name).strip()
        schema_version = str(section_schema_version).strip()
        if not schema_name or not schema_version:
            raise ValueError("section schema name and version are required")

        section_plan = _section_plan_payload(section_plan_item)
        section_id = str(section_plan.get("section_id") or "").strip()
        if not section_id:
            raise ValueError("section plan item must include a section_id")

        prompt_family = ENGAGEMENT_REPORT_GENERATION_CONTRACTS.section_prompt_family
        prompt_version = self._prompt_registry.get_latest_version(prompt_family)
        system_prompt = self._prompt_registry.get_template(
            _SYSTEM_PROMPT_ID,
            version=prompt_version,
        )
        user_template = self._prompt_registry.get_template(
            _USER_PROMPT_ID,
            version=prompt_version,
        )
        report_context_json = render_report_section_context_json(
            context=context,
            candidate_policy=effective_candidate_policy,
            section_schema_name=schema_name,
            section_schema_version=schema_version,
        )
        section_plan_json = render_section_plan_json(section_plan)
        return RenderedReportSectionPrompt(
            system_prompt=system_prompt,
            user_prompt=user_template.format(
                report_context_json=report_context_json,
                section_plan_json=section_plan_json,
            ),
            metadata=MappingProxyType(
                {
                    GENERATION_METADATA_PROMPT_FAMILY_KEY: prompt_family,
                    GENERATION_METADATA_PROMPT_VERSION_KEY: prompt_version,
                    GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY: list(
                        _PROMPT_TEMPLATE_IDS
                    ),
                    "section_id": section_id,
                    "report_type": validated_report_type,
                    "section_schema_name": schema_name,
                    "section_schema_version": schema_version,
                }
            ),
            report_context_json=report_context_json,
            section_plan_json=section_plan_json,
        )


def render_report_section_repair_prompt(
    *,
    rendered_prompt: RenderedReportSectionPrompt,
    failed_section_payload: Mapping[str, Any],
    validation_issues: Sequence[Mapping[str, Any]],
) -> RenderedReportSectionPrompt:
    """Return a bounded section-only repair prompt for validation failures."""

    repair_context_json = _json_dumps(
        {
            "context_schema_version": _REPAIR_CONTEXT_SCHEMA_VERSION,
            "failed_section_payload": _bounded_jsonable(failed_section_payload),
            "validation_issues": _bounded_jsonable(validation_issues),
        }
    )
    return RenderedReportSectionPrompt(
        system_prompt=rendered_prompt.system_prompt,
        user_prompt=(
            f"{rendered_prompt.user_prompt}\n\n"
            "Repair the generated section JSON below. Return exactly one complete "
            "section object for the same section_id and schema_version. Fix only the "
            "listed validation issues. Do not add unsupported facts, raw prompt text, "
            "secrets, or customer-internal reference identifiers to prose.\n\n"
            f"REPAIR_CONTEXT_JSON:\n{repair_context_json}"
        ),
        metadata=MappingProxyType(
            {
                **dict(rendered_prompt.metadata),
                "repair_attempt": True,
                "repair_context_schema_version": _REPAIR_CONTEXT_SCHEMA_VERSION,
            }
        ),
        report_context_json=rendered_prompt.report_context_json,
        section_plan_json=rendered_prompt.section_plan_json,
    )


def render_report_section_context_json(
    *,
    context: ReportContext,
    candidate_policy: ReportCandidateFindingPolicy | None,
    section_schema_name: str,
    section_schema_version: str,
) -> str:
    """Serialize report context to deterministic bounded JSON for prompting."""

    effective_candidate_policy = candidate_policy or context.candidate_policy
    payload = {
        "context_schema_version": _CONTEXT_SCHEMA_VERSION,
        "report_type": context.report_type,
        "engagement": _bounded_jsonable(context.engagement),
        "selected_tasks": _sorted_bounded_sequence(
            context.selected_tasks,
            key=lambda item: (item.task_id, item.memo_id),
        ),
        "selected_memos": _sorted_bounded_sequence(
            context.selected_memos,
            key=lambda item: (item.task_id, item.version, item.memo_id),
        ),
        "memo_partitions": _bounded_jsonable(context.memo_partitions),
        "compatible_knowledge_refs": _sorted_bounded_sequence(
            context.compatible_knowledge_refs,
            key=lambda item: (item.task_id, item.ref),
            max_items=_MAX_SOURCE_REF_ITEMS,
        ),
        "compatible_evidence_refs": _sorted_bounded_sequence(
            _prompt_safe_evidence_refs(context.compatible_evidence_refs),
            key=lambda item: (item["task_id"], item["ref"]),
            max_items=_MAX_SOURCE_REF_ITEMS,
        ),
        "include_candidate_findings": (
            effective_candidate_policy.include_candidate_findings
        ),
        "source_watermark": {
            "schema_version": context.source_watermark.schema_version,
            "report_type": context.source_watermark.report_type,
            "selected_memos": _sorted_bounded_sequence(
                context.source_watermark.selected_memos,
                key=lambda item: (item.task_id, item.version, item.memo_id),
            ),
            "hash_algorithm": context.source_watermark.hash_algorithm,
            "hash": context.source_watermark.hash,
            "generation_metadata": _bounded_jsonable(
                context.source_watermark.generation_metadata
            ),
        },
        "allowed_task_memo_ids": sorted(context.allowed_task_memo_ids),
        "allowed_knowledge_refs": sorted(context.allowed_knowledge_refs)[
            :_MAX_SOURCE_REF_ITEMS
        ],
        "allowed_evidence_refs": sorted(context.allowed_evidence_refs)[
            :_MAX_SOURCE_REF_ITEMS
        ],
        "context_truncated": context.truncated,
        "section_schema": {
            "name": str(section_schema_name).strip(),
            "version": str(section_schema_version).strip(),
        },
    }
    return _json_dumps(payload)


def render_section_plan_json(
    section_plan_item: ReportSectionPlanItem | Mapping[str, Any],
) -> str:
    """Serialize one fixed section plan item to deterministic bounded JSON."""

    return _json_dumps(_section_plan_payload(section_plan_item))


def _prompt_safe_evidence_refs(value: Sequence[Any]) -> tuple[Mapping[str, Any], ...]:
    refs: list[Mapping[str, Any]] = []
    for item in value:
        refs.append(
            {
                "ref": str(getattr(item, "ref", "")),
                "task_id": int(getattr(item, "task_id", 0) or 0),
                "evidence_type": str(getattr(item, "evidence_type", "")),
                "tool_display_name": report_tool_display_name(
                    getattr(item, "source_tool", "")
                ),
                "target": getattr(item, "target", None),
                "observed_at": getattr(item, "observed_at", None),
                "created_at": getattr(item, "created_at", None),
                "linked_knowledge_refs": tuple(
                    str(ref)
                    for ref in getattr(item, "linked_knowledge_refs", ())
                    if str(ref).strip()
                ),
            }
        )
    return tuple(refs)


def _section_plan_payload(
    section_plan_item: ReportSectionPlanItem | Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(section_plan_item, ReportSectionPlanItem):
        return dict(section_plan_item.as_llm_input())
    if isinstance(section_plan_item, Mapping):
        return {
            str(key): _bounded_jsonable(value, key=str(key))
            for key, value in sorted(
                section_plan_item.items(),
                key=lambda item: str(item[0]),
            )
        }
    raise TypeError("section_plan_item must be a report section plan item or mapping")


def _sorted_bounded_sequence(
    value: Sequence[Any],
    *,
    key: Any,
    max_items: int = _DEFAULT_MAX_SEQUENCE_ITEMS,
) -> list[Any]:
    return [
        _bounded_jsonable(item)
        for item in sorted(value, key=key)[:max_items]
    ]


def _bounded_jsonable(value: Any, *, key: str | None = None) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _bounded_jsonable(getattr(value, field.name), key=field.name)
            for field in fields(value)
        }

    if isinstance(value, Mapping):
        return {
            str(item_key): _bounded_jsonable(item_value, key=str(item_key))
            for item_key, item_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
        }

    if isinstance(value, frozenset | set):
        return [_bounded_jsonable(item, key=key) for item in sorted(value)]

    if isinstance(value, tuple | list):
        return [
            _bounded_jsonable(item, key=key)
            for item in value[:_DEFAULT_MAX_SEQUENCE_ITEMS]
        ]

    if isinstance(value, str):
        return _truncate_string(value, max_characters=_string_limit_for_key(key))

    return value


def _string_limit_for_key(key: str | None) -> int:
    if key is None:
        return _DEFAULT_MAX_STRING_CHARACTERS
    return int(_STRING_LIMITS_BY_KEY.get(key, _DEFAULT_MAX_STRING_CHARACTERS))


def _truncate_string(value: str, *, max_characters: int) -> str:
    if len(value) <= max_characters:
        return value
    return f"{value[:max_characters]}{_TRUNCATION_MARKER}"


def _json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "RenderedReportSectionPrompt",
    "ReportSectionPromptRenderer",
    "render_report_section_context_json",
    "render_report_section_repair_prompt",
    "render_section_plan_json",
]
