"""LLM-based parameter resolution for the enhanced planner.

Handles the second LLM call in the planning pipeline: given candidate tools,
the builder commits final executable calls via native provider tool calls
while receiving only the candidate function schemas. It validates generated
parameters through the shared tool-validator and performs one repair round
before surfacing a canonical planner validation error.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.llm import wait_for_with_timeout

from agent.models import Action
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
)
from agent.providers.llm.profiles.registry import require_model_profile
from agent.reasoning.batch_envelope import project_validated_envelope_from_calls
from agent.reasoning.structured_contract_recovery import StructuredContractViolationError
from agent.tools.builder_intent import split_builder_intent
from agent.tools.parameter_generator import ContextualParameterGenerator
from agent.tools.parameter_validation import (
    ToolParameterValidationResult,
    validate_tool_parameters,
)

# GPT-5 Responses API shares max_output_tokens between reasoning and tool calls;
# 500 was too small for required native tool_choice builder calls (incomplete
# responses with reasoning-only output).
NATIVE_BUILDER_MAX_OUTPUT_TOKENS = 5000


class PlannerToolParameterValidationError(StructuredContractViolationError):
    """Raised when planner-generated tool parameters are invalid after repair."""

    def __init__(
        self,
        *,
        tool_id: str,
        reason: str,
        validation_errors: List[Dict[str, str]],
        raw_arguments: str = "",
        provided_parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tool_id = tool_id
        self.reason = reason
        self.validation_errors = list(validation_errors or [])
        self.raw_arguments = (raw_arguments or "")[:800]
        self.provided_parameters = dict(provided_parameters or {})
        contract_error_code = (
            "structured_contract_schema_validation"
            if reason in {"missing_tool_call", "schema_validation_error", "parse_error"}
            else "structured_contract_semantic_validation"
        )
        super().__init__(
            error_code=contract_error_code,
            stage="param_resolver",
            contract="tool_parameter_contract",
            kind=(
                "schema_validation_error"
                    if contract_error_code == "structured_contract_schema_validation"
                    else "semantic_validation_error"
                ),
            details=f"Planner parameter validation failed for {tool_id}: {reason}",
            retryable=True,
            diagnostics={
                "tool_id": tool_id,
                "reason": reason,
                "validation_errors": self.validation_errors,
                "raw_arguments": self.raw_arguments,
            },
        )


@dataclass(frozen=True)
class OrderedBuilderCall:
    """Provider-order native tool call normalized for internal validation."""

    tool_id: str
    parameters: Dict[str, Any]
    raw_arguments: str
    provider_call_id: Optional[str] = None
    intent: str = ""


@dataclass
class ParameterResolutionResult:
    """Resolved parameter outputs for selected tools.

    ``builder_envelope`` carries the internal compatibility envelope
    projected from native builder tool calls so the planner can feed it to
    ``batch_commit.commit_tool_batch`` for id minting and structural
    validation.
    """

    tool_parameters: Dict[str, Dict[str, Any]]
    llm_tool_parameters: Dict[str, Dict[str, Any]]
    usage_records: List[Dict[str, Any]]
    builder_envelope: Optional[Dict[str, Any]] = None
    validated_builder_envelope: Optional[Dict[str, Any]] = None


def _convert_usage_to_dict(usage: Any, source: str) -> Optional[Dict[str, Any]]:
    """Convert provider usage payload into planner-compatible usage record."""
    if usage is None:
        return None
    if hasattr(usage, "to_dict") and callable(usage.to_dict):
        try:
            result = usage.to_dict(source)
            if isinstance(result, dict):
                result["request_mode"] = "non_streaming"
            return result
        except Exception:
            pass
    if isinstance(usage, dict):
        result = {
            "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
            "completion_tokens": usage.get("completion_tokens", 0) or 0,
            "total_tokens": usage.get("total_tokens", 0) or 0,
            "model": usage.get("model", "unknown"),
            "provider": usage.get("provider", "openai"),
            "cached_tokens": usage.get("cached_tokens", 0) or 0,
            "reasoning_tokens": usage.get("reasoning_tokens", 0) or 0,
            "api_surface": usage.get("api_surface", "unknown"),
            "cache_reporting": usage.get("cache_reporting", "unknown"),
            "request_mode": "non_streaming",
            "source": source,
        }
        components = _provider_usage_components_to_dict(
            usage.get("provider_usage_components")
        )
        if components is not None:
            result["provider_usage_components"] = components
        return result
    if hasattr(usage, "prompt_tokens"):
        result = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "model": getattr(usage, "model", "unknown"),
            "provider": getattr(usage, "provider", "openai"),
            "cached_tokens": getattr(usage, "cached_tokens", 0) or 0,
            "reasoning_tokens": getattr(usage, "reasoning_tokens", 0) or 0,
            "api_surface": getattr(usage, "api_surface", "unknown"),
            "cache_reporting": getattr(usage, "cache_reporting", "unknown"),
            "request_mode": "non_streaming",
            "source": source,
        }
        components = _provider_usage_components_to_dict(
            getattr(usage, "provider_usage_components", None)
        )
        if components is not None:
            result["provider_usage_components"] = components
        return result
    return None


def _provider_usage_components_to_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Return canonical provider usage components when present."""
    if value is None:
        return None
    if isinstance(value, dict):
        provider = value.get("provider")
        api_surface = value.get("api_surface")
        components = value.get("components")
        if (
            isinstance(provider, str)
            and isinstance(api_surface, str)
            and isinstance(components, dict)
        ):
            return {
                "provider": provider,
                "api_surface": api_surface,
                "components": dict(components),
            }
        return None
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        result = to_dict()
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def _safe_obj_value(value: Any, key: str, default: Any = None) -> Any:
    """Read a key from dict-like or SDK objects without raising."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _native_call_summaries(calls: List[Any]) -> List[Dict[str, Any]]:
    """Return native call diagnostics without exposing argument values."""
    summaries: List[Dict[str, Any]] = []
    for call in calls:
        raw_arguments = _safe_obj_value(call, "arguments")
        summaries.append(
            {
                "name": _safe_obj_value(call, "name"),
                "id_present": bool(_safe_obj_value(call, "id")),
                "arguments_chars": len(raw_arguments) if isinstance(raw_arguments, str) else None,
            }
        )
    return summaries


def _resolve_client_provider_model_ref(llm_client: Any) -> Optional[ProviderModelRef]:
    """Best-effort provider/model identity for capability-gated request options."""
    for attr in ("provider_model_ref", "_provider_model_ref"):
        value = getattr(llm_client, attr, None)
        if isinstance(value, ProviderModelRef):
            return value.normalized()

    model = getattr(llm_client, "model", None)
    if not isinstance(model, str) or not model.strip():
        return None

    provider = (
        getattr(llm_client, "provider", None)
        or getattr(llm_client, "provider_id", None)
        or getattr(llm_client, "_provider", None)
    )
    if isinstance(provider, str) and provider.strip():
        return ProviderModelRef(provider, model).normalized()

    for candidate_provider in (OPENAI_PROVIDER_ID, ANTHROPIC_PROVIDER_ID):
        ref = ProviderModelRef(candidate_provider, model)
        try:
            require_model_profile(ref)
        except LLMProfileNotFoundError:
            continue
        return ref.normalized()
    return None


class LLMParameterResolver:
    """Run and validate the planner's parameter-generation step."""

    def __init__(
        self,
        llm_client: Any,
        config: Any,
        parameter_generator: ContextualParameterGenerator,
        logger: Optional[logging.Logger],
    ) -> None:
        self.llm_client = llm_client
        self.config = config
        self.parameter_generator = parameter_generator
        self._log = logger

    def parse_ordered_tool_calls(
        self,
        raw_calls: Any,
        fn_map: Dict[str, str],
    ) -> Tuple[List[OrderedBuilderCall], Dict[str, Dict[str, str]]]:
        """Parse raw provider tool_calls while preserving provider order."""
        ordered: List[OrderedBuilderCall] = []
        parse_errors: Dict[str, Dict[str, str]] = {}
        for call in raw_calls or []:
            fn_name = call.get("function", {}).get("name")
            if not fn_name:
                continue
            tool_id = fn_map.get(fn_name)
            if not tool_id:
                continue
            args_raw_obj = call.get("function", {}).get("arguments")
            args_raw = args_raw_obj if isinstance(args_raw_obj, str) else ""
            if args_raw_obj is None or args_raw_obj == "":
                parse_errors.setdefault(
                    tool_id,
                    {
                        "message": "Missing function arguments payload",
                        "raw_arguments": "",
                    },
                )
                continue

            if isinstance(args_raw_obj, dict):
                provided = args_raw_obj
                args_raw = json.dumps(args_raw_obj, ensure_ascii=True, default=str)
            else:
                try:
                    provided = json.loads(args_raw)
                except Exception as exc:
                    parse_errors.setdefault(
                        tool_id,
                        {
                            "message": f"Invalid JSON arguments: {str(exc)}",
                            "raw_arguments": args_raw[:800],
                        },
                    )
                    continue

            if not isinstance(provided, dict):
                parse_errors.setdefault(
                    tool_id,
                    {
                        "message": "Function arguments must be a JSON object",
                        "raw_arguments": str(args_raw)[:800],
                    },
                )
                continue

            parameters, builder_intent = split_builder_intent(dict(provided))
            ordered.append(
                OrderedBuilderCall(
                    tool_id=tool_id,
                    parameters=dict(parameters),
                    raw_arguments=args_raw,
                    provider_call_id=call.get("id") if isinstance(call.get("id"), str) else None,
                    intent=builder_intent,
                )
            )
        return ordered, parse_errors

    def parse_tool_calls(
        self,
        raw_calls: Any,
        fn_map: Dict[str, str],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, str]]]:
        """Parse raw provider tool_calls into legacy per-tool parameter maps."""
        ordered, parse_errors = self.parse_ordered_tool_calls(raw_calls, fn_map)
        parsed: Dict[str, Dict[str, Any]] = {}
        for call in ordered:
            if call.tool_id in parsed:
                if self._log:
                    self._log.debug(
                        "[PLANNER] Ignoring duplicate tool parameter call for %s",
                        call.tool_id,
                    )
                continue
            parsed[call.tool_id] = dict(call.parameters)
        return parsed, parse_errors

    @staticmethod
    def _parse_envelope_parameters(raw_params: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
        """Parse one builder-envelope ``parameters`` payload."""
        if isinstance(raw_params, str):
            stripped = raw_params.strip()
            if not stripped:
                return {}, None
            try:
                params = json.loads(stripped)
            except json.JSONDecodeError:
                return None, {
                    "message": "Builder envelope tool_call.parameters string is not valid JSON",
                    "raw_arguments": stripped[:800],
                }
        else:
            params = raw_params

        if not isinstance(params, dict):
            return None, {
                "message": "Builder envelope tool_call.parameters must be a JSON object",
                "raw_arguments": str(params)[:800] if params is not None else "",
            }
        return dict(params), None

    def validate_parameter_maps(
        self,
        *,
        selected_tools: List[str],
        provided_by_tool: Dict[str, Dict[str, Any]],
        parse_errors: Dict[str, Dict[str, str]],
        action: Action,
    ) -> Tuple[
        Dict[str, Dict[str, Any]],
        Dict[str, Dict[str, Any]],
        Dict[str, ToolParameterValidationResult],
    ]:
        """Validate selected-tool parameters and return normalized effective payloads."""
        tool_params: Dict[str, Dict[str, Any]] = {}
        llm_tool_parameters: Dict[str, Dict[str, Any]] = {}
        failures: Dict[str, ToolParameterValidationResult] = {}

        for tool_id in selected_tools:
            raw_parameters = provided_by_tool.get(tool_id)
            if isinstance(raw_parameters, dict):
                llm_tool_parameters[tool_id] = dict(raw_parameters)

            parse_error = parse_errors.get(tool_id) or {}
            validation = validate_tool_parameters(
                tool_id,
                raw_parameters,
                validation_stage="planner",
                action_target=action.target,
                parse_error=parse_error.get("message"),
                raw_arguments=parse_error.get("raw_arguments", ""),
                logger=self._log,
            )
            if validation.valid:
                tool_params[tool_id] = dict(validation.normalized_parameters)
            else:
                failures[tool_id] = validation

        return tool_params, llm_tool_parameters, failures

    async def _repair_invalid_parameters(
        self,
        *,
        selected_tools: List[str],
        parameter_failures: Dict[str, ToolParameterValidationResult],
        provided_by_tool: Dict[str, Dict[str, Any]],
        parse_errors_by_tool: Dict[str, Dict[str, str]],
        system_prompt: str,
        specs: List[Any],
        action: Action,
        context: Dict[str, Any],
        timeout_s: int,
        fn_map: Dict[str, str],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, str]]]:
        """Attempt one repair round for missing or invalid tool arguments."""
        task_id = context.get("task_id")
        tool_intent = context.get("tool_intent") or {}
        retry_target = tool_intent.get("target") if isinstance(tool_intent, dict) else None
        if not retry_target:
            retry_target = action.target

        failure_details = []
        failing_tools = [tool_id for tool_id in selected_tools if tool_id in parameter_failures]
        for tool_id in failing_tools:
            if tool_id not in parameter_failures:
                continue
            failure = parameter_failures[tool_id]
            failure_details.append(
                {
                    "tool_id": tool_id,
                    "reason": failure.reason,
                    "validation_errors": list(failure.validation_errors),
                }
            )
        if self._log:
            self._log.debug(
                "[PLANNER] Repairing invalid tool parameters for %s",
                [item.get("tool_id") for item in failure_details],
            )

        repair_result = await wait_for_with_timeout(
            self.llm_client.chat_with_tools(
                system_prompt,
                json.dumps(
                    {
                        "note": (
                            "The previous tool arguments were missing or invalid. "
                            "Call the listed tool functions again with corrected JSON arguments."
                        ),
                        "selected_tools": failing_tools,
                        "target": retry_target,
                        "phase": context.get("current_phase", "enumeration"),
                        "constraints": context.get("constraints", {}),
                        "invalid_tools": failure_details,
                    }
                ),
                tools=specs,
                tool_choice="required",
                temperature=0.1,
                max_tokens=400,
            ),
            timeout_sec=timeout_s,
            component="PLANNER",
            operation="parameter_repair_llm_call",
            logger=self._log or logging.getLogger(__name__),
            task_id=task_id,
            outcome="parameter_repair_timeout",
        )
        repaired_calls, repair_parse_errors = self._extract_repair_tool_parameters(
            repair_result,
            fn_map,
        )
        for tool_id, params in repaired_calls.items():
            if tool_id in parameter_failures:
                provided_by_tool[tool_id] = params
                parse_errors_by_tool.pop(tool_id, None)
        for tool_id, parse_error in repair_parse_errors.items():
            if tool_id in parameter_failures and tool_id not in repaired_calls:
                parse_errors_by_tool[tool_id] = parse_error
        return provided_by_tool, parse_errors_by_tool

    def _extract_repair_tool_parameters(
        self,
        repair_result: Any,
        fn_map: Dict[str, str],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, str]]]:
        """Read repair parameters from normalized tool calls or legacy dicts."""
        native_calls = getattr(repair_result, "tool_calls", None)
        if native_calls is not None:
            envelope = self._native_calls_to_envelope(native_calls, fn_map)
            provided, parse_errors = self._envelope_to_provided_by_tool(envelope)
            native_parse_errors = envelope.get("_parse_errors_by_tool", {})
            if isinstance(native_parse_errors, dict):
                for tool_id, parse_error in native_parse_errors.items():
                    if isinstance(parse_error, dict):
                        parse_errors.setdefault(tool_id, parse_error)
            return provided, parse_errors

        if isinstance(repair_result, dict):
            return self.parse_tool_calls(repair_result.get("tool_calls") or [], fn_map)
        return {}, {}

    def _envelope_to_provided_by_tool(
        self,
        envelope: Optional[Dict[str, Any]],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, str]]]:
        """Project the structured-output envelope into the per-tool param map.

        The legacy validator/repair pipeline keys provided parameters by
        ``tool_id``. This projection remains intentionally lossy for the
        non-duplicate path; duplicate ``tool_id`` envelopes are detected
        separately and validated in manifest order by
        ``_validate_duplicate_envelope_calls``.
        """
        provided: Dict[str, Dict[str, Any]] = {}
        parse_errors: Dict[str, Dict[str, str]] = {}
        if not isinstance(envelope, dict):
            return provided, parse_errors
        tool_calls = envelope.get("tool_calls")
        if not isinstance(tool_calls, list):
            return provided, parse_errors
        for entry in tool_calls:
            if not isinstance(entry, dict):
                continue
            tool_id = str(entry.get("tool_id") or "").strip()
            if not tool_id or tool_id in provided:
                continue
            # Compatibility callers may still provide a JSON-encoded
            # ``parameters`` string, while native-call paths provide decoded
            # dictionaries. Accept both shapes — mirrors
            # ``batch_commit.commit_tool_batch``.
            params, parse_error = self._parse_envelope_parameters(entry.get("parameters"))
            if parse_error is not None:
                parse_errors.setdefault(tool_id, parse_error)
                continue
            provided[tool_id] = dict(params or {})
        return provided, parse_errors

    @staticmethod
    def _has_duplicate_envelope_tool_ids(envelope: Optional[Dict[str, Any]]) -> bool:
        """Return true when the structured builder envelope repeats a tool id."""
        if not isinstance(envelope, dict):
            return False
        tool_calls = envelope.get("tool_calls")
        if not isinstance(tool_calls, list):
            return False
        seen: set[str] = set()
        for entry in tool_calls:
            if not isinstance(entry, dict):
                continue
            tool_id = str(entry.get("tool_id") or "").strip()
            if not tool_id:
                continue
            if tool_id in seen:
                return True
            seen.add(tool_id)
        return False

    def _validate_duplicate_envelope_calls(
        self,
        *,
        envelope: Dict[str, Any],
        action: Action,
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Validate duplicate-tool builder calls without collapsing by tool id."""
        raw_calls = envelope.get("tool_calls")
        if not isinstance(raw_calls, list):
            return (
                project_validated_envelope_from_calls(envelope=envelope, tool_calls=[]),
                {},
                {},
            )

        validated_calls: List[Dict[str, Any]] = []
        tool_params: Dict[str, Dict[str, Any]] = {}
        llm_tool_parameters: Dict[str, Dict[str, Any]] = {}

        for index, entry in enumerate(raw_calls):
            if not isinstance(entry, dict):
                continue
            tool_id = str(entry.get("tool_id") or "").strip()
            if not tool_id:
                continue

            raw_parameters, parse_error = self._parse_envelope_parameters(
                entry.get("parameters")
            )
            if isinstance(raw_parameters, dict) and tool_id not in llm_tool_parameters:
                llm_tool_parameters[tool_id] = dict(raw_parameters)

            validation = validate_tool_parameters(
                tool_id,
                raw_parameters,
                validation_stage="planner",
                action_target=action.target,
                parse_error=(parse_error or {}).get("message"),
                raw_arguments=(parse_error or {}).get("raw_arguments", ""),
                logger=self._log,
            )
            if not validation.valid:
                validation_errors = []
                for error in list(validation.validation_errors or []):
                    error_payload = dict(error)
                    error_payload.setdefault("tool_id", tool_id)
                    error_payload.setdefault("tool_call_index", str(index))
                    validation_errors.append(error_payload)
                if not validation_errors:
                    validation_errors.append(
                        {
                            "tool_id": tool_id,
                            "tool_call_index": str(index),
                            "message": str(validation.reason or "schema_validation_error"),
                        }
                    )
                raise PlannerToolParameterValidationError(
                    tool_id=tool_id,
                    reason=str(validation.reason or "schema_validation_error"),
                    validation_errors=validation_errors,
                    raw_arguments=str(validation.raw_arguments or ""),
                    provided_parameters=dict(validation.provided_parameters or {}),
                )

            normalized = dict(validation.normalized_parameters)
            if tool_id not in tool_params:
                tool_params[tool_id] = dict(normalized)
            validated_call: Dict[str, Any] = {
                "tool_id": tool_id,
                "parameters": normalized,
            }
            entry_intent = entry.get("intent")
            if isinstance(entry_intent, str) and entry_intent:
                validated_call["intent"] = entry_intent
            validated_calls.append(validated_call)

        return (
            project_validated_envelope_from_calls(
                envelope=envelope,
                tool_calls=validated_calls,
            ),
            tool_params,
            llm_tool_parameters,
        )

    def _native_calls_to_envelope(
        self,
        raw_calls: Any,
        fn_map: Dict[str, str],
    ) -> Dict[str, Any]:
        """Project native provider tool calls into the internal envelope shape."""
        tool_calls: List[Dict[str, Any]] = []
        parse_errors: Dict[str, Dict[str, str]] = {}

        for call in raw_calls or []:
            fn_name = getattr(call, "name", None)
            raw_arguments = getattr(call, "arguments", None)
            provider_call_id = getattr(call, "id", None)
            if not isinstance(fn_name, str) or not fn_name.strip():
                continue
            tool_id = fn_map.get(fn_name)
            if not tool_id:
                continue

            if raw_arguments is None or raw_arguments == "":
                parse_errors.setdefault(
                    tool_id,
                    {
                        "message": "Missing function arguments payload",
                        "raw_arguments": "",
                    },
                )
            elif not isinstance(raw_arguments, (str, dict)):
                parse_errors.setdefault(
                    tool_id,
                    {
                        "message": "Function arguments must be a JSON object",
                        "raw_arguments": str(raw_arguments)[:800],
                    },
                )

            # Strip the reserved intent meta-field out of the parameters before
            # they flow into validation/commit. ``split_builder_intent`` returns
            # the payload untouched on decode failure so the parse-error
            # branches above keep their existing semantics.
            parameters, builder_intent = split_builder_intent(
                raw_arguments if raw_arguments is not None else ""
            )
            entry: Dict[str, Any] = {
                "tool_id": tool_id,
                "parameters": parameters if parameters is not None else "",
            }
            if builder_intent:
                entry["intent"] = builder_intent
            if isinstance(provider_call_id, str) and provider_call_id:
                entry["provider_call_id"] = provider_call_id
            tool_calls.append(entry)

        return {
            "tool_calls": tool_calls,
            "_parse_errors_by_tool": parse_errors,
        }

    def _raise_empty_native_builder_response(self, raw_call_count: int) -> None:
        """Raise the canonical retryable error for empty native builder output."""
        detail = (
            "Builder returned no native tool calls"
            if raw_call_count == 0
            else "Builder returned no recognized native tool calls"
        )
        raise StructuredContractViolationError(
            error_code="structured_contract_schema_validation",
            stage="param_resolver",
            contract="native_tool_call_builder",
            kind="schema_validation_error",
            details=detail,
            retryable=True,
            diagnostics={"raw_tool_call_count": raw_call_count},
        )

    def _validation_targets(
        self,
        *,
        selected_tools: List[str],
        provided_by_tool: Dict[str, Dict[str, Any]],
        builder_envelope: Optional[Dict[str, Any]],
    ) -> List[str]:
        """Return the committed tool IDs that should be parameter-validated."""
        if isinstance(builder_envelope, dict):
            tool_calls = builder_envelope.get("tool_calls")
            if isinstance(tool_calls, list):
                committed: List[str] = []
                for entry in tool_calls:
                    if not isinstance(entry, dict):
                        continue
                    tool_id = str(entry.get("tool_id") or "").strip()
                    if tool_id and tool_id not in committed:
                        committed.append(tool_id)
                if committed:
                    return committed
        provided = [tool_id for tool_id in selected_tools if tool_id in provided_by_tool]
        return provided or list(selected_tools)

    def _supports_parallel_tool_calls(self) -> bool:
        """Return true when the selected model declares parallel tool support."""
        ref = _resolve_client_provider_model_ref(self.llm_client)
        if ref is None:
            return False
        try:
            return require_model_profile(ref).supports(LLMCapability.PARALLEL_TOOLS)
        except LLMProfileNotFoundError:
            return False

    async def _call_builder_with_recovery(
        self,
        *,
        system_prompt: str,
        parameters_prompt: str,
        specs: List[Any],
        fn_map: Dict[str, str],
        timeout_s: int,
        task_id: Any,
    ) -> Tuple[
        Dict[str, Dict[str, Any]],
        Dict[str, Dict[str, str]],
        Optional[Any],
        Optional[Dict[str, Any]],
    ]:
        """Run the native tool-call builder and project calls for validation.

        Returns ``(provided_by_tool, parse_errors_by_tool, usage, envelope)``.
        ``envelope`` is an internal compatibility shape projected from the
        provider's native tool-call response.
        """
        builder_kwargs: Dict[str, Any] = {
            "tool_choice": "required",
            "temperature": 0.1,
            "max_tokens": NATIVE_BUILDER_MAX_OUTPUT_TOKENS,
        }
        if self._supports_parallel_tool_calls():
            builder_kwargs["parallel_tool_calls"] = True

        logger = self._log or logging.getLogger(__name__)
        logger.warning(
            "[NATIVE_BUILDER_REQUEST] task_id=%s specs=%s function_names=%s "
            "tool_choice=%r parallel_tool_calls=%r max_tokens=%r timeout_s=%s",
            task_id,
            len(specs),
            list(fn_map.keys()),
            builder_kwargs.get("tool_choice"),
            builder_kwargs.get("parallel_tool_calls"),
            builder_kwargs.get("max_tokens"),
            timeout_s,
        )
        tool_call_result = await wait_for_with_timeout(
            self.llm_client.chat_with_tools_with_usage(
                system_prompt,
                parameters_prompt,
                tools=specs,
                **builder_kwargs,
            ),
            timeout_sec=timeout_s,
            component="PLANNER",
            operation="builder_tool_calls_llm_call",
            logger=logger,
            task_id=task_id,
            outcome="builder_tool_calls_timeout",
        )

        native_calls = list(getattr(tool_call_result, "tool_calls", None) or [])
        if not native_calls:
            raw_response = getattr(tool_call_result, "raw", None)
            logger.warning(
                "[NATIVE_BUILDER_RESULT] task_id=%s raw_native_calls=0 "
                "response_id=%s status=%s incomplete_details=%r raw_type=%s",
                task_id,
                _safe_obj_value(raw_response, "id"),
                _safe_obj_value(raw_response, "status"),
                _safe_obj_value(raw_response, "incomplete_details"),
                type(raw_response).__name__ if raw_response is not None else None,
            )
            self._raise_empty_native_builder_response(0)

        envelope = self._native_calls_to_envelope(native_calls, fn_map)
        if not envelope.get("tool_calls"):
            logger.warning(
                "[NATIVE_BUILDER_RESULT] task_id=%s raw_native_calls=%s "
                "recognized_calls=0 raw_calls=%s function_map=%s",
                task_id,
                len(native_calls),
                _native_call_summaries(native_calls),
                fn_map,
            )
            self._raise_empty_native_builder_response(len(native_calls))

        logger.warning(
            "[NATIVE_BUILDER_RESULT] task_id=%s raw_native_calls=%s recognized_calls=%s "
            "raw_calls=%s recognized_tool_ids=%s",
            task_id,
            len(native_calls),
            len(envelope.get("tool_calls") or []),
            _native_call_summaries(native_calls),
            [call.get("tool_id") for call in envelope.get("tool_calls") or []],
        )
        provided, parse_errors = self._envelope_to_provided_by_tool(envelope)
        native_parse_errors = envelope.pop("_parse_errors_by_tool", {})
        if isinstance(native_parse_errors, dict):
            for tool_id, parse_error in native_parse_errors.items():
                if isinstance(parse_error, dict):
                    parse_errors.setdefault(tool_id, parse_error)
        return (
            provided,
            parse_errors,
            getattr(tool_call_result, "usage", None),
            envelope,
        )

    async def resolve_parameters(
        self,
        *,
        system_prompt: str,
        parameters_prompt: str,
        selected_tools: List[str],
        action: Action,
        context: Dict[str, Any],
        specs: List[Any],
        fn_map: Dict[str, str],
        timeout_s: int,
    ) -> ParameterResolutionResult:
        """Resolve and validate tool parameters for selected planner tools."""
        usage_records: List[Dict[str, Any]] = []
        task_id = context.get("task_id")
        provided_by_tool, parse_errors_by_tool, builder_usage, builder_envelope = await self._call_builder_with_recovery(
            system_prompt=system_prompt,
            parameters_prompt=parameters_prompt,
            specs=specs,
            fn_map=fn_map,
            timeout_s=timeout_s,
            task_id=task_id,
        )

        param_usage = _convert_usage_to_dict(
            builder_usage,
            "planner_parameter_generation",
        )
        if param_usage:
            usage_records.append(param_usage)

        if self._has_duplicate_envelope_tool_ids(builder_envelope):
            (
                validated_builder_envelope,
                tool_params,
                llm_tool_parameters,
            ) = self._validate_duplicate_envelope_calls(
                envelope=builder_envelope or {},
                action=action,
            )
            return ParameterResolutionResult(
                tool_parameters=tool_params,
                llm_tool_parameters=llm_tool_parameters,
                usage_records=usage_records,
                builder_envelope=builder_envelope,
                validated_builder_envelope=validated_builder_envelope,
            )

        validation_targets = self._validation_targets(
            selected_tools=selected_tools,
            provided_by_tool=provided_by_tool,
            builder_envelope=builder_envelope,
        )

        tool_params, llm_tool_parameters, parameter_failures = self.validate_parameter_maps(
            selected_tools=validation_targets,
            provided_by_tool=provided_by_tool,
            parse_errors=parse_errors_by_tool,
            action=action,
        )

        if parameter_failures:
            provided_by_tool, parse_errors_by_tool = await self._repair_invalid_parameters(
                selected_tools=validation_targets,
                parameter_failures=parameter_failures,
                provided_by_tool=provided_by_tool,
                parse_errors_by_tool=parse_errors_by_tool,
                system_prompt=system_prompt,
                specs=specs,
                action=action,
                context=context,
                timeout_s=timeout_s,
                fn_map=fn_map,
            )
            tool_params, llm_tool_parameters, parameter_failures = self.validate_parameter_maps(
                selected_tools=validation_targets,
                provided_by_tool=provided_by_tool,
                parse_errors=parse_errors_by_tool,
                action=action,
            )

        if parameter_failures:
            failing_tool = next(
                (tool for tool in validation_targets if tool in parameter_failures),
                validation_targets[0],
            )
            failure = parameter_failures[failing_tool]
            raise PlannerToolParameterValidationError(
                tool_id=failing_tool,
                reason=str(failure.reason or "schema_validation_error"),
                validation_errors=list(failure.validation_errors),
                raw_arguments=str(failure.raw_arguments or ""),
                provided_parameters=dict(failure.provided_parameters or {}),
            )

        return ParameterResolutionResult(
            tool_parameters=tool_params,
            llm_tool_parameters=llm_tool_parameters,
            usage_records=usage_records,
            builder_envelope=builder_envelope,
            validated_builder_envelope=None,
        )


__all__ = [
    "LLMParameterResolver",
    "OrderedBuilderCall",
    "ParameterResolutionResult",
    "PlannerToolParameterValidationError",
]
