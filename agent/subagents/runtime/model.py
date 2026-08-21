"""Definition-configured subagent model/tool loop node.

Purpose
-------
Run one bounded model turn for a declarative subagent, allowing provider-native
tool calls while budget remains and accepting a concise text handoff when the
assignment is complete.

Responsibility boundary
-----------------------
This module owns LLM prompt construction, native tool-call validation, and
terminal handoff projection only. It does not execute tools, mutate
control-plane state, depend on control-plane services, or choose between legacy
and generic runtime paths.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from agent.config import AgentConfig
from agent.execution_strategy import ExecutionStrategy
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.emission.reasoning_section import reasoning_section
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.node_utils import _usage_to_dict
from agent.graph.state import InteractiveState
from agent.graph.utils import iteration_memory
from agent.graph.utils.llm_resolver import resolve_llm_client
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.providers.llm.core.base import LLMResponse, ToolCallResult
from agent.providers.llm.core.capabilities import LLMCapability
from agent.reasoning.llm_parameter_resolution import NATIVE_BUILDER_MAX_OUTPUT_TOKENS
from agent.reasoning.structured_contract_recovery import (
    contains_retryable_llm_timeout,
    run_structured_contract_retry,
)
from agent.subagents.contracts import AgentResult
from agent.subagents.definition import SubagentDefinition
from agent.subagents.runtime.state import (
    SubagentRuntimeState,
    subagent_state_from_graph_state,
)
from agent.subagents.runtime.tool_outcomes import (
    outcome_section_payload,
    project_tool_batch_outcome,
)
from agent.tool_runtime.batch.plan_view import serialized_tool_calls_from_metadata
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tools.builder_intent import split_builder_intent
from agent.tools.parameter_validation import validate_tool_parameters
from agent.tools.tool_call_specs import build_function_tool_specs_for
from core.llm import LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC, wait_for_with_timeout
from core.prompts.builders.post_tool.evidence import (
    select_compact_evidence_for_reasoning,
)
from core.prompts.builders.post_tool.last_tool import LAST_TOOL_SECTION_HEADINGS
from core.prompts.builders.subagent_runtime import SubagentRuntimePromptBuilder
from core.prompts.builders.skill_guidance import PromptSkill
from core.skills.registry import SkillRegistry, get_skill_registry


logger = logging.getLogger(__name__)

SUBAGENT_ACTION_METADATA_KEY = "subagent_action"
SUBAGENT_RESULT_METADATA_KEY = "subagent_result"
SUBAGENT_EXECUTION_STRATEGY_KEY = "_execution_strategy"
SUBAGENT_FORCED_FINAL_METADATA_KEY = "subagent_forced_final"
SUBAGENT_COUNTED_TOOL_BATCH_METADATA_KEY = "subagent_counted_tool_batch_id"
_SUBAGENT_EXECUTION_STRATEGY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["parallel", "sequential"],
    "description": (
        "Batch scheduling metadata shared by every native call in this response. "
        "Not a tool parameter."
    ),
}


class SubagentActionSelectionError(ValueError):
    """Raised when a subagent cannot produce a safe canonical tool batch."""


async def run_subagent_model_turn(
    definition: SubagentDefinition,
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
    writer: Any = None,
    llm_resolver: Callable[..., Any] | None = None,
    skill_registry: SkillRegistry | None = None,
) -> dict[str, Any]:
    """Run one bounded subagent model turn and route to tools or handoff."""

    interactive = InteractiveState.from_mapping(state)
    subagent = subagent_state_from_graph_state(interactive, definition=definition)
    materialized_skills = (skill_registry or get_skill_registry()).materialize(
        subagent.resolved_skills
    )
    prompt_skills = tuple(
        PromptSkill(
            skill_id=skill.skill_id,
            description=skill.metadata.description,
            body=skill.body,
        )
        for skill in materialized_skills
    )

    max_committed_calls = _max_committed_calls(definition)
    remaining_iterations = _remaining_iterations(definition, interactive)
    finalization_only = remaining_iterations <= 0
    can_call_tools = (
        bool(subagent.tool_profile.tools)
        and not finalization_only
        and max_committed_calls > 0
    )
    if can_call_tools:
        tool_specs, function_to_tool_id = _build_subagent_function_specs(subagent)
    else:
        tool_specs = []
        function_to_tool_id = {}

    prompt_builder = SubagentRuntimePromptBuilder()
    role_prompt = definition.runtime_role_prompt or definition.instructions
    boundary_rules = definition.runtime_boundary_rules or (
        definition.ownership_boundary,
    )
    resolver = llm_resolver or resolve_llm_client
    llm_client = resolver(
        interactive.facts.ensure_metadata(),
        context,
        config=config,
        role="reasoning_main",
    )
    request_kwargs: dict[str, Any] = {
        "temperature": 0.1,
        "max_tokens": NATIVE_BUILDER_MAX_OUTPUT_TOKENS,
    }
    if can_call_tools:
        request_kwargs["tool_choice"] = "auto"
        supports_capability = getattr(llm_client, "supports_capability", None)
        if callable(supports_capability) and supports_capability(
            LLMCapability.PARALLEL_TOOLS
        ):
            request_kwargs["parallel_tool_calls"] = True

    system_prompt = prompt_builder.build_system_prompt(
        definition_id=definition.id,
        display_name=definition.display_name,
        role_prompt=role_prompt,
        definition_instructions=definition.instructions,
        ownership_boundary=definition.ownership_boundary,
        boundary_rules=boundary_rules,
        max_committed_tools_per_batch=max_committed_calls,
        callable_tool_ids=(
            tuple(spec.tool_id for spec in tool_specs) if can_call_tools else ()
        ),
        prompt_skills=prompt_skills,
        finalization_only=finalization_only,
    )
    user_prompt = prompt_builder.build_user_prompt(
        display_name=definition.display_name,
        assignment=subagent.assignment.model_dump(mode="json"),
        tool_ids=subagent.tool_profile.tool_ids,
        working_memory=_working_memory_prompt_context(interactive.facts.safe_metadata),
        previous_tool_summary=_build_previous_tool_context(interactive),
        finalization_only=finalization_only,
    )

    async with reasoning_section(
        writer,
        state=interactive,
        step="subagent_model",
        label="Running the subagent model turn.",
        config=config,
        context=context,
    ) as emitter:
        async def request_action() -> tuple[str, Any]:
            if can_call_tools:
                request = llm_client.chat_with_tools_with_usage(
                    system_prompt,
                    user_prompt,
                    tools=tool_specs,
                    **request_kwargs,
                )
            else:
                request = llm_client.chat_with_usage(
                    system_prompt,
                    user_prompt,
                    **request_kwargs,
                )
            result = await wait_for_with_timeout(
                request,
                timeout_sec=LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
                component="SUBAGENT",
                operation="subagent_runtime_model_llm_call",
                logger=logger,
                task_id=interactive.facts.task_id,
                outcome="subagent_runtime_model_timeout",
            )

            _append_usage(interactive, result)
            if not can_call_tools:
                return (
                    "handoff",
                    _apply_subagent_text_result(
                        interactive,
                        subagent,
                        result,
                        forced_final=True,
                    ),
                )
            if not result.tool_calls:
                return (
                    "handoff",
                    _apply_subagent_text_result(
                        interactive,
                        subagent,
                        result,
                        forced_final=not can_call_tools,
                    ),
                )

            if not can_call_tools:
                raise SubagentActionSelectionError(
                    "Subagent returned tool calls after tool budget was exhausted"
                )
            return (
                "tool",
                _build_tool_batch_from_result(
                    result,
                    subagent=subagent,
                    function_to_tool_id=function_to_tool_id,
                    max_committed_calls=max_committed_calls,
                    display_name=definition.display_name,
                ),
            )

        try:
            route, selected = await run_structured_contract_retry(
                operation=request_action,
                logger=logger,
                stage="subagent_model",
                contract="native_action",
                max_attempts=2,
                backoff_seconds=0.25,
                is_retryable_error=lambda exc: isinstance(
                    exc,
                    SubagentActionSelectionError,
                )
                or contains_retryable_llm_timeout(exc),
            )
        except SubagentActionSelectionError as exc:
            logger.warning(
                "Subagent model action contract remained invalid after retry: %s",
                exc,
            )
            if emitter is not None:
                emitter.emit_reasoning_delta(
                    "Subagent could not produce a valid bounded action and is "
                    "returning a blocked handoff."
                )
            return _apply_subagent_blocked_result(interactive, subagent)

        if route == "handoff":
            if emitter is not None:
                emitter.emit_reasoning_delta("Subagent prepared parent handoff.")
            return selected

        batch = selected
        if not isinstance(batch, ToolBatch):
            raise TypeError("Subagent action selection returned an invalid tool batch")
        if emitter is not None:
            emitter.emit_reasoning_delta(batch.selection_rationale)
    return _apply_subagent_tool_batch(
        interactive,
        subagent,
        batch,
        display_name=definition.display_name,
    )


def _build_subagent_function_specs(
    subagent: SubagentRuntimeState,
) -> tuple[list[FunctionToolSpec], dict[str, str]]:
    """Return every bounded subagent tool with scheduling metadata injected."""

    tool_ids = list(subagent.tool_profile.tool_ids)
    if len(tool_ids) != len(set(tool_ids)):
        raise SubagentActionSelectionError(
            "Subagent tool profile contains duplicate tool ids"
        )
    specs, mapping = build_function_tool_specs_for(tool_ids)
    return [_inject_execution_strategy(spec) for spec in specs], mapping


def _inject_execution_strategy(spec: FunctionToolSpec) -> FunctionToolSpec:
    """Add batch scheduling metadata to one function schema."""

    schema = deepcopy(spec.parameters_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise SubagentActionSelectionError(
            f"Subagent tool {spec.tool_id!r} does not expose an object schema"
        )
    properties[SUBAGENT_EXECUTION_STRATEGY_KEY] = dict(
        _SUBAGENT_EXECUTION_STRATEGY_SCHEMA
    )
    required = schema.get("required")
    if not isinstance(required, list):
        required = []
        schema["required"] = required
    if SUBAGENT_EXECUTION_STRATEGY_KEY not in required:
        required.append(SUBAGENT_EXECUTION_STRATEGY_KEY)
    return FunctionToolSpec(
        tool_id=spec.tool_id,
        name=spec.name,
        description=spec.description,
        parameters_schema=schema,
    )


def _build_tool_batch_from_result(
    result: ToolCallResult,
    *,
    subagent: SubagentRuntimeState,
    function_to_tool_id: Mapping[str, str],
    max_committed_calls: int,
    display_name: str,
) -> ToolBatch:
    """Parse, validate, and order native calls as one canonical batch."""

    native_calls = list(result.tool_calls or [])
    if not native_calls:
        raise SubagentActionSelectionError(
            "Subagent runtime model returned no native tool calls"
        )
    if len(native_calls) > max_committed_calls:
        raise SubagentActionSelectionError(
            "Subagent runtime model exceeded the committed call cap: "
            f"{len(native_calls)} > {max_committed_calls}"
        )

    batch_strategy: ExecutionStrategy | None = None
    committed_calls: list[ToolCall] = []
    for native_call in native_calls:
        function_name, raw_arguments = _call_name_and_arguments(native_call)
        tool_id = function_to_tool_id.get(function_name)
        if tool_id is None:
            raise SubagentActionSelectionError(
                f"Subagent selected unbound function {function_name!r}"
            )
        if tool_id not in subagent.tool_profile.tool_ids:
            raise SubagentActionSelectionError(
                f"Subagent tool {tool_id!r} is not allowlisted"
            )

        raw_parameters = _parse_arguments(raw_arguments)
        strategy = _pop_execution_strategy(
            raw_parameters,
            default=(
                ExecutionStrategy.SEQUENTIAL if len(native_calls) == 1 else None
            ),
        )
        if batch_strategy is None:
            batch_strategy = strategy
        elif strategy is not batch_strategy:
            raise SubagentActionSelectionError(
                "Subagent native calls declared inconsistent execution strategies"
            )

        parameters_without_intent, builder_intent = split_builder_intent(
            raw_parameters
        )
        if not isinstance(parameters_without_intent, dict):
            raise SubagentActionSelectionError(
                "Subagent tool arguments must decode to a JSON object"
            )
        parameters = _validate_registered_tool_parameters(
            tool_id,
            parameters_without_intent,
        )
        committed_calls.append(
            ToolCall(
                tool_call_id=f"subagent-call-{uuid.uuid4().hex}",
                tool_id=tool_id,
                parameters=parameters,
                intent=builder_intent.strip(),
            )
        )

    if batch_strategy is None:
        raise SubagentActionSelectionError(
            "Subagent runtime model omitted execution strategy"
        )
    if len(committed_calls) == 1:
        batch_strategy = ExecutionStrategy.SEQUENTIAL

    rationale = "; ".join(call.intent for call in committed_calls if call.intent)
    return ToolBatch(
        tool_batch_id=f"subagent-batch-{uuid.uuid4().hex}",
        tool_calls=tuple(committed_calls),
        requested_execution_strategy=batch_strategy,
        selection_rationale=rationale
        or f"{display_name} committed bounded tool calls.",
    )


def _apply_subagent_text_result(
    interactive: InteractiveState,
    subagent: SubagentRuntimeState,
    result: LLMResponse | ToolCallResult,
    *,
    forced_final: bool,
) -> dict[str, Any]:
    """Project a model text response into terminal handoff state."""

    summary = _clean_text(result.content)
    if not summary:
        raise SubagentActionSelectionError(
            "Subagent model turn returned neither tool calls nor handoff text"
        )

    metadata = interactive.facts.ensure_metadata()
    metadata.pop(SUBAGENT_RESULT_METADATA_KEY, None)
    metadata[SUBAGENT_FORCED_FINAL_METADATA_KEY] = forced_final
    metadata[SUBAGENT_ACTION_METADATA_KEY] = {
        "route": "handoff",
        "agent_run_id": subagent.agent_run_id,
        "agent_id": subagent.agent_id,
        "forced_final": forced_final,
    }
    interactive.trace.final_text = summary
    interactive.trace.history.append(
        {
            "type": "subagent_model_handoff",
            "agent_run_id": subagent.agent_run_id,
            "agent_id": subagent.agent_id,
            "forced_final": forced_final,
        }
    )
    return interactive.as_graph_update()


def _apply_subagent_blocked_result(
    interactive: InteractiveState,
    subagent: SubagentRuntimeState,
) -> dict[str, Any]:
    """Finish a bounded run when model action repair cannot produce a safe action."""

    result = AgentResult(
        agent_run_id=subagent.agent_run_id,
        agent_id=subagent.agent_id,
        agent_kind=subagent.agent_kind,
        outcome="blocked",
        summary="Subagent could not produce a valid bounded tool action.",
        limitations=("The subagent model action contract remained invalid after retry.",),
        recommended_next_steps=(
            "Continue from the parent agent or dispatch a new bounded assignment.",
        ),
    )
    metadata = interactive.facts.ensure_metadata()
    metadata[SUBAGENT_RESULT_METADATA_KEY] = result.model_dump(mode="json")
    metadata[SUBAGENT_FORCED_FINAL_METADATA_KEY] = True
    metadata[SUBAGENT_ACTION_METADATA_KEY] = {
        "route": "handoff",
        "agent_run_id": subagent.agent_run_id,
        "agent_id": subagent.agent_id,
        "forced_final": True,
    }
    interactive.trace.final_text = result.summary
    interactive.trace.history.append(
        {
            "type": "subagent_model_blocked",
            "agent_run_id": subagent.agent_run_id,
            "agent_id": subagent.agent_id,
        }
    )
    return interactive.as_graph_update()


def _call_name_and_arguments(call: Any) -> tuple[str, Any]:
    """Return normalized provider function name and raw arguments."""

    if isinstance(call, Mapping):
        function = call.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            arguments = function.get("arguments")
        else:
            name = call.get("name")
            arguments = call.get("arguments")
    else:
        name = getattr(call, "name", None)
        arguments = getattr(call, "arguments", None)
    if not isinstance(name, str) or not name.strip():
        raise SubagentActionSelectionError(
            "Subagent native call is missing a function name"
        )
    return name.strip(), arguments


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    """Decode one provider-native JSON arguments object."""

    if isinstance(raw_arguments, str):
        if not raw_arguments.strip():
            raise SubagentActionSelectionError(
                "Subagent native call arguments are empty"
            )
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise SubagentActionSelectionError(
                f"Subagent native call arguments are not valid JSON: {exc}"
            ) from exc
    elif isinstance(raw_arguments, Mapping):
        decoded = dict(raw_arguments)
    else:
        raise SubagentActionSelectionError(
            "Subagent native call arguments must be a JSON object"
        )
    if not isinstance(decoded, dict):
        raise SubagentActionSelectionError(
            "Subagent native call arguments must decode to a JSON object"
        )
    return decoded


def _pop_execution_strategy(
    raw_parameters: dict[str, Any],
    *,
    default: ExecutionStrategy | None = None,
) -> ExecutionStrategy:
    """Strip and validate batch scheduling metadata."""

    raw_strategy = raw_parameters.pop(SUBAGENT_EXECUTION_STRATEGY_KEY, None)
    if raw_strategy is None and default is not None:
        return default
    normalized = str(raw_strategy or "").strip().lower()
    if normalized == ExecutionStrategy.PARALLEL.value:
        return ExecutionStrategy.PARALLEL
    if normalized == ExecutionStrategy.SEQUENTIAL.value:
        return ExecutionStrategy.SEQUENTIAL
    raise SubagentActionSelectionError(
        "Subagent native call must declare _execution_strategy as "
        "'parallel' or 'sequential'"
    )


def _validate_registered_tool_parameters(
    tool_id: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Return canonical execution parameters compiled from planner output."""

    validation = validate_tool_parameters(
        tool_id,
        dict(parameters),
        validation_stage="planner",
    )
    if validation.valid:
        return dict(validation.normalized_parameters)

    details = "; ".join(
        f"{error.get('field', 'arguments')}: "
        f"{error.get('message') or error.get('error') or 'invalid value'}"
        for error in validation.validation_errors
    )
    suffix = f": {details}" if details else ""
    raise SubagentActionSelectionError(
        f"Subagent selected invalid parameters for {tool_id} "
        f"({validation.reason or 'validation_error'}){suffix}"
    )


def _apply_subagent_tool_batch(
    interactive: InteractiveState,
    subagent: SubagentRuntimeState,
    batch: ToolBatch,
    *,
    display_name: str,
) -> dict[str, Any]:
    """Project the canonical native-call batch into shared runtime metadata."""

    committed_tool_ids = [call.tool_id for call in batch.tool_calls]
    plan_data = {
        "selected_tools": committed_tool_ids,
        "candidate_tools": list(subagent.tool_profile.tool_ids),
        "execution_strategy": batch.requested_execution_strategy.value,
        "reasoning": batch.selection_rationale,
        "expected_outcome": batch.selection_rationale,
        "tool_batch": _serialize_tool_batch(batch),
    }
    first_call = batch.tool_calls[0]
    metadata = interactive.facts.ensure_metadata()
    metadata.pop(SUBAGENT_RESULT_METADATA_KEY, None)
    metadata["planner_plan"] = plan_data
    metadata["tool_plan_prepared"] = True
    metadata["planned_execution_strategy"] = batch.requested_execution_strategy.value
    metadata[SUBAGENT_ACTION_METADATA_KEY] = {
        "route": "tool",
        "agent_run_id": subagent.agent_run_id,
        "agent_id": subagent.agent_id,
        "tool_id": first_call.tool_id,
        "tool_ids": committed_tool_ids,
        "tool_batch_id": batch.tool_batch_id,
    }
    interactive.facts.tool_ids = committed_tool_ids
    interactive.facts.tool_candidates = list(subagent.tool_profile.tool_ids)
    interactive.facts.selected_tool = first_call.tool_id
    interactive.facts.tool_parameters = dict(first_call.parameters)
    interactive.trace.reasoning.append(
        f"{display_name} committed "
        f"{len(batch.tool_calls)} tool call(s) "
        f"for {batch.requested_execution_strategy.value} execution."
    )
    return interactive.as_graph_update()


def record_subagent_observation_and_budget(
    definition: SubagentDefinition,
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record bounded tool context and synchronize the child execution budget."""

    _ = (context, config)
    interactive = InteractiveState.from_mapping(state)
    subagent_state_from_graph_state(interactive, definition=definition)
    _record_missing_subagent_tool_phase(interactive)
    _sync_completed_execution_iterations(interactive)
    return interactive.as_graph_update()


def _record_missing_subagent_tool_phase(interactive: InteractiveState) -> None:
    """Record one authoritative compact outcome for an uncounted tool batch."""

    metadata = interactive.facts.ensure_metadata()
    batch_id = _current_tool_batch_id(metadata)
    counted_batch_id = str(
        metadata.get(SUBAGENT_COUNTED_TOOL_BATCH_METADATA_KEY) or ""
    ).strip()
    if not batch_id or batch_id == counted_batch_id:
        return

    completed = max(int(interactive.facts.iterations or 0), 0)
    tool_phase_count = _completed_tool_phase_count(metadata)

    turn_sequence = _phase_memory_turn_sequence(metadata)
    if not isinstance(turn_sequence, int):
        return
    evidence, _ = select_compact_evidence_for_reasoning(metadata)
    if evidence is None:
        return
    outcome = project_tool_batch_outcome(
        evidence,
        tool_calls=serialized_tool_calls_from_metadata(metadata),
    )
    if not outcome.get("calls"):
        return
    payload = outcome_section_payload(outcome)
    if tool_phase_count > completed:
        attached = iteration_memory.append_sections_to_latest_record(
            metadata,
            turn_sequence=turn_sequence,
            source="tool",
            payload=payload,
        )
        if attached is not None:
            return
    iteration_memory.append(
        metadata,
        turn_sequence=turn_sequence,
        source="tool",
        payload=payload,
    )


def _serialize_tool_batch(batch: ToolBatch) -> dict[str, Any]:
    """Serialize the canonical batch for checkpoint-safe runtime dispatch."""

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


def _append_usage(
    interactive: InteractiveState,
    result: LLMResponse | ToolCallResult,
) -> None:
    """Append the single subagent model call to normal usage accounting."""

    usage = _usage_to_dict(
        result.usage,
        "subagent_runtime_model",
        request_mode="non_streaming",
    )
    if usage is None:
        return
    if interactive.trace.usage_records is None:
        interactive.trace.usage_records = []
    interactive.trace.usage_records.append(usage)


def _max_committed_calls(definition: SubagentDefinition) -> int:
    """Return the per-turn native call cap allowed for this definition."""

    configured = max(int(AgentConfig().max_committed_tools_per_batch), 0)
    definition_limit = max(int(definition.max_tool_calls_per_iteration), 0)
    return min(configured, definition_limit) if configured else definition_limit


def _remaining_iterations(
    definition: SubagentDefinition,
    interactive: InteractiveState,
) -> int:
    """Return remaining tool-capable model turns for this child session."""

    completed_iterations = max(int(interactive.facts.iterations or 0), 0)
    return max(max(int(definition.max_iterations), 1) - completed_iterations, 0)


def _bounded_mapping(value: Any) -> dict[str, Any]:
    """Return checkpoint-safe prompt context when a mapping is available."""

    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _working_memory_prompt_context(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return the existing bounded runtime-state projection for the child prompt."""

    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, Mapping):
        return {}
    return _bounded_mapping(bundle.get("runtime_state"))


def _build_previous_tool_context(interactive: InteractiveState) -> dict[str, Any]:
    """Return canonical phase memory, falling back to the latest compact result."""

    metadata = interactive.facts.safe_metadata
    phase_memory = iteration_memory.render_phase_memory_section(
        _existing_phase_memory_prompt_metadata(metadata),
        turn_sequence=_phase_memory_turn_sequence(metadata),
    )
    if phase_memory:
        return {"current_turn_phase_memory": phase_memory}

    evidence, _ = select_compact_evidence_for_reasoning(metadata)
    compact = (
        dict(evidence.raw)
        if evidence is not None
        and evidence.raw.get("execution_session_aggregate") is True
        else {}
    )
    if not compact:
        compact = _bounded_mapping(interactive.facts.last_tool_result_compact) or (
            _bounded_mapping(metadata.get("last_tool_result_compact"))
        )
    return {"last_tool_result": compact} if compact else {}


def _existing_phase_memory_prompt_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the canonical ledger without redundant batch-result sections."""

    working_memory = metadata.get("working_memory")
    raw_records = (
        working_memory.get("current_turn_phases")
        if isinstance(working_memory, Mapping)
        else None
    )
    records: list[dict[str, Any]] = []
    for raw_record in raw_records or []:
        if not isinstance(raw_record, Mapping):
            continue
        record = dict(raw_record)
        source = record.get("source")
        record["sections"] = [
            dict(section)
            for section in raw_record.get("sections") or []
            if isinstance(section, Mapping)
            and not (
                source == "tool"
                and section.get("heading")
                == LAST_TOOL_SECTION_HEADINGS["batch_tool_results"]
            )
        ]
        records.append(record)
    return {"working_memory": {"current_turn_phases": records}}


def _sync_completed_execution_iterations(interactive: InteractiveState) -> None:
    """Count each terminal tool batch once, including transient sessions."""

    metadata = interactive.facts.ensure_metadata()
    completed = max(int(interactive.facts.iterations or 0), 0)
    tool_phase_count = _completed_tool_phase_count(metadata)
    batch_id = _current_tool_batch_id(metadata)
    counted_batch_id = str(
        metadata.get(SUBAGENT_COUNTED_TOOL_BATCH_METADATA_KEY) or ""
    ).strip()

    if tool_phase_count > completed:
        completed = tool_phase_count
    elif batch_id and batch_id != counted_batch_id:
        completed += 1

    if batch_id:
        metadata[SUBAGENT_COUNTED_TOOL_BATCH_METADATA_KEY] = batch_id
    interactive.facts.metadata = metadata
    interactive.facts.iterations = completed


def _completed_tool_phase_count(metadata: Mapping[str, Any]) -> int:
    """Return canonical completed tool iterations from current-turn phase memory."""

    return sum(
        1
        for record in iteration_memory.get_ledger(dict(metadata))
        if record.get("source") == "tool"
    )


def _current_tool_batch_id(metadata: Mapping[str, Any]) -> str:
    """Return the active batch identity from existing compact metadata."""

    batch_id = str(metadata.get("tool_batch_id") or "").strip()
    if batch_id:
        return batch_id
    compact_batch = metadata.get("last_tool_result_compact_batch")
    if not isinstance(compact_batch, Mapping):
        return ""
    return str(compact_batch.get("tool_batch_id") or "").strip()


def _phase_memory_turn_sequence(metadata: Mapping[str, Any]) -> int | None:
    """Return the active phase-memory turn scope when present."""

    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return None
    turn_sequence = working_memory.get("current_turn_phase_turn")
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        return turn_sequence
    return None


def _clean_text(value: Any) -> str:
    """Normalize nullable prompt/result text to a stripped string."""

    if value is None:
        return ""
    return str(value).strip()


__all__ = [
    "SUBAGENT_ACTION_METADATA_KEY",
    "SUBAGENT_COUNTED_TOOL_BATCH_METADATA_KEY",
    "SUBAGENT_EXECUTION_STRATEGY_KEY",
    "SUBAGENT_FORCED_FINAL_METADATA_KEY",
    "SUBAGENT_RESULT_METADATA_KEY",
    "SubagentActionSelectionError",
    "record_subagent_observation_and_budget",
    "run_subagent_model_turn",
]
