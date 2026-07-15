"""Tool/function spec builders for registered tools.

Transforms planner-facing Pydantic schemas into provider-neutral function tool
contracts and keeps OpenAI-compatible wrappers available for legacy callers.
Exposure stays minimal by building specs only for a small, resolved set of tool
IDs.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Tuple

from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.providers.llm.adapters.openai.tool_contracts import (
    build_openai_chat_tool_spec_from_function_spec,
)
from .builder_intent import inject_builder_intent_property
from .tool_registry import get_tool


def _sanitize_default(value: Any) -> Any:
    """Convert non-JSON-serializable defaults (e.g., Enums) to strings where possible."""
    try:
        import enum  # local import to avoid hard dependency elsewhere
        if isinstance(value, enum.Enum):
            return value.value
    except Exception:
        pass

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    # Fallback to string representation for complex types
    try:
        return str(value)
    except Exception:
        return None


def _sanitize_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Pydantic JSON schema for OpenAI tool parameters.

    - Ensures enum values are JSON-serializable
    - Converts non-primitive defaults to strings
    """
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            new_node: Dict[str, Any] = {}
            for k, v in node.items():
                if k == "default":
                    new_node[k] = _sanitize_default(v)
                elif k == "enum":
                    new_node[k] = [
                        _sanitize_default(x) if not isinstance(x, (str, int, float, bool)) else x
                        for x in (v or [])
                    ]
                else:
                    new_node[k] = walk(v)
            return new_node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def make_function_name_for_tool(tool_id: str) -> str:
    """Return the deterministic provider-facing function name for a tool id."""
    return f"tool__{tool_id.replace('.', '_')}"


def build_function_tool_spec_for(tool_id: str) -> FunctionToolSpec:
    """Create a provider-neutral function tool spec for a registered tool."""
    tool_cls = get_tool(tool_id)
    schema_model = tool_cls.get_planner_args_model()
    schema = schema_model.model_json_schema() if schema_model is not None else {"type": "object", "properties": {}}
    parameters = _sanitize_schema(schema if isinstance(schema, dict) else {"type": "object", "properties": {}})
    parameters = inject_builder_intent_property(parameters)

    description = inspect.getdoc(tool_cls) or f"Execute tool {tool_id}"
    planner_guidance = tool_cls.get_planner_guidance()
    if planner_guidance:
        description = f"{description}\n\nPlanner Guidance:\n{planner_guidance}"

    return FunctionToolSpec(
        tool_id=tool_id,
        name=make_function_name_for_tool(tool_id),
        description=description,
        parameters_schema=parameters,
    )


def build_openai_tool_spec_from_function_spec(spec: FunctionToolSpec) -> Dict[str, Any]:
    """Translate a neutral function tool spec to the OpenAI Chat tool wrapper."""
    return build_openai_chat_tool_spec_from_function_spec(spec)


def build_openai_tool_spec_for(tool_id: str) -> Dict[str, Any]:
    """Create an OpenAI tool spec for a single registered tool."""
    return build_openai_tool_spec_from_function_spec(
        build_function_tool_spec_for(tool_id)
    )


def build_function_tool_specs_for(tool_ids: List[str]) -> Tuple[List[FunctionToolSpec], Dict[str, str]]:
    """Build neutral tool specs and a function-name to tool-id map."""
    specs: List[FunctionToolSpec] = []
    mapping: Dict[str, str] = {}
    seen: set[str] = set()
    for tool_id in tool_ids:
        if tool_id in seen:
            continue
        seen.add(tool_id)
        spec = build_function_tool_spec_for(tool_id)
        specs.append(spec)
        mapping[spec.name] = tool_id
    return specs, mapping


def build_openai_tool_specs_for(tool_ids: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Build OpenAI tool specs for a list of tool ids.

    Returns
    -------
    specs: list
        List of tool specs suitable for OpenAI chat.completions tools parameter
    fn_to_tool_id: dict
        Mapping from function name -> tool_id for reverse lookup on tool_calls
    """
    neutral_specs, mapping = build_function_tool_specs_for(tool_ids)
    specs = [
        build_openai_tool_spec_from_function_spec(spec)
        for spec in neutral_specs
    ]
    return specs, mapping


def build_get_tool_results_function_spec() -> FunctionToolSpec:
    """Neutral spec for a read-only function that retrieves prior tool outputs."""
    return FunctionToolSpec(
        tool_id="get_tool_results",
        name="get_tool_results",
        description="Retrieve previous tool outputs and summaries without re-executing tools.",
        parameters_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "Filter by tool id/name"},
                "target": {"type": "string", "description": "Filter by target"},
                "since": {"type": "string", "description": "ISO timestamp lower bound"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    )


def build_get_tool_results_spec() -> Dict[str, Any]:
    """OpenAI wrapper for the read-only prior tool-output retrieval function."""
    return build_openai_tool_spec_from_function_spec(
        build_get_tool_results_function_spec()
    )


__all__ = [
    "build_function_tool_spec_for",
    "build_function_tool_specs_for",
    "build_get_tool_results_function_spec",
    "build_get_tool_results_spec",
    "build_openai_tool_spec_for",
    "build_openai_tool_spec_from_function_spec",
    "build_openai_tool_specs_for",
    "make_function_name_for_tool",
]
