"""Deterministic renderer for working-memory scratchpad text.

This module converts canonical working-memory JSON into a compact,
human-readable ``trace.scratchpad`` string used for **diagnostics
only** (operator traces, telemetry, test observability). After the
Phase 4 narrowing, no prompt consumer treats ``trace.scratchpad`` as
authoritative memory; recent-turn continuity is owned by the shared
``ConversationContextBundle`` and its runtime-state projection.
"""

from __future__ import annotations

from typing import Any, Mapping

from .working_memory import normalize_working_memory

DEFAULT_RENDER_MAX_CHARS = 4800
ITEM_MAX_CHARS = 640

SENSITIVE_KEY_MARKERS = ("token", "password", "secret", "api_key", "authorization", "cookie", "bearer")
_TARGET_GAP_CODES = {"target_handle_required", "missing_target_handle", "unresolved_target_for_tool_run"}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _mask_sensitive_text(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in SENSITIVE_KEY_MARKERS):
        return "<REDACTED>"
    return text


def _safe_scalar(value: Any, *, max_chars: int = ITEM_MAX_CHARS) -> str:
    text = _truncate(str(value or ""), max_chars)
    return _mask_sensitive_text(text)


def _join_items(values: list[Any], *, max_items: int = 3) -> str:
    if not values:
        return "-"
    rendered = [_safe_scalar(v) for v in values[:max_items]]
    return ", ".join(rendered)


def _render_tool_summary(memory: Mapping[str, Any]) -> str:
    tool_runs = memory.get("tool_runs", [])
    if isinstance(tool_runs, list) and tool_runs:
        latest = tool_runs[-1] if isinstance(tool_runs[-1], Mapping) else {}
        tool_id = _safe_scalar(latest.get("tool_id", "unknown"))
        summary = _safe_scalar(latest.get("summary", ""))
        return f"{tool_id}: {summary}" if summary else tool_id
    return "none"


def _render_coverage_gaps(memory: Mapping[str, Any]) -> str:
    stage = str(memory.get("stage", "chat"))
    validation = memory.get("validation")
    if isinstance(validation, Mapping):
        missing = validation.get("missing")
        if isinstance(missing, list) and missing:
            gaps: list[str] = []
            for item in missing[:3]:
                if isinstance(item, Mapping):
                    raw_code = str(item.get("code", "required_input"))
                    if stage == "tool_selection" and raw_code in _TARGET_GAP_CODES:
                        continue
                    code = _safe_scalar(raw_code)
                    gaps.append(code)
            if gaps:
                return ", ".join(gaps)

    open_questions = memory.get("open_questions", [])
    if not isinstance(open_questions, list) or not open_questions:
        return "-"
    gaps: list[str] = []
    for item in open_questions[:3]:
        if isinstance(item, Mapping):
            raw_code = str(item.get("code", "question"))
            if stage == "tool_selection" and raw_code in _TARGET_GAP_CODES:
                continue
            code = _safe_scalar(raw_code)
            gaps.append(code)
        else:
            gaps.append(_safe_scalar(item))
    return ", ".join(gaps) if gaps else "-"


def _render_active_target(memory: Mapping[str, Any]) -> str:
    active = memory.get("active")
    if not isinstance(active, Mapping):
        return "-"

    target_id = active.get("target_id")
    if not isinstance(target_id, str) or not target_id.strip():
        return "-"

    referent_key = target_id.strip()
    if referent_key.startswith("target:"):
        referent_key = referent_key[len("target:") :]
    if not referent_key:
        return "-"

    referents = memory.get("referents")
    if isinstance(referents, Mapping):
        referent_payload = referents.get(referent_key)
        if isinstance(referent_payload, Mapping):
            referent_value = referent_payload.get("value", referent_payload.get("target"))
            if referent_value:
                return _safe_scalar(referent_value)
        elif referent_payload:
            return _safe_scalar(referent_payload)

    return _safe_scalar(referent_key)


def render_working_memory(memory: Mapping[str, Any] | None, *, max_chars: int = DEFAULT_RENDER_MAX_CHARS) -> str:
    """Render working memory into a deterministic bounded scratchpad string."""
    normalized = normalize_working_memory(memory)
    stage = _safe_scalar(normalized.get("stage", "chat"))
    raw_stage = str(normalized.get("stage", "chat"))
    objective = normalized.get("objective", {})
    objective_text = _safe_scalar(objective.get("text", "unknown")) if isinstance(objective, Mapping) else "unknown"
    constraints = normalized.get("constraints", {})
    preferences = normalized.get("preferences", {})

    constraint_scope = "-"
    constraint_boundaries = "-"
    if isinstance(constraints, Mapping):
        constraint_scope = _join_items(list(constraints.get("scope", []) if isinstance(constraints.get("scope"), list) else []))
        constraint_boundaries = _join_items(
            list(constraints.get("boundaries", []) if isinstance(constraints.get("boundaries"), list) else [])
        )

    prefs_line = "-"
    if isinstance(preferences, Mapping):
        prefs_line = ", ".join(
            [
                f"verbosity={_safe_scalar(preferences.get('verbosity', 'normal'))}",
                f"format={_safe_scalar(preferences.get('output_format', 'text'))}",
                f"language={_safe_scalar(preferences.get('language', 'en'))}",
            ]
        )

    open_questions_line = "-"
    validation = normalized.get("validation")
    if isinstance(validation, Mapping):
        missing = validation.get("missing")
        if isinstance(missing, list) and missing:
            rendered_missing: list[str] = []
            for item in missing[:3]:
                if isinstance(item, Mapping):
                    code = str(item.get("code", "")).strip()
                    if raw_stage == "tool_selection" and code in _TARGET_GAP_CODES:
                        continue
                    msg = item.get("message") or item.get("code") or "clarification required"
                    rendered_missing.append(_safe_scalar(msg))
            if rendered_missing:
                open_questions_line = "; ".join(rendered_missing)

    if open_questions_line == "-":
        open_questions = normalized.get("open_questions", [])
        if isinstance(open_questions, list) and open_questions:
            rendered_questions: list[str] = []
            for item in open_questions[:3]:
                if isinstance(item, Mapping):
                    code = str(item.get("code", "")).strip()
                    if raw_stage == "tool_selection" and code in _TARGET_GAP_CODES:
                        continue
                    msg = item.get("message") or item.get("code") or "clarification required"
                    rendered_questions.append(_safe_scalar(msg))
                else:
                    rendered_questions.append(_safe_scalar(item))
            if rendered_questions:
                open_questions_line = "; ".join(rendered_questions)

    lines = [
        f"stage: {stage}",
        f"objective: {objective_text}",
        f"active_target: {_render_active_target(normalized)}",
        f"constraints.scope: {constraint_scope}",
        f"constraints.boundaries: {constraint_boundaries}",
        f"preferences: {prefs_line}",
        f"last_tool_run: {_render_tool_summary(normalized)}",
        f"coverage_gaps: {_render_coverage_gaps(normalized)}",
        f"open_questions: {open_questions_line}",
    ]

    rendered = "\n".join(lines)
    return _truncate(rendered, max_chars)
