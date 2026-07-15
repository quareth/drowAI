"""Planner lifecycle and context helpers for tool-execution runtime."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from agent.config import AgentConfig
from agent.models import Action, ActionType, ExecutionStrategy
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from agent.reasoning.tool_selection_sentinel import (
    UNAVAILABLE_CAPABILITY_METADATA_KEY,
    UNAVAILABLE_CAPABILITY_TOOL,
    plan_is_unavailable_capability,
)
from agent.tool_runtime import ToolExecutionRequest
from agent.tool_runtime.batch.types import ToolBatch as _ToolBatch
from agent.tool_runtime.batch.plan_view import (
    primary_tool_call_from_metadata,
    serialized_tool_calls_from_plan,
)
from agent.tool_runtime.artifact_file_metadata import (
    collect_artifact_file_ref_candidates,
)


def _serialize_tool_batch(batch: _ToolBatch) -> Dict[str, Any]:
    """Serialize a ToolBatch into the planner_plan dict shape.

    The orchestrator (Phase 5 Task 5.4) reconstructs the batch via
    ``_deserialize_tool_batch_from_plan_data`` to feed BatchValidator and
    the lifecycle emitters. Pure data — no execution semantics.
    """
    return {
        "tool_batch_id": batch.tool_batch_id,
        "requested_execution_strategy": batch.requested_execution_strategy.value,
        "deferred_followups": list(batch.deferred_followups),
        "selection_rationale": batch.selection_rationale,
        "tool_calls": [
            {
                "tool_call_id": call.tool_call_id,
                "tool_id": call.tool_id,
                "parameters": dict(call.parameters),
                "intent": call.intent,
            }
            for call in batch.tool_calls
        ],
    }
from agent.tool_runtime.artifact_tool_policy import iter_non_artifact_tools  # noqa: E402
from backend.services.metrics.utils import safe_inc  # noqa: E402

from ...memory.findings import (  # noqa: E402
    count_known_open_port_findings,
    select_relevant_findings_for_prompt,
)
from ...context.builder import METADATA_CONTEXT_BUNDLE_KEY  # noqa: E402
from ...context.projections import project_for_planner  # noqa: E402
from ...context.serialization import (  # noqa: E402
    SECTION_REFERENCED_PRIOR_TURNS,
    SECTION_RUNTIME_STATE,
    serialize_projection_to_section_map,
)
from ...memory.target_resolution import (  # noqa: E402
    coerce_target_value,
    resolve_planner_target,
)
from ...state import InteractiveState  # noqa: E402
from ...utils import iteration_memory as _iteration_memory  # noqa: E402
from ...utils.cache_invalidation import create_plan_context, invalidate_plan, should_invalidate_plan  # noqa: E402
from ...utils.history_formatter import sanitize_history_content  # noqa: E402

_CURRENT_TURN_RUNTIME_CONTROLS_KEY = "current_turn_runtime_controls"


# NOTE: ``resolve_planner_target`` previously had a local definition here that
# duplicated (and diverged from) the canonical implementation in
# ``agent/graph/memory/target_resolution.py``. The local version included
# scrappy fallbacks (history scan, raw user_message) that the canonical
# version intentionally omits in favor of the structured continuity gate
# documented in
# ``docs/plans/memory-consolidation-phase2-implementation-guide.md`` (the
# planner doc explicitly cites ``target_resolution.py:592`` as the resolver
# of record). The duplicate was reintroduced by the worktree merge
# ``e137bd6f``; we now route back to the canonical implementation.


def _metadata_from_facts(facts: Any) -> Mapping[str, Any]:
    """Return facts metadata, tolerating test doubles without safe_metadata."""
    safe_metadata = getattr(facts, "safe_metadata", None)
    if isinstance(safe_metadata, Mapping):
        return safe_metadata
    metadata = getattr(facts, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def build_action_for_planner(
    interactive: InteractiveState,
    request: ToolExecutionRequest,
) -> Action:
    """Build Action for planner with LLM-centric tool selection."""
    facts = interactive.facts
    metadata = _metadata_from_facts(facts)
    action_type = ActionType.GATHER_INFO
    reasoning = metadata.get("planner_reasoning") or ""
    expected_outcome = metadata.get("expected_outcome") or ""

    tool_intent = metadata.get("tool_intent") or {}
    resolved_target = resolve_planner_target(
        user_message=str(facts.message or request.message or ""),
        request_targets=list(request.targets or []),
        metadata=metadata,
        history=list(request.history or []),
        tool_intent=tool_intent if isinstance(tool_intent, Mapping) else {},
    )
    return Action(
        type=action_type,
        target=resolved_target,
        parameters={},
        reasoning=str(reasoning or ""),
        expected_outcome=str(expected_outcome or ""),
    )


def build_working_memory_context_for_planner(
    metadata: Mapping[str, Any],
    *,
    max_summary_chars: int,
) -> Dict[str, Any]:
    """Build planner runtime-state context from the shared bundle projection.

    Phase-5 authority cutover: planner prompt continuity/runtime context must
    come from ``metadata[context_bundle]`` via ``project_for_planner``.
    ``metadata["working_memory"]`` remains canonical reducer state, but this
    helper is no longer allowed to read it as a second prompt authority.
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, Mapping):
        return {
            "working_memory": {},
            "working_memory_summary": "",
            "referenced_prior_turns": "",
        }

    planner_projection = project_for_planner(bundle)
    runtime_state = planner_projection.get("runtime_state")
    compact_working_memory: Dict[str, Any] = (
        dict(runtime_state) if isinstance(runtime_state, Mapping) else {}
    )
    compact_working_memory = {
        key: value
        for key, value in compact_working_memory.items()
        if value not in (None, "", [], {})
    }

    section_map = serialize_projection_to_section_map(planner_projection)
    runtime_state_summary = section_map.get(SECTION_RUNTIME_STATE, "")
    referenced_prior_turns = section_map.get(SECTION_REFERENCED_PRIOR_TURNS, "")
    working_memory_summary = str(runtime_state_summary or "")[:max_summary_chars]
    return {
        "working_memory": compact_working_memory,
        "working_memory_summary": working_memory_summary,
        "referenced_prior_turns": str(referenced_prior_turns or "")[:max_summary_chars],
    }


def _current_turn_phase_records(metadata: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return existing current-turn phase records for planner projection."""
    turn_sequence = metadata.get("turn_sequence")
    requested_turn = turn_sequence if isinstance(turn_sequence, int) else None
    records: List[Dict[str, Any]] = []
    for record in _iteration_memory.get_ledger(dict(metadata)):
        if requested_turn is not None and record.get("turn_sequence") != requested_turn:
            continue
        records.append(dict(record))
    return records


def _current_turn_unavailable_tools(metadata: Mapping[str, Any]) -> List[str]:
    """Return runtime-owned current-turn unavailable tools for planner control."""
    controls = metadata.get(_CURRENT_TURN_RUNTIME_CONTROLS_KEY)
    if not isinstance(controls, Mapping):
        return []

    requested_turn = metadata.get("turn_sequence")
    control_turn = controls.get("turn_sequence")
    if (
        isinstance(requested_turn, int)
        and isinstance(control_turn, int)
        and requested_turn != control_turn
    ):
        return []

    raw_tools = controls.get("unavailable_tools")
    if not isinstance(raw_tools, list):
        return []

    tools: List[str] = []
    for item in raw_tools:
        normalized = str(item or "").strip()
        if normalized and normalized not in tools:
            tools.append(normalized)
    return tools


def _combine_working_memory_summary(
    *,
    runtime_summary: str,
    phase_memory_section: str,
    max_summary_chars: int,
) -> str:
    """Prefer current-turn phase memory, then append bounded runtime summary."""
    parts = [
        str(phase_memory_section or "").strip(),
        str(runtime_summary or "").strip(),
    ]
    combined = "\n\n".join(part for part in parts if part)
    return combined[:max_summary_chars]


def _current_tool_params(
    metadata: Mapping[str, Any],
    previous_tool: Any,
) -> Mapping[str, Any]:
    last_tool_result = metadata.get("last_tool_result") or {}
    if isinstance(last_tool_result, Mapping) and isinstance(last_tool_result.get("parameters"), Mapping):
        return dict(last_tool_result.get("parameters") or {})
    primary_call = primary_tool_call_from_metadata(metadata)
    if primary_call is not None and primary_call.tool_id == previous_tool:
        return dict(primary_call.parameters)
    return {}


def build_planner_context(
    interactive: InteractiveState,
    request: ToolExecutionRequest,
    *,
    get_category_filtered_catalog: Callable[[List[str], Optional[AgentConfig]], List[str]],
    get_full_tool_catalog_for_planner: Callable[[Optional[AgentConfig]], List[str]],
    working_memory_summary_max_chars: int,
) -> Dict[str, Any]:
    """Build planner context while preserving existing key contract."""
    facts = interactive.facts
    trace = interactive.trace
    metadata = _metadata_from_facts(facts)

    history: List[Dict[str, Any]] = []
    compact_result = metadata.get("last_tool_result_compact") or {}
    compact_summary = str(compact_result.get("summary") or "").strip() or None
    if isinstance(request.history, list):
        for entry in request.history:
            if not isinstance(entry, Mapping):
                continue
            role = str(entry.get("role") or "assistant")
            content = sanitize_history_content(
                str(entry.get("content") or ""),
                compact_summary=compact_summary,
            )
            if content:
                history.append({"role": role, "content": content})

    for entry in trace.reasoning[-3:]:
        history.append({"role": "assistant", "content": str(entry)})
    for obs in trace.observations[-3:]:
        safe_obs = sanitize_history_content(
            str(obs),
            compact_summary=compact_summary,
        )
        history.append({"role": "assistant", "content": f"Observation: {safe_obs}"})

    if not history:
        history.append({"role": "user", "content": request.message})

    constraints = metadata.get("constraints", {})
    phase = metadata.get("current_phase") or metadata.get("phase") or "enumeration"
    scan_phase = metadata.get("current_scan_phase")
    if scan_phase:
        phase = scan_phase

    tool_intent = metadata.get("tool_intent") or {}
    next_tool_hint = facts.next_tool_hint or metadata.get("next_tool_hint")
    agent_config = metadata.get("agent_config")
    selected_categories = metadata.get("selected_categories")
    working_memory = metadata.get("working_memory")
    raw_intent_brief = (
        working_memory.get("intent_brief")
        if isinstance(working_memory, Mapping)
        else None
    )
    intent_brief = (
        raw_intent_brief if isinstance(raw_intent_brief, Mapping) else None
    )

    if selected_categories:
        resolved_tools = get_category_filtered_catalog(selected_categories, agent_config)
    else:
        resolved_tools = get_full_tool_catalog_for_planner(agent_config)

    resolved_tools = iter_non_artifact_tools(resolved_tools)
    if not resolved_tools:
        fallback_tools = get_full_tool_catalog_for_planner(agent_config)
        resolved_tools = iter_non_artifact_tools(fallback_tools)
    artifact_tool_exposure_metadata = {
        "allow_search": False,
        "allow_read": False,
        "has_persisted_artifacts": False,
        "known_artifact_ids": [],
        "evidence_gap_signal": False,
    }

    resolved_target = resolve_planner_target(
        user_message=str(request.message or facts.message or ""),
        request_targets=list(request.targets or []),
        metadata=metadata,
        history=history,
        tool_intent=tool_intent if isinstance(tool_intent, Mapping) else {},
    )
    targets_list: List[str] = []
    if resolved_target:
        targets_list.append(resolved_target)

    for raw_target in list(request.targets or []):
        candidate = coerce_target_value(raw_target)
        if candidate and candidate not in targets_list:
            targets_list.append(candidate)

    plan_text = facts.plan or []
    current_goal = facts.current_goal or ""
    last_tool_result = metadata.get("last_tool_result") or {}
    primary_call = primary_tool_call_from_metadata(metadata)
    previous_tool = (
        last_tool_result.get("tool")
        if isinstance(last_tool_result, Mapping)
        else None
    ) or (primary_call.tool_id if primary_call is not None else None)

    previous_tool_params = _current_tool_params(metadata, previous_tool)

    previous_tool_call = None
    if previous_tool and previous_tool_params:
        params_str = ", ".join(
            f"{k}={repr(v)}" for k, v in previous_tool_params.items() if v is not None
        )
        previous_tool_call = f"{previous_tool}({params_str})"
    elif previous_tool:
        previous_tool_call = previous_tool

    synthesized_output = metadata.get("synthesized_output") or {}
    compact_result = metadata.get("last_tool_result_compact") or {}
    previous_tool_output_summary = (
        synthesized_output.get("summary")
        or compact_result.get("summary")
        or ""
    )

    working_memory_context = build_working_memory_context_for_planner(
        metadata,
        max_summary_chars=working_memory_summary_max_chars,
    )
    current_turn_phases = _current_turn_phase_records(metadata)
    current_turn_unavailable_tools = _current_turn_unavailable_tools(metadata)
    planner_working_memory = dict(working_memory_context["working_memory"])
    if current_turn_phases:
        planner_working_memory["current_turn_phases"] = current_turn_phases

    turn_sequence = metadata.get("turn_sequence")
    phase_memory_section = _iteration_memory.render_phase_memory_section(
        dict(metadata),
        turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
    )
    latest_phase_memory = _iteration_memory.render_latest_phase_memory_section(
        dict(metadata),
        turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
    )
    working_memory_summary = _combine_working_memory_summary(
        runtime_summary=working_memory_context["working_memory_summary"],
        phase_memory_section=phase_memory_section,
        max_summary_chars=working_memory_summary_max_chars,
    )
    available_findings = []
    if isinstance(working_memory, Mapping):
        raw_available_findings = working_memory.get("available_findings")
        if isinstance(raw_available_findings, list):
            available_findings = raw_available_findings
    relevant_findings = select_relevant_findings_for_prompt(
        available_findings=available_findings,
        target=resolved_target,
        subject_hint_components=(
            facts.current_goal,
            next_tool_hint,
            previous_tool_params,
            tool_intent.get("focus") if isinstance(tool_intent, Mapping) else "",
        ),
        limit=8,
    )
    known_open_port_findings_count = count_known_open_port_findings(
        available_findings,
        target=resolved_target,
    )
    return {
        "user_message": request.message,
        "current_phase": phase,
        "constraints": constraints,
        "targets": targets_list,
        "task_id": request.task_id,
        "tool_intent": tool_intent if tool_intent else None,
        "resolved_tools": resolved_tools,
        "selected_categories": selected_categories,
        "artifact_tool_exposure": artifact_tool_exposure_metadata,
        "artifact_file_refs": collect_artifact_file_ref_candidates(metadata),
        "workspace_path": metadata.get("workspace_path") or request.workspace_path,
        "relevant_findings": relevant_findings,
        "known_open_port_findings_count": known_open_port_findings_count,
        "planner_metadata": {
            "todo_list": facts.todo_list,
            "plan": facts.plan,
            "current_goal": facts.current_goal,
        },
        "plan_text": plan_text,
        "current_goal": current_goal,
        "next_tool_hint": next_tool_hint,
        "previous_tool": previous_tool_call,
        "previous_tool_output_summary": previous_tool_output_summary,
        "working_memory": planner_working_memory,
        "working_memory_summary": working_memory_summary,
        "selection_working_memory_summary": working_memory_context["working_memory_summary"],
        "latest_phase_memory": latest_phase_memory,
        "referenced_prior_turns": working_memory_context["referenced_prior_turns"],
        "current_turn_unavailable_tools": current_turn_unavailable_tools,
        "intent_brief": intent_brief,
    }


def apply_plan_to_state(interactive: InteractiveState, plan_data: Dict[str, Any]) -> None:
    """Apply planner result payload to facts/trace state."""
    facts = interactive.facts
    facts.ensure_metadata().pop(UNAVAILABLE_CAPABILITY_METADATA_KEY, None)

    batch_calls = serialized_tool_calls_from_plan(plan_data)
    selected_tools = [call.tool_id for call in batch_calls]

    if selected_tools:
        facts.tool_ids = selected_tools
        facts.tool_candidates = list(plan_data.get("candidate_tools") or selected_tools)

    facts.metadata.setdefault("planner_plan", plan_data)
    execution_strategy = plan_data.get("execution_strategy")
    if execution_strategy:
        facts.metadata["planned_execution_strategy"] = execution_strategy

    reasoning = plan_data.get("reasoning")
    if reasoning:
        entry = f"Planning: {reasoning}"
        if not interactive.trace.reasoning or interactive.trace.reasoning[-1] != entry:
            interactive.trace.reasoning.append(entry)


def _unavailable_capability_summary(interactive: InteractiveState, plan: Any) -> str:
    """Build a concise PTR-facing unavailable-capability summary."""
    metadata = interactive.facts.safe_metadata
    tool_intent = metadata.get("tool_intent")
    intent_text = ""
    if isinstance(tool_intent, Mapping):
        intent_text = str(tool_intent.get("description") or "").strip()
    if not intent_text:
        intent_text = str(interactive.facts.next_tool_hint or "").strip()
    if not intent_text:
        intent_text = str(interactive.facts.current_goal or "").strip()

    target = str(getattr(plan, "target", "") or "").strip()
    base = "Tool selector found no available tool or reasonable substitute"
    if intent_text and target:
        return f"{base} for `{intent_text}` against `{target}`."
    if intent_text:
        return f"{base} for `{intent_text}`."
    if target:
        return f"{base} for target `{target}`."
    return f"{base} for the current tool intent."


def _record_unavailable_capability_phase(
    metadata: Dict[str, Any],
    *,
    summary: str,
    target: str,
    reasoning: str,
) -> None:
    """Append the synthetic planner-stage blocker to current-turn phase memory."""
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        return

    sections = [
        {
            "heading": "Tool Selection",
            "body": "\n".join(
                part
                for part in (
                    f"selected_tools: {UNAVAILABLE_CAPABILITY_TOOL}",
                    "status: unavailable_capability",
                    f"target: {target}" if target else "",
                    f"reason: {reasoning}" if reasoning else "",
                )
                if part
            ),
        },
        {"heading": "Tool Output Summary", "body": summary},
        {
            "heading": "Tool Errors",
            "body": "- Requested capability has no available tool or reasonable substitute.",
        },
    ]
    _iteration_memory.append(
        metadata,
        turn_sequence=turn_sequence,
        source="tool",
        payload={"sections": sections},
    )


def apply_unavailable_capability_to_state(
    interactive: InteractiveState,
    plan: Any,
) -> None:
    """Project an unavailable-capability selector result into PTR-readable state."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    summary = _unavailable_capability_summary(interactive, plan)
    target = str(getattr(plan, "target", "") or "").strip()
    reasoning = str(getattr(plan, "reasoning", "") or "").strip()

    marker = {
        "active": True,
        "status": "unavailable_capability",
        "selected_tools": [UNAVAILABLE_CAPABILITY_TOOL],
        "candidate_tools": [UNAVAILABLE_CAPABILITY_TOOL],
        "target": target,
        "summary": summary,
        "reasoning": reasoning,
    }
    metadata[UNAVAILABLE_CAPABILITY_METADATA_KEY] = marker
    metadata.pop("planner_plan", None)
    metadata.pop("tool_plan_prepared", None)
    metadata.pop("planned_execution_strategy", None)

    compact_result = {
        "schema_version": "2.0",
        "tool": UNAVAILABLE_CAPABILITY_TOOL,
        "status": "unavailable_capability",
        "success": False,
        "summary": summary,
        "key_findings": [],
        "errors": [
            "Requested capability has no available tool or reasonable substitute."
        ],
        "report_recommendations": [],
        "structured_signals": [
            {
                "kind": "tool_selection",
                "status": "unavailable_capability",
                "tool": UNAVAILABLE_CAPABILITY_TOOL,
            }
        ],
        "decision_evidence": [summary],
        "lossiness_risk": "low",
        "artifact_refs": [],
        "compression": {"source": "deterministic", "fallback_reason": None},
    }
    metadata["last_tool_result_compact"] = compact_result
    metadata["last_tool_result"] = {
        "tool": UNAVAILABLE_CAPABILITY_TOOL,
        "status": "unavailable_capability",
        "success": False,
        "parameters": {},
        "error": "Requested capability has no available tool or reasonable substitute.",
    }
    metadata["synthesized_output"] = {
        "status": "unavailable_capability",
        "success": False,
        "summary": summary,
        "key_findings": [],
    }

    _record_unavailable_capability_phase(
        metadata,
        summary=summary,
        target=target,
        reasoning=reasoning,
    )
    if reasoning:
        entry = f"Planning: {reasoning}"
        if not interactive.trace.reasoning or interactive.trace.reasoning[-1] != entry:
            interactive.trace.reasoning.append(entry)


async def ensure_action_plan(
    interactive: InteractiveState,
    request: ToolExecutionRequest,
    config: AgentConfig,
    *,
    build_action_for_planner: Callable[[InteractiveState, ToolExecutionRequest], Any],
    build_planner_context: Callable[[InteractiveState, ToolExecutionRequest], Dict[str, Any]],
) -> None:
    """Ensure planner output exists, reusing/invalidation-aware cache when possible."""
    metadata = request.metadata or {}

    if should_invalidate_plan(interactive):
        invalidate_plan(interactive, reason="state change detected before tool execution")
        metadata = interactive.facts.metadata_copy()
        safe_inc("langgraph_planner_plan_invalidated")

    plan_data = metadata.get("planner_plan")
    if plan_data:
        apply_plan_to_state(interactive, plan_data)
        safe_inc("langgraph_planner_plan_reused")
        return

    metadata.pop(UNAVAILABLE_CAPABILITY_METADATA_KEY, None)
    interactive.facts.metadata = metadata

    try:
        llm_client = None
        llm_client_resolver = getattr(config, "llm_client_resolver", None)
        if callable(llm_client_resolver):
            llm_client = llm_client_resolver()
        planner = EnhancedActionPlanner(config, llm_client=llm_client)
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize planner: {exc}") from exc

    action = build_action_for_planner(interactive, request)
    planner_context = build_planner_context(interactive, request)
    plan = await planner.build_action_plan(action, planner_context)
    if plan_is_unavailable_capability(plan):
        apply_unavailable_capability_to_state(interactive, plan)
        request.metadata = interactive.facts.metadata
        safe_inc("langgraph_planner_unavailable_capability")
        if plan.usage_records:
            if interactive.trace.usage_records is None:
                interactive.trace.usage_records = []
            interactive.trace.usage_records.extend(plan.usage_records)
        return

    plan_data = {
        "selected_tools": list(plan.selected_tools),
        "candidate_tools": list(getattr(plan, "candidate_tools", None) or plan.selected_tools),
        "execution_strategy": (
            plan.execution_strategy.value
            if isinstance(plan.execution_strategy, ExecutionStrategy)
            else str(plan.execution_strategy)
        ),
        "reasoning": plan.reasoning,
        "expected_outcome": plan.expected_outcome,
    }
    if getattr(plan, "tool_batch", None) is not None:
        plan_data["tool_batch"] = _serialize_tool_batch(plan.tool_batch)

    metadata["planner_plan"] = plan_data
    metadata["planner_context_snapshot"] = planner_context
    metadata["plan_context"] = create_plan_context(interactive)
    request.metadata = metadata
    interactive.facts.metadata = metadata

    apply_plan_to_state(interactive, plan_data)
    safe_inc("langgraph_planner_plan_created")

    if plan.usage_records:
        if interactive.trace.usage_records is None:
            interactive.trace.usage_records = []
        interactive.trace.usage_records.extend(plan.usage_records)


def get_full_tool_catalog_for_planner(
    config: Optional[AgentConfig],
    *,
    logger: Any,
) -> List[str]:
    """Get complete tool catalog for LLM-based selection."""
    try:
        from agent.tools.catalog_visibility import visible_available_tools
    except ImportError:
        logger.warning("[PLANNER_CONTEXT] Could not import catalog visibility, returning empty catalog")
        return []

    all_tools = visible_available_tools()
    logger.debug(f"[PLANNER_CONTEXT] visible_available_tools() returned {len(all_tools)} items")
    if all_tools:
        logger.debug(f"[PLANNER_CONTEXT] First few tools: {all_tools[:5]}")
    if not all_tools:
        logger.warning("[PLANNER_CONTEXT] Tool registry is empty")
        return []

    valid_tools = [
        t
        for t in all_tools
        if ("." in str(t) or "_" in str(t))
    ]
    valid_tools = _filter_hidden_catalog_tools(valid_tools, logger=logger)
    if not valid_tools:
        logger.warning(
            f"[PLANNER_CONTEXT] No valid tool IDs found in registry. "
            f"Got: {all_tools[:10]}. These look like metadata keys, not tool IDs!"
        )
        valid_tools = all_tools

    max_tools_limit = 10
    if config is not None:
        try:
            max_tools_limit = int(getattr(config, "max_tools_exposed", 10))
        except (TypeError, ValueError, AttributeError):
            pass

    limited_catalog = valid_tools[:max_tools_limit] if max_tools_limit > 0 else valid_tools
    logger.info(
        f"[PLANNER_CONTEXT] Providing {len(limited_catalog)} tools to planner "
        f"(from {len(all_tools)} available, limit={max_tools_limit})"
    )
    logger.debug(f"[PLANNER_CONTEXT] Catalog tools: {limited_catalog}")
    return limited_catalog


def _filter_hidden_catalog_tools(tools: List[str], *, logger: Any) -> List[str]:
    """Exclude tools hidden from LLM-facing catalogs."""
    try:
        from agent.tools.catalog_visibility import filter_visible_tool_ids
    except ImportError:
        logger.warning("[PLANNER_CONTEXT] Could not import catalog visibility policy")
        return tools
    return filter_visible_tool_ids(tools)


def get_category_filtered_catalog(
    categories: List[str],
    config: Optional[AgentConfig],
    *,
    logger: Any,
    get_full_tool_catalog_for_planner_fn: Callable[[Optional[AgentConfig]], List[str]],
) -> List[str]:
    """Get tool catalog filtered to requested categories plus utility categories."""
    _ = config
    try:
        from agent.tools.category_utils import get_tools_for_categories
    except ImportError:
        logger.warning("[PLANNER_CONTEXT] Could not import category_utils, falling back to full catalog")
        return get_full_tool_catalog_for_planner_fn(config)

    utility_categories = ["shell", "filesystem", "networking_utilities"]
    all_categories = list(set(categories + utility_categories))
    filtered_tools = _filter_hidden_catalog_tools(
        get_tools_for_categories(all_categories),
        logger=logger,
    )

    logger.info(
        f"[PLANNER_CONTEXT] Category-filtered catalog: {len(filtered_tools)} tools "
        f"from categories: {all_categories} (requested: {categories}, +utilities)"
    )
    logger.debug(f"[PLANNER_CONTEXT] Filtered tools: {filtered_tools[:10]}...")

    if not filtered_tools:
        logger.warning(
            f"[PLANNER_CONTEXT] No tools found for categories: {all_categories}. "
            "Falling back to full catalog."
        )
        return get_full_tool_catalog_for_planner_fn(config)

    logger.info(
        f"[PLANNER_CONTEXT] Providing complete category catalog: {len(filtered_tools)} tools "
        f"from categories: {all_categories}"
    )
    logger.debug(f"[PLANNER_CONTEXT] Category tools: {filtered_tools[:10]}...")
    return filtered_tools
