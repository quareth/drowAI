"""Single authority for mapping canonical working memory to bundle sections.

This module is the **only** place that derives
``RuntimeStateSnapshot`` fields and ``EvidenceRef`` entries from the
canonical working-memory payload produced by
``agent.graph.memory.working_memory``. The derivation is deterministic
and must remain side-effect free: no prompt assembly, no projection
logic, and no normalization of working memory beyond what this helper
needs to read already-normalized fields.

Purpose
-------
The hot-path ``ConversationContextBundle`` carries a runtime-state
snapshot that every prompt-authoritative projection consumes (see
``agent.graph.context.projections``). Without a refresh step, the
bundle built at turn start contains an empty snapshot for the entire
turn, because canonical working memory is reduced *after* the bundle
is seeded. This module closes that loop with a narrow, additive helper
that refreshes the bundle's ``runtime_state`` and ``evidence_refs``
slots in place whenever working memory is mutated.

Scope notes
-----------
- No projection logic lives here — projections own prompt-level
  filtering and ordering in ``agent.graph.context.projections``.
- No prompt serialization or token budgeting.
- No reducer logic — working memory remains owned by
  ``agent.graph.memory.memory_manager``; this helper only *reads* it.
- The module is intentionally small; its public surface is the three
  functions exported in ``__all__``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.contracts import EvidenceRef, RuntimeStateSnapshot
from agent.graph.memory.target_resolution import (
    coerce_target_candidate,
    coerce_target_value,
    resolve_target_from_working_memory,
)
from agent.graph.memory.working_memory import normalize_working_memory, update_active_handles


_DEFAULT_OBJECTIVE_TEXT = "unknown"
_EVIDENCE_REFS_MAX = 10
_EVIDENCE_SUMMARY_MAX_CHARS = 640


def _empty_runtime_state() -> RuntimeStateSnapshot:
    """Return an explicit empty runtime-state snapshot.

    Mirrors the shape produced by
    ``agent.graph.context.builder._empty_runtime_state`` so callers
    receive the same bundle contract when working memory is missing or
    has not yet been populated on the hot path.
    """
    return RuntimeStateSnapshot(
        active_target=None,
        current_goal=None,
        current_decision=None,
        in_flight_tool=None,
        handles={},
        active_todo=None,
    )


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` truncated to ``limit`` characters (no ellipsis)."""
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


def _resolve_active_target(wm: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve ``active.target_id`` against ``referents`` into a snapshot dict."""
    active = wm.get("active")
    if not isinstance(active, Mapping):
        return None
    target_id = active.get("target_id")
    if not target_id:
        return None

    referents = wm.get("referents")
    if not isinstance(referents, Mapping):
        return None

    # ``target_id`` is typed as ``target:<key>``; the referents map keys
    # are the raw ``<key>`` portion.
    typed = str(target_id)
    prefix = "target:"
    referent_key = typed[len(prefix):] if typed.startswith(prefix) else typed
    referent = referents.get(referent_key)
    if not isinstance(referent, Mapping):
        return None

    value = referent.get("value")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None

    return {
        "target_id": typed,
        "value": value,
        "kind": referent.get("kind"),
    }


def _resolve_current_goal(wm: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a normalized ``current_goal`` snapshot, or ``None``.

    The canonical working memory always has an ``objective`` record;
    its default text is ``"unknown"`` with status ``"unknown"``. Those
    defaults are treated as "no active goal" to keep the bundle slice
    useful to prompts.
    """
    objective = wm.get("objective")
    if not isinstance(objective, Mapping):
        return None

    text = objective.get("text")
    status = objective.get("status")
    if not isinstance(text, str) or not text.strip():
        return None
    if text.strip() == _DEFAULT_OBJECTIVE_TEXT and (
        not isinstance(status, str) or status.strip() == _DEFAULT_OBJECTIVE_TEXT
    ):
        return None

    return {
        "text": text,
        "status": str(status) if isinstance(status, str) and status else "",
    }


def _resolve_current_decision(wm: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the current ``active_decision`` dict, or ``None`` when absent."""
    decision = wm.get("active_decision")
    if isinstance(decision, Mapping):
        return dict(decision)
    return None


def _resolve_in_flight_tool(wm: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the in-flight tool descriptor, or ``None`` when idle."""
    tool_state = wm.get("tool_state")
    if not isinstance(tool_state, Mapping):
        return None
    status = tool_state.get("status")
    selected_tool = tool_state.get("selected_tool")
    if not isinstance(status, str) or status.strip() in ("", "none"):
        return None
    if not isinstance(selected_tool, str) or not selected_tool.strip():
        return None
    return {"selected_tool": selected_tool, "status": status}


_ACTIVE_HANDLE_FIELDS: tuple[str, ...] = ("target_id", "subject_id", "collection_id")


def _resolve_handles(wm: Mapping[str, Any]) -> dict[str, Any]:
    """Project the canonical ``active`` handle set onto the bundle snapshot.

    Working memory carries typed handles (``target_id``, ``subject_id``,
    ``collection_id``) under ``wm["active"]``. They are the deterministic
    IDs the planner / articulation projections advertise in their
    runtime-state slice, so they must live on the bundle alongside
    ``active_target`` / ``current_goal`` / ``current_decision``. Empty /
    falsy slots are dropped so the projection stays compact for the
    common no-handle case.
    """
    active = wm.get("active")
    if not isinstance(active, Mapping):
        return {}
    handles: dict[str, Any] = {}
    for field in _ACTIVE_HANDLE_FIELDS:
        value = active.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        handles[field] = value
    return handles


def runtime_state_snapshot_from_working_memory(
    working_memory: Mapping[str, Any] | None,
) -> RuntimeStateSnapshot:
    """Deterministically project canonical working memory to a runtime snapshot.

    Returns an explicit empty snapshot when ``working_memory`` is
    missing or not a mapping; callers can assume the returned value is
    always a fully-shaped ``RuntimeStateSnapshot``.
    """
    if not isinstance(working_memory, Mapping):
        return _empty_runtime_state()

    return RuntimeStateSnapshot(
        active_target=_resolve_active_target(working_memory),
        current_goal=_resolve_current_goal(working_memory),
        current_decision=_resolve_current_decision(working_memory),
        in_flight_tool=_resolve_in_flight_tool(working_memory),
        handles=_resolve_handles(working_memory),
        active_todo=None,
    )


def evidence_refs_from_working_memory(
    working_memory: Mapping[str, Any] | None,
) -> list[EvidenceRef]:
    """Derive evidence refs from the tail of ``wm["tool_runs"]``.

    Returns ``[]`` when working memory is missing or carries no tool
    runs. Summaries are truncated to a short bound to keep the bundle
    prompt-safe; payloads stay in their canonical stores.
    """
    if not isinstance(working_memory, Mapping):
        return []
    tool_runs = working_memory.get("tool_runs")
    if not isinstance(tool_runs, list) or not tool_runs:
        return []

    refs: list[EvidenceRef] = []
    for entry in tool_runs[-_EVIDENCE_REFS_MAX:]:
        if not isinstance(entry, Mapping):
            continue
        evidence_id = entry.get("id") or entry.get("execution_id") or ""
        summary = entry.get("summary") or ""
        tool_id = entry.get("tool_id") or ""
        refs.append(
            EvidenceRef(
                evidence_id=str(evidence_id),
                kind="tool_run",
                summary=_truncate(str(summary), _EVIDENCE_SUMMARY_MAX_CHARS),
                source=str(tool_id),
            )
        )
    return refs


def refresh_bundle_from_working_memory(
    metadata: MutableMapping[str, Any],
) -> None:
    """Refresh the bundle's runtime-state and evidence refs from working memory.

    Reads ``metadata["working_memory"]`` and
    ``metadata[METADATA_CONTEXT_BUNDLE_KEY]`` and mutates the bundle
    in place when both are present. Does nothing when the bundle is
    absent — the Phase 5 hard-fail on missing bundle is enforced at
    prompt consumers, not here; this helper is non-authoritative
    plumbing that keeps already-wired bundles in sync with canonical
    working memory.

    The ``active_todo`` slot is deliberately NOT refreshed here —
    todos live on ``facts.todo_list`` (not working memory), so they
    are refreshed by :func:`refresh_bundle_active_todo` at the nodes
    that mutate todo progression.
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, MutableMapping):
        return

    wm = metadata.get("working_memory")
    previous_runtime_state = bundle.get("runtime_state") or {}
    previous_active_todo = (
        previous_runtime_state.get("active_todo")
        if isinstance(previous_runtime_state, Mapping)
        else None
    )

    runtime_state = runtime_state_snapshot_from_working_memory(wm)
    runtime_state["active_todo"] = previous_active_todo
    bundle["runtime_state"] = runtime_state
    bundle["evidence_refs"] = evidence_refs_from_working_memory(wm)


def refresh_bundle_active_todo(
    metadata: MutableMapping[str, Any],
    todo_list: Any,
) -> None:
    """Refresh the bundle's ``active_todo`` slot from a todo list.

    Called at the narrow set of nodes that mutate todo progression
    (planner bootstrap, post-tool reasoning progress application,
    reflection replanning) so the context bundle stays in sync with
    the single IN_PROGRESS item. The descriptor is resolved by
    :func:`agent.graph.utils.plan_progress_authority.resolve_active_todo`
    and is either a compact ``{"index", "description"}`` dict or
    ``None`` when no todo is actionable.

    Does nothing when the bundle is absent (same non-authoritative
    contract as :func:`refresh_bundle_from_working_memory`).
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, MutableMapping):
        return

    # Local import to avoid a cycle: plan_progress_authority ->
    # state.TodoItem, but context.runtime_state is imported by many
    # graph nodes that also touch plan_progress_authority.
    from agent.graph.utils.plan_progress_authority import resolve_active_todo

    active_todo = resolve_active_todo(todo_list)

    runtime_state = bundle.get("runtime_state")
    if not isinstance(runtime_state, MutableMapping):
        runtime_state = runtime_state_snapshot_from_working_memory(
            metadata.get("working_memory")
        )
        bundle["runtime_state"] = runtime_state
    runtime_state["active_todo"] = active_todo


def _iter_target_candidate_sources(
    *,
    todo_list: Any,
    plan: Any,
    current_goal: Any,
) -> list[str]:
    """Build candidate text sources ordered by plan/todo authority."""
    sources: list[str] = []

    if isinstance(todo_list, Iterable) and not isinstance(todo_list, (str, bytes)):
        for todo in todo_list:
            status_raw: Any = None
            description: str = ""
            if isinstance(todo, Mapping):
                status_raw = todo.get("status")
                description = str(todo.get("description") or todo.get("text") or "").strip()
            else:
                status_raw = getattr(todo, "status", None)
                description = str(getattr(todo, "description", "")).strip()

            status_text = str(getattr(status_raw, "value", status_raw or "")).strip().lower()
            if status_text == "in_progress" and description:
                sources.append(description)
                break

    if isinstance(plan, Iterable) and not isinstance(plan, (str, bytes)):
        plan_added = 0
        for step in plan:
            step_text = str(step).strip()
            if step_text:
                sources.append(step_text)
                plan_added += 1
            if plan_added >= 2:
                break

    goal_text = str(current_goal or "").strip()
    if goal_text:
        sources.append(goal_text)
    return sources


def sync_target_hint_from_plan_todo(
    metadata: MutableMapping[str, Any],
    *,
    todo_list: Any,
    plan: Any,
    current_goal: Any,
) -> bool:
    """Synchronize working-memory intent target from plan/todo/current-goal hints."""
    raw_memory = metadata.get("working_memory")
    if not isinstance(raw_memory, Mapping):
        return False

    memory = normalize_working_memory(raw_memory)
    candidate: dict[str, Any] | None = None
    for source in _iter_target_candidate_sources(
        todo_list=todo_list,
        plan=plan,
        current_goal=current_goal,
    ):
        candidate = coerce_target_candidate(source, allow_single_label=True)
        if candidate:
            break
    if not candidate:
        return False

    candidate_value = coerce_target_value(candidate, allow_single_label=True)
    if not candidate_value:
        return False

    current_value = resolve_target_from_working_memory(memory)
    if isinstance(current_value, str) and current_value.strip() == candidate_value:
        return False

    referents = dict(memory.get("referents", {}))
    referents["intent:target"] = {
        "value": candidate_value,
        "kind": candidate.get("kind"),
        "confidence": candidate.get("confidence"),
        "source": "plan_todo_sync",
    }
    memory["referents"] = referents
    metadata["working_memory"] = update_active_handles(memory, target_id="intent:target")
    refresh_bundle_from_working_memory(metadata)
    return True


__all__ = [
    "evidence_refs_from_working_memory",
    "refresh_bundle_active_todo",
    "refresh_bundle_from_working_memory",
    "runtime_state_snapshot_from_working_memory",
    "sync_target_hint_from_plan_todo",
]
