"""Scout native tool-batch builder for the recon subagent pilot.

The node binds Scout's complete bounded tool profile in one provider-native
builder call. It reuses selector-independent guidance from the canonical tool
parameter builder, validates one-to-many returned calls, and writes the shared
``ToolBatch`` contract consumed by the existing execution runtime.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from agent.config import AgentConfig
from agent.execution_strategy import ExecutionStrategy
from agent.graph.emission.reasoning_section import reasoning_section
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.node_utils import _usage_to_dict
from agent.graph.state import InteractiveState
from agent.graph.utils.llm_resolver import resolve_llm_client
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.providers.llm.core.base import ToolCallResult
from agent.reasoning.llm_parameter_resolution import (
    NATIVE_BUILDER_MAX_OUTPUT_TOKENS,
)
from agent.subagents.scout.state import (
    ScoutRuntimeState,
    scout_state_from_graph_state,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tools.builder_intent import split_builder_intent
from agent.tools.tool_call_specs import build_function_tool_specs_for
from agent.tools.tool_registry import get_tool
from core.llm import (
    LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
    wait_for_with_timeout,
)
from core.prompts.builders.scout_tool_builder import ScoutToolBuilderPromptBuilder


logger = logging.getLogger(__name__)

SCOUT_ACTION_METADATA_KEY = "scout_action"
SCOUT_RESULT_METADATA_KEY = "scout_result"
SCOUT_EXECUTION_STRATEGY_KEY = "_execution_strategy"

_SCOUT_EXECUTION_STRATEGY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["parallel", "sequential"],
    "description": (
        "Batch scheduling metadata shared by every native call in this response. "
        "Not a tool parameter."
    ),
}


class ScoutActionSelectionError(ValueError):
    """Raised when Scout cannot produce a safe canonical tool batch."""


async def choose_scout_action(
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
    writer: Any = None,
) -> dict[str, Any]:
    """Build one bounded Scout tool batch from all visible Scout tools."""

    interactive = InteractiveState.from_mapping(state)
    scout = scout_state_from_graph_state(interactive)
    if not scout.tool_profile.tools:
        raise ScoutActionSelectionError("Scout tool profile is empty")

    max_committed_calls = AgentConfig().max_committed_tools_per_batch
    tool_specs, function_to_tool_id = _build_scout_function_specs(scout)
    prompt_builder = ScoutToolBuilderPromptBuilder()
    llm_client = resolve_llm_client(
        interactive.facts.ensure_metadata(),
        context,
        config=config,
        role="reasoning_main",
    )
    async with reasoning_section(
        writer,
        state=interactive,
        step="scout_action_selection",
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
                    assignment=scout.assignment.model_dump(mode="json"),
                    tool_ids=scout.tool_profile.tool_ids,
                    working_memory=_bounded_mapping(
                        interactive.facts.safe_metadata.get("working_memory")
                    ),
                    previous_tool_summary=_bounded_mapping(
                        interactive.facts.last_tool_result_compact
                    ),
                ),
                tools=tool_specs,
                tool_choice="required",
                parallel_tool_calls=True,
                temperature=0.1,
                max_tokens=NATIVE_BUILDER_MAX_OUTPUT_TOKENS,
            ),
            timeout_sec=LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
            component="SCOUT",
            operation="native_tool_builder_llm_call",
            logger=logger,
            task_id=interactive.facts.task_id,
            outcome="native_tool_builder_timeout",
        )

        _append_usage(interactive, result)
        batch = _build_tool_batch_from_result(
            result,
            scout=scout,
            function_to_tool_id=function_to_tool_id,
            max_committed_calls=max_committed_calls,
        )
        if emitter is not None:
            emitter.emit_reasoning_delta(batch.selection_rationale)
    return _apply_recon_tool_batch(interactive, scout, batch)


def _build_scout_function_specs(
    scout: ScoutRuntimeState,
) -> tuple[list[FunctionToolSpec], dict[str, str]]:
    """Return every bounded Scout tool with scheduling metadata injected."""

    tool_ids = list(scout.tool_profile.tool_ids)
    if len(tool_ids) != len(set(tool_ids)):
        raise ScoutActionSelectionError(
            "Scout tool profile contains duplicate tool ids"
        )
    specs, mapping = build_function_tool_specs_for(tool_ids)
    return [_inject_execution_strategy(spec) for spec in specs], mapping


def _inject_execution_strategy(spec: FunctionToolSpec) -> FunctionToolSpec:
    """Add Scout-only batch scheduling metadata to one function schema."""

    schema = deepcopy(spec.parameters_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ScoutActionSelectionError(
            f"Scout tool {spec.tool_id!r} does not expose an object schema"
        )
    properties[SCOUT_EXECUTION_STRATEGY_KEY] = dict(
        _SCOUT_EXECUTION_STRATEGY_SCHEMA
    )
    required = schema.get("required")
    if not isinstance(required, list):
        required = []
        schema["required"] = required
    if SCOUT_EXECUTION_STRATEGY_KEY not in required:
        required.append(SCOUT_EXECUTION_STRATEGY_KEY)
    return FunctionToolSpec(
        tool_id=spec.tool_id,
        name=spec.name,
        description=spec.description,
        parameters_schema=schema,
    )


def _build_tool_batch_from_result(
    result: ToolCallResult,
    *,
    scout: ScoutRuntimeState,
    function_to_tool_id: Mapping[str, str],
    max_committed_calls: int,
) -> ToolBatch:
    """Parse, validate, and order native calls as one canonical batch."""

    native_calls = list(result.tool_calls or [])
    if not native_calls:
        raise ScoutActionSelectionError(
            "Scout native tool builder returned no calls"
        )
    if len(native_calls) > max_committed_calls:
        raise ScoutActionSelectionError(
            "Scout native tool builder exceeded the committed call cap: "
            f"{len(native_calls)} > {max_committed_calls}"
        )

    batch_strategy: ExecutionStrategy | None = None
    committed_calls: list[ToolCall] = []
    for native_call in native_calls:
        function_name, raw_arguments = _call_name_and_arguments(native_call)
        tool_id = function_to_tool_id.get(function_name)
        if tool_id is None:
            raise ScoutActionSelectionError(
                f"Scout selected unbound function {function_name!r}"
            )
        if tool_id not in scout.tool_profile.tool_ids:
            raise ScoutActionSelectionError(
                f"Scout tool {tool_id!r} is not allowlisted"
            )

        raw_parameters = _parse_arguments(raw_arguments)
        strategy = _pop_execution_strategy(raw_parameters)
        if batch_strategy is None:
            batch_strategy = strategy
        elif strategy is not batch_strategy:
            raise ScoutActionSelectionError(
                "Scout native calls declared inconsistent execution strategies"
            )

        parameters_without_intent, builder_intent = split_builder_intent(
            raw_parameters
        )
        if not isinstance(parameters_without_intent, dict):
            raise ScoutActionSelectionError(
                "Scout recon tool arguments must decode to a JSON object"
            )
        parameters = _validate_registered_tool_parameters(
            tool_id,
            parameters_without_intent,
        )
        committed_calls.append(
            ToolCall(
                tool_call_id=f"scout-call-{uuid.uuid4().hex}",
                tool_id=tool_id,
                parameters=parameters,
                intent=builder_intent.strip(),
            )
        )

    if batch_strategy is None:
        raise ScoutActionSelectionError(
            "Scout native tool builder omitted execution strategy"
        )
    if len(committed_calls) == 1:
        batch_strategy = ExecutionStrategy.SEQUENTIAL

    rationale = "; ".join(call.intent for call in committed_calls if call.intent)
    return ToolBatch(
        tool_batch_id=f"scout-batch-{uuid.uuid4().hex}",
        tool_calls=tuple(committed_calls),
        requested_execution_strategy=batch_strategy,
        selection_rationale=rationale or "Scout committed bounded recon calls.",
    )


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
        raise ScoutActionSelectionError(
            "Scout native call is missing a function name"
        )
    return name.strip(), arguments


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    """Decode one provider-native JSON arguments object."""

    if isinstance(raw_arguments, str):
        if not raw_arguments.strip():
            raise ScoutActionSelectionError(
                "Scout native call arguments are empty"
            )
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ScoutActionSelectionError(
                f"Scout native call arguments are not valid JSON: {exc}"
            ) from exc
    elif isinstance(raw_arguments, Mapping):
        decoded = dict(raw_arguments)
    else:
        raise ScoutActionSelectionError(
            "Scout native call arguments must be a JSON object"
        )
    if not isinstance(decoded, dict):
        raise ScoutActionSelectionError(
            "Scout native call arguments must decode to a JSON object"
        )
    return decoded


def _pop_execution_strategy(
    raw_parameters: dict[str, Any],
) -> ExecutionStrategy:
    """Strip and validate Scout batch scheduling metadata."""

    raw_strategy = raw_parameters.pop(SCOUT_EXECUTION_STRATEGY_KEY, None)
    normalized = str(raw_strategy or "").strip().lower()
    if normalized == ExecutionStrategy.PARALLEL.value:
        return ExecutionStrategy.PARALLEL
    if normalized == ExecutionStrategy.SEQUENTIAL.value:
        return ExecutionStrategy.SEQUENTIAL
    raise ScoutActionSelectionError(
        "Scout native call must declare _execution_strategy as "
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
        raise ScoutActionSelectionError(
            f"Scout selected unsupported parameters for {tool_id}: {unknown_fields}"
        )
    try:
        validated = schema_model.model_validate(dict(parameters))
    except Exception as exc:
        raise ScoutActionSelectionError(
            f"Scout selected invalid parameters for {tool_id}: {exc}"
        ) from exc
    dumped = validated.model_dump(mode="json")
    return dumped if isinstance(dumped, dict) else dict(parameters)


def _apply_recon_tool_batch(
    interactive: InteractiveState,
    scout: ScoutRuntimeState,
    batch: ToolBatch,
) -> dict[str, Any]:
    """Project the canonical native-call batch into shared runtime metadata."""

    committed_tool_ids = [call.tool_id for call in batch.tool_calls]
    plan_data = {
        "selected_tools": committed_tool_ids,
        "candidate_tools": list(scout.tool_profile.tool_ids),
        "execution_strategy": batch.requested_execution_strategy.value,
        "reasoning": batch.selection_rationale,
        "expected_outcome": batch.selection_rationale,
        "tool_batch": _serialize_tool_batch(batch),
    }
    first_call = batch.tool_calls[0]
    metadata = interactive.facts.ensure_metadata()
    metadata.pop(SCOUT_RESULT_METADATA_KEY, None)
    metadata["planner_plan"] = plan_data
    metadata["tool_plan_prepared"] = True
    metadata["planned_execution_strategy"] = (
        batch.requested_execution_strategy.value
    )
    metadata[SCOUT_ACTION_METADATA_KEY] = {
        "route": "tool",
        "agent_run_id": scout.agent_run_id,
        "agent_id": scout.agent_id,
        "tool_id": first_call.tool_id,
        "tool_ids": committed_tool_ids,
        "tool_batch_id": batch.tool_batch_id,
    }
    interactive.facts.tool_ids = committed_tool_ids
    interactive.facts.tool_candidates = list(scout.tool_profile.tool_ids)
    interactive.facts.selected_tool = first_call.tool_id
    interactive.facts.tool_parameters = dict(first_call.parameters)
    interactive.trace.reasoning.append(
        "Scout committed "
        f"{len(batch.tool_calls)} recon tool call(s) "
        f"for {batch.requested_execution_strategy.value} execution."
    )
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


def _append_usage(
    interactive: InteractiveState,
    result: ToolCallResult,
) -> None:
    """Append the single Scout builder call to normal usage accounting."""

    usage = _usage_to_dict(
        result.usage,
        "scout_tool_builder",
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


__all__ = [
    "SCOUT_ACTION_METADATA_KEY",
    "SCOUT_EXECUTION_STRATEGY_KEY",
    "SCOUT_RESULT_METADATA_KEY",
    "ScoutActionSelectionError",
    "choose_scout_action",
]
