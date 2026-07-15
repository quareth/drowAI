"""Prompt builders and templates for simple tool execution.

This module loads templates from `core/prompts/versions/simple_tool/`
via the shared `TemplateLoader`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from core.prompts.base import ChatPromptBuilder
from core.prompts.loader import TemplateLoader
from core.prompts.route_labels import llm_facing_route_label


_VERSIONS_ROOT = Path(__file__).resolve().parents[1] / "versions"
_LOADER = TemplateLoader(_VERSIONS_ROOT)


def _stringify_collection(values: Iterable[Any]) -> str:
    items = [str(value) for value in values if value not in (None, "")]
    return ", ".join(items) if items else "none"


def _render_latest(filename: str, **context: Any) -> str:
    template = _LOADER.load_latest_version("simple_tool", filename)
    safe_context = {key: "" if value is None else str(value) for key, value in context.items()}
    return template.format_map(safe_context)


def _extract_catalog_lines(facts: Mapping[str, Any]) -> str:
    metadata = facts.get("metadata") or {}
    catalog = (metadata.get("tool_catalog") or {}).get("entries") or []
    lines = []
    for entry in catalog:
        tool_id = entry.get("tool_id") or entry.get("id") or ""
        name = entry.get("name") or tool_id
        description = entry.get("description") or ""
        lines.append(f"- {name} ({tool_id}): {description}")
    return "\n".join(lines) if lines else "- No catalog entries provided."


def _build_prompt_context(state: Mapping[str, object]) -> Dict[str, Any]:
    facts = state.get("facts", {}) or {}
    intent_hints = facts.get("intent_hints") or {}

    catalog_lines = _extract_catalog_lines(facts)
    tool_candidates = facts.get("tool_candidates") or facts.get("tool_ids") or []

    return {
        "user_message": facts.get("message", ""),
        "capability": llm_facing_route_label(facts.get("capability") or "simple_tool_execution"),
        "tool_hints": _stringify_collection(intent_hints.get("tool_hints") or []),
        "targets": _stringify_collection(intent_hints.get("targets") or []),
        "eligible_routes": _stringify_collection(
            [llm_facing_route_label(route) for route in (facts.get("eligible_routes") or [])]
        ),
        "catalog_lines": catalog_lines,
        "tool_candidates": _stringify_collection(tool_candidates),
        "selected_tool": facts.get("selected_tool") or "",
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Iterable):
        rendered = []
        for entry in value:
            if entry in (None, ""):
                continue
            rendered.append(str(entry))
        return rendered
    return []


class SimpleToolPromptBuilder(ChatPromptBuilder):
    """Prompt builder leveraging template files for tool execution flows."""

    def build_system_prompt(self, state: Mapping[str, object]) -> str:
        context = _build_prompt_context(state)
        return _render_latest("system.txt", **context)

    def build_decision_prompt(self, state: Mapping[str, object]) -> str:
        context = _build_prompt_context(state)
        return _render_latest("selection.txt", **context)

    def build_tool_summary_prompt(self, tool_result: Mapping[str, object]) -> str:
        compact_result = _as_mapping(tool_result.get("compact_tool_result"))
        if not compact_result and any(
            key in tool_result for key in ("summary", "key_findings", "errors", "report_recommendations")
        ):
            compact_result = _as_mapping(tool_result)

        observation = (
            compact_result.get("summary")
            or tool_result.get("summary")
            or tool_result.get("observation")
            or ""
        )
        errors = _as_string_list(compact_result.get("errors") or tool_result.get("errors"))
        status = compact_result.get("status") or tool_result.get("status") or tool_result.get("status_text") or ""

        if errors:
            stderr_line = f"stderr: {' | '.join(errors)}"
        else:
            stderr_line = "stderr: none"

        context = {
            "observation": observation or "No observation produced.",
            "stderr_line": stderr_line,
            "status": status,
        }
        return _render_latest("summary.txt", **context)


def build_simple_tool_prompt(state: Mapping[str, object]) -> str:
    """Convenience helper returning the system prompt template output."""

    return SimpleToolPromptBuilder().build_system_prompt(state)


__all__ = ["SimpleToolPromptBuilder", "build_simple_tool_prompt"]
