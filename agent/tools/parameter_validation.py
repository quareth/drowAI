"""Shared tool-parameter validation and normalization for planner and runtime.

This module centralizes reusable parameter validation across planner-time
resolution and runtime dispatch. It keeps each tool's ``args_model`` as the
single source of truth, layers shell-specific command policy checks on top of
schema validation, and applies a single target-autofill policy surface that can
be reused without re-implementing tool-specific rules in callers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ValidationError

from .shell.policy import validate_shell_tool_parameters
from .tool_registry import get_tool
from .utils import generate_fix_suggestion, sanitize_for_file_comm
from agent.tool_runtime.backend_tool_policy import resolve_execution_lane


@dataclass(frozen=True, slots=True)
class ToolParameterPolicy:
    """Planner/runtime parameter policy resolved for one tool."""

    autofill_target: bool = False


@dataclass(slots=True)
class ToolParameterValidationResult:
    """Canonical validation result shared by planner and runtime flows."""

    tool_id: str
    valid: bool
    normalized_parameters: Dict[str, Any] = field(default_factory=dict)
    validation_errors: List[Dict[str, str]] = field(default_factory=list)
    reason: Optional[str] = None
    provided_parameters: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    target_autofill_applied: bool = False


def _sanitize_parameters(parameters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(parameters, dict):
        return {}
    return {key: value for key, value in parameters.items() if value is not None}


def _serialize_parameters(parameters: Optional[Dict[str, Any]]) -> str:
    if not isinstance(parameters, dict):
        return ""
    try:
        return json.dumps(parameters, ensure_ascii=True, default=str)[:800]
    except Exception:
        return str(parameters)[:800]


def normalize_validation_errors(raw_errors: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Convert raw Pydantic errors into the canonical validation payload shape."""
    normalized: List[Dict[str, str]] = []
    for err in raw_errors:
        field = ".".join(str(x) for x in err.get("loc", [])) or "arguments"
        message = str(err.get("msg", "Invalid value"))
        normalized.append(
            {
                "field": field,
                "error": message,
                "message": message,
                "suggested_fix": generate_fix_suggestion(err),
            }
        )
    return normalized


def classify_validation_reason(raw_errors: List[Dict[str, Any]]) -> str:
    """Classify Pydantic errors into schema vs semantic buckets."""
    for err in raw_errors:
        err_type = str(err.get("type", ""))
        if err_type.startswith("value_error") or err_type == "assertion_error":
            return "semantic_validation_error"
    return "schema_validation_error"


def _build_error_result(
    *,
    tool_id: str,
    reason: str,
    validation_errors: List[Dict[str, str]],
    provided_parameters: Optional[Dict[str, Any]] = None,
    raw_arguments: str = "",
) -> ToolParameterValidationResult:
    return ToolParameterValidationResult(
        tool_id=tool_id,
        valid=False,
        normalized_parameters={},
        validation_errors=list(validation_errors or []),
        reason=reason,
        provided_parameters=dict(provided_parameters or {}),
        raw_arguments=raw_arguments,
    )


def _direct_transport_error(tool_id: str, parameters: Dict[str, Any]) -> Optional[ToolParameterValidationResult]:
    """Reject direct execution for tools owned by the Kali runtime."""
    transport = str(parameters.get("transport") or "").strip().lower().replace("_", "-")
    if transport != "direct":
        return None
    if resolve_execution_lane(tool_id) != "container_scoped":
        return None
    return _build_error_result(
        tool_id=tool_id,
        reason="transport_policy_violation",
        validation_errors=[
            {
                "field": "transport",
                "error": "Container-scoped tools cannot use transport=direct",
                "message": (
                    "Container-scoped tools must execute inside the active Kali runtime "
                    "using file-comm or pty transport."
                ),
                "suggested_fix": "Remove transport to auto-select, or use transport='file-comm' or transport='pty'.",
            }
        ],
        provided_parameters=parameters,
        raw_arguments=_serialize_parameters(parameters),
    )


def resolve_tool_parameter_policy(
    tool_id: str,
    *,
    validation_stage: Literal["planner", "execution"] = "execution",
    get_tool_fn: Callable[[str], Any] = get_tool,
) -> ToolParameterPolicy:
    """Resolve the centralized parameter policy for one tool."""
    try:
        tool_cls = get_tool_fn(tool_id)
    except Exception:
        return ToolParameterPolicy()

    override = getattr(tool_cls, "parameter_validation_policy", None)
    if isinstance(override, ToolParameterPolicy):
        return override
    if isinstance(override, dict):
        try:
            return ToolParameterPolicy(**override)
        except Exception:
            pass

    schema_model = None
    if validation_stage == "planner":
        schema_model = getattr(tool_cls, "planner_args_model", None) or getattr(tool_cls, "args_model", None)
    else:
        schema_model = getattr(tool_cls, "args_model", None)
    if schema_model is None:
        return ToolParameterPolicy()

    try:
        schema = schema_model.model_json_schema()
    except Exception:
        return ToolParameterPolicy()

    properties = schema.get("properties", {})
    supports_target = isinstance(properties, dict) and "target" in properties
    return ToolParameterPolicy(autofill_target=bool(supports_target))


def _validation_result_from_error(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    exc: Exception,
    fallback_reason: str,
) -> ToolParameterValidationResult:
    if isinstance(exc, ValidationError):
        raw_errors = exc.errors()
        return _build_error_result(
            tool_id=tool_id,
            reason=classify_validation_reason(raw_errors),
            validation_errors=normalize_validation_errors(raw_errors),
            provided_parameters=parameters,
            raw_arguments=_serialize_parameters(parameters),
        )

    message = str(exc).strip() or "Invalid arguments"
    return _build_error_result(
        tool_id=tool_id,
        reason="semantic_validation_error" if isinstance(exc, ValueError) else fallback_reason,
        validation_errors=[
            {
                "field": "arguments",
                "error": message,
                "message": message,
                "suggested_fix": f"Provide valid arguments for {tool_id}",
            }
        ],
        provided_parameters=parameters,
        raw_arguments=_serialize_parameters(parameters),
    )


def _validate_execution_candidate(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    get_tool_fn: Callable[[str], Any],
    max_shell_command_chars: int,
    metric_hook: Optional[Callable[[str], None]],
    logger: Any,
) -> ToolParameterValidationResult:
    transport_error = _direct_transport_error(tool_id, parameters)
    if transport_error is not None:
        return transport_error

    try:
        tool_cls = get_tool_fn(tool_id)
        args_model = getattr(tool_cls, "args_model", None)
        if args_model is None:
            return _build_error_result(
                tool_id=tool_id,
                reason="schema_validation_error",
                validation_errors=[
                    {
                        "field": "arguments",
                        "error": "Tool schema unavailable",
                        "message": "Tool schema unavailable",
                        "suggested_fix": f"Ensure {tool_id} declares a valid args_model",
                    }
                ],
                provided_parameters=parameters,
                raw_arguments=_serialize_parameters(parameters),
            )

        args = args_model(**dict(parameters))

        shell_validation_errors = validate_shell_tool_parameters(
            tool_id,
            parameters,
            get_tool_fn=get_tool_fn,
            generate_fix_suggestion_fn=generate_fix_suggestion,
            max_command_chars=max_shell_command_chars,
            metric_hook=metric_hook,
            logger=logger,
        )
        if shell_validation_errors:
            return _build_error_result(
                tool_id=tool_id,
                reason="semantic_validation_error",
                validation_errors=shell_validation_errors,
                provided_parameters=parameters,
                raw_arguments=_serialize_parameters(parameters),
            )

        return ToolParameterValidationResult(
            tool_id=tool_id,
            valid=True,
            normalized_parameters=sanitize_for_file_comm(args.model_dump(exclude_none=True)),
            validation_errors=[],
            reason=None,
            provided_parameters=dict(parameters),
            raw_arguments=_serialize_parameters(parameters),
        )
    except Exception as exc:
        return _validation_result_from_error(
            tool_id=tool_id,
            parameters=parameters,
            exc=exc,
            fallback_reason="schema_validation_error",
        )


def _validate_planner_candidate(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    action_target: Optional[str],
    get_tool_fn: Callable[[str], Any],
    max_shell_command_chars: int,
    metric_hook: Optional[Callable[[str], None]],
    logger: Any,
) -> ToolParameterValidationResult:
    transport_error = _direct_transport_error(tool_id, parameters)
    if transport_error is not None:
        return transport_error

    try:
        tool_cls = get_tool_fn(tool_id)
        planner_model = getattr(tool_cls, "planner_args_model", None) or getattr(tool_cls, "args_model", None)
        if planner_model is None:
            return _build_error_result(
                tool_id=tool_id,
                reason="schema_validation_error",
                validation_errors=[
                    {
                        "field": "arguments",
                        "error": "Planner schema unavailable",
                        "message": "Planner schema unavailable",
                        "suggested_fix": f"Ensure {tool_id} declares a valid planner or execution schema",
                    }
                ],
                provided_parameters=parameters,
                raw_arguments=_serialize_parameters(parameters),
            )

        planner_args = planner_model(**dict(parameters))

        compile_fn = getattr(tool_cls, "compile_planner_parameters", None)
        if callable(compile_fn):
            compiled_parameters = compile_fn(planner_args, action_target=action_target)
        elif isinstance(planner_args, BaseModel):
            compiled_parameters = planner_args.model_dump(exclude_none=True)
        else:
            compiled_parameters = dict(planner_args or {})

        if not isinstance(compiled_parameters, dict):
            return _build_error_result(
                tool_id=tool_id,
                reason="schema_validation_error",
                validation_errors=[
                    {
                        "field": "arguments",
                        "error": "Compiled planner parameters must be a JSON object",
                        "message": "Compiled planner parameters must be a JSON object",
                        "suggested_fix": f"Ensure {tool_id} compiles planner parameters to a JSON object",
                    }
                ],
                provided_parameters=parameters,
                raw_arguments=_serialize_parameters(parameters),
            )

        return _validate_execution_candidate(
            tool_id=tool_id,
            parameters=_sanitize_parameters(compiled_parameters),
            get_tool_fn=get_tool_fn,
            max_shell_command_chars=max_shell_command_chars,
            metric_hook=metric_hook,
            logger=logger,
        )
    except Exception as exc:
        return _validation_result_from_error(
            tool_id=tool_id,
            parameters=parameters,
            exc=exc,
            fallback_reason="schema_validation_error",
        )


def validate_tool_parameters(
    tool_id: str,
    raw_parameters: Optional[Dict[str, Any]],
    *,
    validation_stage: Literal["planner", "execution"] = "execution",
    action_target: Optional[str] = None,
    parse_error: Optional[str] = None,
    raw_arguments: str = "",
    get_tool_fn: Callable[[str], Any] = get_tool,
    max_shell_command_chars: int = 320,
    metric_hook: Optional[Callable[[str], None]] = None,
    logger: Any = None,
) -> ToolParameterValidationResult:
    """Validate and normalize one tool parameter payload."""
    provided_parameters = _sanitize_parameters(raw_parameters)
    serialized_arguments = raw_arguments or _serialize_parameters(raw_parameters)

    if parse_error:
        return _build_error_result(
            tool_id=tool_id,
            reason="parse_error",
            validation_errors=[
                {
                    "field": "arguments",
                    "error": parse_error,
                    "message": parse_error,
                    "suggested_fix": "Return one JSON object of arguments for the selected tool.",
                }
            ],
            provided_parameters=provided_parameters,
            raw_arguments=serialized_arguments,
        )

    if raw_parameters is None:
        return _build_error_result(
            tool_id=tool_id,
            reason="missing_tool_call",
            validation_errors=[
                {
                    "field": "arguments",
                    "error": "Missing tool call for selected tool",
                    "message": "Planner selected this tool but did not emit a function call.",
                    "suggested_fix": "Call the selected tool exactly once with a JSON object of arguments.",
                }
            ],
            provided_parameters={},
            raw_arguments=serialized_arguments,
        )

    if not isinstance(raw_parameters, dict):
        return _build_error_result(
            tool_id=tool_id,
            reason="parse_error",
            validation_errors=[
                {
                    "field": "arguments",
                    "error": "Function arguments must be a JSON object",
                    "message": "Function arguments must be a JSON object",
                    "suggested_fix": "Return one JSON object of arguments for the selected tool.",
                }
            ],
            provided_parameters={},
            raw_arguments=serialized_arguments or str(raw_parameters)[:800],
        )

    policy = resolve_tool_parameter_policy(
        tool_id,
        validation_stage=validation_stage,
        get_tool_fn=get_tool_fn,
    )
    trimmed_target = str(action_target or "").strip()
    should_try_target_autofill = (
        policy.autofill_target
        and bool(trimmed_target)
        and not str(provided_parameters.get("target", "") or "").strip()
    )

    def _attempt(parameters: Dict[str, Any]) -> ToolParameterValidationResult:
        if validation_stage == "planner":
            return _validate_planner_candidate(
                tool_id=tool_id,
                parameters=parameters,
                action_target=action_target,
                get_tool_fn=get_tool_fn,
                max_shell_command_chars=max_shell_command_chars,
                metric_hook=metric_hook,
                logger=logger,
            )
        return _validate_execution_candidate(
            tool_id=tool_id,
            parameters=parameters,
            get_tool_fn=get_tool_fn,
            max_shell_command_chars=max_shell_command_chars,
            metric_hook=metric_hook,
            logger=logger,
        )

    if should_try_target_autofill:
        candidate_parameters = dict(provided_parameters)
        candidate_parameters["target"] = trimmed_target
        candidate_result = _attempt(candidate_parameters)
        if candidate_result.valid:
            candidate_result.provided_parameters = dict(provided_parameters)
            candidate_result.raw_arguments = serialized_arguments
            candidate_result.target_autofill_applied = True
            return candidate_result

        raw_result = _attempt(provided_parameters)
        if raw_result.valid:
            raw_result.provided_parameters = dict(provided_parameters)
            raw_result.raw_arguments = serialized_arguments
            return raw_result

        candidate_result.provided_parameters = dict(provided_parameters)
        candidate_result.raw_arguments = serialized_arguments
        return candidate_result

    result = _attempt(provided_parameters)
    result.provided_parameters = dict(provided_parameters)
    result.raw_arguments = serialized_arguments
    return result


def validation_result_from_exception(
    tool_id: str,
    exc: Exception,
    *,
    raw_parameters: Optional[Dict[str, Any]] = None,
) -> Optional[ToolParameterValidationResult]:
    """Convert known validation exceptions into the canonical result shape."""
    provided_parameters = _sanitize_parameters(raw_parameters)
    serialized_arguments = _serialize_parameters(raw_parameters)

    if isinstance(exc, ValidationError):
        raw_errors = exc.errors()
        return _build_error_result(
            tool_id=tool_id,
            reason=classify_validation_reason(raw_errors),
            validation_errors=normalize_validation_errors(raw_errors),
            provided_parameters=provided_parameters,
            raw_arguments=serialized_arguments,
        )

    if isinstance(exc, ValueError):
        message = str(exc).strip() or "Invalid arguments"
        return _build_error_result(
            tool_id=tool_id,
            reason="semantic_validation_error",
            validation_errors=[
                {
                    "field": "arguments",
                    "error": message,
                    "message": message,
                    "suggested_fix": f"Provide valid arguments for {tool_id}",
                }
            ],
            provided_parameters=provided_parameters,
            raw_arguments=serialized_arguments,
        )

    return None


__all__ = [
    "ToolParameterPolicy",
    "ToolParameterValidationResult",
    "classify_validation_reason",
    "normalize_validation_errors",
    "resolve_tool_parameter_policy",
    "validate_tool_parameters",
    "validation_result_from_exception",
]
