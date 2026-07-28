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
from typing import Any, Sequence

from agent.config import AgentConfig
from agent.execution_strategy import ExecutionStrategy
from agent.graph.emission.reasoning_section import reasoning_section
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.node_utils import _usage_to_dict
from agent.graph.state import InteractiveState
from agent.graph.utils.llm_resolver import resolve_llm_client
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.providers.llm.core.base import ToolCallResult
from agent.reasoning.llm_parameter_resolution import NATIVE_BUILDER_MAX_OUTPUT_TOKENS
from agent.subagents.definition import SubagentDefinition
from agent.subagents.runtime.state import (
    SubagentRuntimeState,
    subagent_state_from_graph_state,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tools.builder_intent import split_builder_intent
from agent.tools.tool_call_specs import build_function_tool_specs_for
from agent.tools.tool_registry import get_tool
from core.llm import LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC, wait_for_with_timeout
from core.prompts.builders.subagent_runtime import SubagentRuntimePromptBuilder


logger = logging.getLogger(__name__)

SUBAGENT_ACTION_METADATA_KEY = "subagent_action"
SUBAGENT_RESULT_METADATA_KEY = "subagent_result"
SUBAGENT_EXECUTION_STRATEGY_KEY = "_execution_strategy"
SUBAGENT_OBSERVATION_TRANSCRIPT_KEY = "subagent_observation_transcript"
SUBAGENT_FORCED_FINAL_METADATA_KEY = "subagent_forced_final"
SUBAGENT_ITERATION_MARKERS_KEY = "subagent_completed_iteration_markers"
_MAX_SUBAGENT_OBSERVATIONS = 6
_MAX_OBSERVATION_FINDINGS = 8

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


class SubagentToolBuilderPromptBuilder:
    """Build one native-call request over all definition-visible tools."""

    def __init__(self, definition: SubagentDefinition) -> None:
        self._definition = definition
        self._delegate = SubagentRuntimePromptBuilder()

    def build_system_prompt(
        self,
        *,
        max_committed_tools_per_batch: int,
    ) -> str:
        """Return canonical shared guidance plus scheduling metadata."""

        display_name = self._definition.display_name
        role_prompt = self._definition.tool_builder_role_prompt
        if role_prompt is None:
            role_prompt = self._definition.instructions
        boundary_rules = self._definition.tool_builder_boundary_rules
        if not boundary_rules:
            boundary_rules = (self._definition.ownership_boundary,)
        return self._delegate.build_system_prompt(
            definition_id=self._definition.id,
            display_name=display_name,
            role_prompt=role_prompt,
            definition_instructions=self._definition.instructions,
            ownership_boundary=self._definition.ownership_boundary,
            boundary_rules=boundary_rules,
            max_committed_tools_per_batch=max_committed_tools_per_batch,
        )

    def build_user_prompt(
        self,
        *,
        assignment: Mapping[str, Any],
        tool_ids: Sequence[str],
        working_memory: Mapping[str, Any] | None = None,
        previous_tool_summary: Mapping[str, Any] | None = None,
        remaining_limits: Mapping[str, Any] | None = None,
    ) -> str:
        """Return bounded assignment context for the canonical builder rules."""

        return self._delegate.build_user_prompt(
            display_name=self._definition.display_name,
            assignment=assignment,
            tool_ids=tool_ids,
            working_memory=working_memory,
            previous_tool_summary=previous_tool_summary,
            remaining_limits=remaining_limits,
        )


async def choose_subagent_action(
    definition: SubagentDefinition,
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
    writer: Any = None,
    llm_resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run one bounded subagent model turn and route to tools or completion."""

    interactive = InteractiveState.from_mapping(state)
    subagent = subagent_state_from_graph_state(interactive, definition=definition)
    _refresh_subagent_observation_transcript(interactive)

    max_committed_calls = _max_committed_calls(definition)
    can_call_tools = (
        bool(subagent.tool_profile.tools)
        and _remaining_iterations(definition, interactive) > 0
        and max_committed_calls > 0
    )
    if can_call_tools:
        tool_specs, function_to_tool_id = _build_subagent_function_specs(subagent)
        tool_choice = "auto"
    else:
        tool_specs = []
        function_to_tool_id = {}
        tool_choice = "none"

    prompt_builder = SubagentToolBuilderPromptBuilder(definition)
    resolver = llm_resolver or resolve_llm_client
    llm_client = resolver(
        interactive.facts.ensure_metadata(),
        context,
        config=config,
        role="reasoning_main",
    )
    request_kwargs: dict[str, Any] = {
        "tool_choice": tool_choice,
        "temperature": 0.1,
        "max_tokens": NATIVE_BUILDER_MAX_OUTPUT_TOKENS,
    }
    if can_call_tools:
        request_kwargs["parallel_tool_calls"] = True

    async with reasoning_section(
        writer,
        state=interactive,
        step="subagent_action_selection",
        label="Selecting reconnaissance tools and preparing the execution batch.",
        config=config,
        context=context,
    ) as emitter:
        result = await wait_for_with_timeout(
            llm_client.chat_with_tools_with_usage(
                prompt_builder.build_system_prompt(
                    max_committed_tools_per_batch=max_committed_calls,
                ),
                prompt_builder.build_user_prompt(
                    assignment=subagent.assignment.model_dump(mode="json"),
                    tool_ids=subagent.tool_profile.tool_ids,
                    working_memory=_bounded_mapping(
                        interactive.facts.safe_metadata.get("working_memory")
                    ),
                    previous_tool_summary=_build_previous_tool_context(interactive),
                    remaining_limits=_build_remaining_limits(
                        definition,
                        interactive,
                        max_committed_calls=max_committed_calls,
                    ),
                ),
                tools=tool_specs,
                **request_kwargs,
            ),
            timeout_sec=LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
            component="SUBAGENT",
            operation="native_tool_builder_llm_call",
            logger=logger,
            task_id=interactive.facts.task_id,
            outcome="native_tool_builder_timeout",
        )

        _append_usage(interactive, result)
        if not result.tool_calls:
            update = _apply_subagent_text_result(
                interactive,
                subagent,
                result,
                forced_final=not can_call_tools,
            )
            if emitter is not None:
                emitter.emit_reasoning_delta("Subagent prepared parent handoff.")
            return update

        if not can_call_tools:
            raise SubagentActionSelectionError(
                "Subagent returned tool calls after tool budget was exhausted"
            )
        batch = _build_tool_batch_from_result(
            result,
            subagent=subagent,
            function_to_tool_id=function_to_tool_id,
            max_committed_calls=max_committed_calls,
            display_name=definition.display_name,
        )
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
            "Subagent native tool builder returned no calls"
        )
    if len(native_calls) > max_committed_calls:
        raise SubagentActionSelectionError(
            "Subagent native tool builder exceeded the committed call cap: "
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
        strategy = _pop_execution_strategy(raw_parameters)
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
            "Subagent native tool builder omitted execution strategy"
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
    result: ToolCallResult,
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
        "route": "complete",
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


def _pop_execution_strategy(raw_parameters: dict[str, Any]) -> ExecutionStrategy:
    """Strip and validate batch scheduling metadata."""

    raw_strategy = raw_parameters.pop(SUBAGENT_EXECUTION_STRATEGY_KEY, None)
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
    """Validate builder output against the registered planner schema."""

    tool_cls = get_tool(tool_id)
    schema_model = tool_cls.get_planner_args_model()
    if schema_model is None:
        return dict(parameters)
    allowed_fields = set(getattr(schema_model, "model_fields", {}) or {})
    unknown_fields = sorted(set(parameters) - allowed_fields)
    if unknown_fields:
        raise SubagentActionSelectionError(
            f"Subagent selected unsupported parameters for {tool_id}: {unknown_fields}"
        )
    try:
        validated = schema_model.model_validate(dict(parameters))
    except Exception as exc:
        raise SubagentActionSelectionError(
            f"Subagent selected invalid parameters for {tool_id}: {exc}"
        ) from exc
    dumped = validated.model_dump(mode="json")
    return dumped if isinstance(dumped, dict) else dict(parameters)


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
    """Record compact observations and advance completed execution budget once."""

    _ = (context, config)
    interactive = InteractiveState.from_mapping(state)
    subagent = subagent_state_from_graph_state(interactive, definition=definition)
    _refresh_subagent_observation_transcript(interactive)
    _advance_completed_execution_iteration(interactive, subagent)
    return interactive.as_graph_update()


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


def _append_usage(interactive: InteractiveState, result: ToolCallResult) -> None:
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


def _bounded_mapping(value: Any) -> dict[str, Any]:
    """Return checkpoint-safe prompt context when a mapping is available."""

    if not isinstance(value, Mapping):
        return {}
    return dict(value)


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


def _build_remaining_limits(
    definition: SubagentDefinition,
    interactive: InteractiveState,
    *,
    max_committed_calls: int,
) -> dict[str, int]:
    """Return prompt-facing limits that bound this subagent turn."""

    completed_iterations = max(int(interactive.facts.iterations or 0), 0)
    max_iterations = max(int(definition.max_iterations), 1)
    return {
        "completed_iterations": completed_iterations,
        "max_iterations": max_iterations,
        "remaining_iterations": _remaining_iterations(definition, interactive),
        "max_tool_calls_per_iteration": int(definition.max_tool_calls_per_iteration),
        "remaining_tool_calls_this_iteration": int(max_committed_calls),
    }


def _build_previous_tool_context(interactive: InteractiveState) -> dict[str, Any]:
    """Return bounded prior observations for the next model turn."""

    metadata = interactive.facts.safe_metadata
    compact = _bounded_mapping(interactive.facts.last_tool_result_compact)
    transcript = metadata.get(SUBAGENT_OBSERVATION_TRANSCRIPT_KEY)
    if not isinstance(transcript, list) or not transcript:
        return compact
    return {
        "last_tool_result": compact,
        "subagent_observations": list(transcript)[-_MAX_SUBAGENT_OBSERVATIONS:],
    }


def _refresh_subagent_observation_transcript(interactive: InteractiveState) -> None:
    """Append the latest compact synthesis to a bounded in-session transcript."""

    metadata = interactive.facts.ensure_metadata()
    observation = _current_observation(metadata, interactive.facts.selected_tool)
    if observation is None:
        return

    transcript = metadata.get(SUBAGENT_OBSERVATION_TRANSCRIPT_KEY)
    if not isinstance(transcript, list):
        transcript = []
    else:
        transcript = [
            item
            for item in transcript
            if isinstance(item, Mapping)
        ]
    if not transcript or dict(transcript[-1]) != observation:
        transcript.append(observation)
    metadata[SUBAGENT_OBSERVATION_TRANSCRIPT_KEY] = [
        dict(item) for item in transcript[-_MAX_SUBAGENT_OBSERVATIONS:]
    ]


def _current_observation(
    metadata: Mapping[str, Any],
    selected_tool: Any,
) -> dict[str, Any] | None:
    """Return a prompt-safe observation from current compact synthesis metadata."""

    synthesized = _bounded_mapping(metadata.get("synthesized_output"))
    compact = _bounded_mapping(metadata.get("last_tool_result_compact"))
    summary = _clean_text(synthesized.get("summary") or compact.get("summary"))
    findings = _string_list(
        synthesized.get("key_findings") or compact.get("key_findings")
    )[:_MAX_OBSERVATION_FINDINGS]
    if not summary and not findings:
        return None

    tool = _clean_text(
        synthesized.get("tool") or compact.get("tool") or selected_tool
    )
    status = _clean_text(synthesized.get("status") or compact.get("status"))
    observation: dict[str, Any] = {
        "summary": summary,
        "key_findings": findings,
    }
    if tool:
        observation["tool"] = tool
    if status:
        observation["status"] = status
    if "success" in synthesized or "success" in compact:
        observation["success"] = bool(
            synthesized.get("success", compact.get("success"))
        )
    next_actions = _string_list(
        synthesized.get("next_actions") or compact.get("report_recommendations")
    )[:_MAX_OBSERVATION_FINDINGS]
    if next_actions:
        observation["next_actions"] = next_actions
    return observation


def _advance_completed_execution_iteration(
    interactive: InteractiveState,
    subagent: SubagentRuntimeState,
) -> None:
    """Advance iteration budget once for each completed tool batch identity."""

    metadata = interactive.facts.ensure_metadata()
    marker = _completed_execution_marker(metadata, subagent)
    if not marker:
        return

    markers = metadata.get(SUBAGENT_ITERATION_MARKERS_KEY)
    if not isinstance(markers, list):
        markers = []
    normalized_markers = [str(item) for item in markers if str(item).strip()]
    if marker in normalized_markers:
        metadata[SUBAGENT_ITERATION_MARKERS_KEY] = normalized_markers
        return

    normalized_markers.append(marker)
    metadata[SUBAGENT_ITERATION_MARKERS_KEY] = normalized_markers[
        -_MAX_SUBAGENT_OBSERVATIONS:
    ]
    interactive.facts.iterations = max(int(interactive.facts.iterations or 0), 0) + 1


def _completed_execution_marker(
    metadata: Mapping[str, Any],
    subagent: SubagentRuntimeState,
) -> str:
    """Return the stable completed batch/run marker for checkpoint idempotence."""

    action = metadata.get(SUBAGENT_ACTION_METADATA_KEY)
    if isinstance(action, Mapping):
        batch_id = _clean_text(action.get("tool_batch_id"))
        if batch_id:
            return f"{subagent.agent_run_id}:{batch_id}"
    compact_batch = _bounded_mapping(metadata.get("last_tool_result_compact_batch"))
    batch_id = _clean_text(compact_batch.get("tool_batch_id"))
    if batch_id:
        return f"{subagent.agent_run_id}:{batch_id}"
    compact = _bounded_mapping(metadata.get("last_tool_result_compact"))
    execution_id = _clean_text(compact.get("execution_id") or compact.get("id"))
    tool = _clean_text(compact.get("tool"))
    if execution_id:
        return f"{subagent.agent_run_id}:{execution_id}"
    if tool:
        status = _clean_text(compact.get("status"))
        return f"{subagent.agent_run_id}:{tool}:{status}:{_compact_signature(compact)}"
    return ""


def _compact_signature(compact: Mapping[str, Any]) -> str:
    """Return a deterministic compact signature for legacy single-call results."""

    return json.dumps(
        {
            "summary": _clean_text(compact.get("summary")),
            "key_findings": _string_list(compact.get("key_findings")),
            "errors": _string_list(compact.get("errors")),
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def _string_list(value: Any) -> list[str]:
    """Return non-empty strings from a list-like value."""

    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := _clean_text(item))]


def _clean_text(value: Any) -> str:
    """Normalize nullable prompt/result text to a stripped string."""

    if value is None:
        return ""
    return str(value).strip()


__all__ = [
    "SUBAGENT_ACTION_METADATA_KEY",
    "SUBAGENT_EXECUTION_STRATEGY_KEY",
    "SUBAGENT_FORCED_FINAL_METADATA_KEY",
    "SUBAGENT_ITERATION_MARKERS_KEY",
    "SUBAGENT_OBSERVATION_TRANSCRIPT_KEY",
    "SUBAGENT_RESULT_METADATA_KEY",
    "SubagentActionSelectionError",
    "SubagentToolBuilderPromptBuilder",
    "choose_subagent_action",
    "record_subagent_observation_and_budget",
]
