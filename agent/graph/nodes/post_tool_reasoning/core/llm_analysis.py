"""Capability-agnostic LLM analysis and decision making.

This module owns the decision-only LLM call for post-tool reasoning.
It keeps structured-output recovery localized to this scope so the node
can automatically recover once from provider parse failures and surface a
single retryable checkpoint error when recovery is exhausted.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.providers.llm.core.base import LLMClient

from agent.providers.llm.core.exceptions import (
    LLMRefusalError,
    LLMResponseError,
    LLMStructuredOutputParseError,
)
from agent.providers.llm.contracts.recovery import (
    build_provider_recovery_diagnostics,
    log_provider_recovery_attempt,
)
from agent.reasoning.structured_contract_recovery import (
    contains_retryable_llm_timeout,
    run_structured_contract_retry,
)
from core.llm import LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC, wait_for_with_timeout
from core.llm.structured_schemas import POST_TOOL_DECISION_STRUCTURED_OUTPUT
from ..parser import parse_reasoning_response
from ..models import (
    CandidateObservation,
    PostToolReasoningDecisionOutput,
    PostToolReasoningError,
    RetryablePostToolReasoningError,
)
from .failure_detection import FailureContext
from agent.graph.config.token_limits import LIMITS

logger = logging.getLogger(__name__)

# Constants - use centralized limits
MAX_REASONING_TOKENS = LIMITS.post_tool_reasoning
DEFAULT_TEMPERATURE = 0.3
_RETRYABLE_PARSE_ERROR_CODE = "provider_structured_output_parse"
_CANONICAL_VULNERABILITY_OBSERVATION_TYPE = "finding.vulnerability_candidate"
_VULNERABILITY_OBSERVATION_PATTERN = re.compile(r"^finding\.vulnerability(?:[._]|$)")
_LEGACY_VULNERABILITY_OBSERVATION_ALIASES = {
    "vulnerability_candidate",
    "vulnerability_candidates",
    "vulnerability_detected",
    "vulnerability_detected_candidate",
}


def _canonicalize_observation_type(observation_type: Any) -> str:
    """Normalize known legacy vulnerability observation types to canonical form."""
    normalized = str(observation_type or "").strip()
    if not normalized:
        return normalized
    if _VULNERABILITY_OBSERVATION_PATTERN.search(normalized):
        return normalized
    alias_key = normalized.lower().replace("-", "_").replace(" ", "_")
    if alias_key in _LEGACY_VULNERABILITY_OBSERVATION_ALIASES:
        return _CANONICAL_VULNERABILITY_OBSERVATION_TYPE
    return normalized


def _sanitize_candidate_observations(payload: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize and validate candidate rows before strict model validation.

    Invalid candidate rows are dropped to avoid failing the entire decision payload.
    """
    rows = payload.get("candidate_observations")
    if not isinstance(rows, list):
        return payload

    sanitized_rows: list[dict[str, Any]] = []
    dropped_count = 0
    canonicalized_count = 0

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            dropped_count += 1
            continue
        candidate_row = dict(row)
        current_type = candidate_row.get("observation_type")
        canonical_type = _canonicalize_observation_type(current_type)
        if canonical_type and canonical_type != current_type:
            candidate_row["observation_type"] = canonical_type
            canonicalized_count += 1
        try:
            CandidateObservation.model_validate(candidate_row)
        except Exception as exc:
            dropped_count += 1
            logger.warning(
                "[LLM_ANALYSIS] Dropping invalid candidate_observations[%s]: %s",
                index,
                exc,
            )
            continue
        sanitized_rows.append(candidate_row)

    if canonicalized_count or dropped_count:
        logger.info(
            "[LLM_ANALYSIS] Candidate row sanitation applied "
            "(canonicalized=%s dropped=%s kept=%s)",
            canonicalized_count,
            dropped_count,
            len(sanitized_rows),
        )

    sanitized_payload = dict(payload)
    sanitized_payload["candidate_observations"] = sanitized_rows
    return sanitized_payload


def _build_provider_recovery_diagnostics(exc: Exception) -> dict[str, object]:
    """Build normalized diagnostics for provider-side decision call failures.

    Delegates to the shared recovery helper.
    """
    return build_provider_recovery_diagnostics(exc)


def _log_provider_recovery_attempt(exc: Exception, diagnostics: dict[str, object]) -> None:
    """Emit provider recovery logs with structured/response specific context.

    Delegates to the shared recovery helper with LLM_ANALYSIS prefix.
    """
    log_provider_recovery_attempt(
        exc, diagnostics, target_logger=logger, log_prefix="LLM_ANALYSIS",
    )


def _parse_decision_output_from_response(
    response: str,
    structured_payload: Any,
) -> PostToolReasoningDecisionOutput:
    """Normalize provider output into the decision-only runtime contract."""
    if structured_payload is not None and not isinstance(structured_payload, dict):
        logger.warning(
            "[LLM_ANALYSIS] structured_output is not dict-like (%s); "
            "falling back to response content",
            type(structured_payload).__name__,
        )
        structured_payload = None

    if structured_payload is not None:
        sanitized_payload = _sanitize_candidate_observations(structured_payload)
        return PostToolReasoningDecisionOutput.model_validate(sanitized_payload)

    parsed_output = parse_reasoning_response(response)
    payload = parsed_output.model_dump(exclude={"observation"})
    return PostToolReasoningDecisionOutput.model_validate(payload)


async def _call_decision_llm(
    *,
    llm_client: "LLMClient",
    system_prompt: str,
    user_prompt: str,
    interactive: Optional[Any],
    reasoning_effort: Optional[str],
    structured_output: bool,
) -> tuple[str, Any]:
    """Execute the post-tool decision call with optional structured mode."""
    request_kwargs = {
        "temperature": DEFAULT_TEMPERATURE,
        "max_tokens": MAX_REASONING_TOKENS,
        "reasoning_effort": reasoning_effort,
    }

    if structured_output:
        request_kwargs["structured_output"] = POST_TOOL_DECISION_STRUCTURED_OUTPUT

    if hasattr(llm_client, "chat_with_usage"):
        llm_response = await wait_for_with_timeout(
            llm_client.chat_with_usage(
                system_prompt,
                user_prompt,
                **request_kwargs,
            ),
            timeout_sec=LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
            component="POST_TOOL_OBSERVATION",
            operation="decision_llm_call",
            logger=logger,
            task_id=getattr(getattr(interactive, "facts", None), "task_id", None),
            outcome="post_tool_decision_timeout",
        )
        if interactive is not None and llm_response.usage:
            from ...node_utils import append_usage_to_state

            append_usage_to_state(
                interactive,
                llm_response.usage,
                "post_tool_analysis",
                request_mode="non_streaming",
            )
        return llm_response.content, llm_response.structured_output

    response = await wait_for_with_timeout(
        llm_client.chat(
            system_prompt,
            user_prompt,
            **request_kwargs,
        ),
        timeout_sec=LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
        component="POST_TOOL_OBSERVATION",
        operation="decision_llm_call",
        logger=logger,
        task_id=getattr(getattr(interactive, "facts", None), "task_id", None),
        outcome="post_tool_decision_timeout",
    )
    return response, None


async def analyze_tool_result(
    llm_client: "LLMClient",
    system_prompt: str,
    user_prompt: str,
    failure_context: Optional[FailureContext] = None,
    interactive: Optional[Any] = None,
    reasoning_effort: Optional[str] = None,
) -> PostToolReasoningDecisionOutput:
    """Analyze tool result using LLM and return decision-only structured payload.
    
    Pure business logic - no streaming, no capability checks. This function
    makes a non-streaming LLM call with `structured_output` to produce decision
    fields only.
    
    Args:
        llm_client: The LLMClient instance for making calls
        system_prompt: System prompt guiding the LLM's behavior
        user_prompt: User prompt with tool result context
        failure_context: Optional failure context for debugging/logging
        interactive: Optional InteractiveState for usage tracking (Phase 7)
        
    Returns:
        Structured PostToolReasoningDecisionOutput with decision fields
        
    Raises:
        PostToolReasoningError: If LLM call or parsing fails
    """
    logger.debug(
        "[LLM_ANALYSIS] Analyzing tool result "
        f"(failure_context={'provided' if failure_context else 'none'})"
    )

    response: str = ""
    payload: Any = None
    provider_recovery_diagnostics: dict[str, object] | None = None

    try:
        response, payload = await _call_decision_llm(
            llm_client=llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            interactive=interactive,
            reasoning_effort=reasoning_effort,
            structured_output=True,
        )
    except LLMRefusalError:
        raise
    except (LLMStructuredOutputParseError, LLMResponseError) as exc:
        diagnostics = _build_provider_recovery_diagnostics(exc)
        provider_recovery_diagnostics = diagnostics
        _log_provider_recovery_attempt(exc, diagnostics)
        try:
            response, payload = await _call_decision_llm(
                llm_client=llm_client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                interactive=interactive,
                reasoning_effort=reasoning_effort,
                structured_output=False,
            )
        except LLMRefusalError:
            raise
        except Exception as fallback_exc:
            diagnostics["fallback_error"] = str(fallback_exc)
            msg = (
                "Provider returned invalid or incomplete decision output and plain-text "
                f"fallback failed: {fallback_exc}"
            )
            logger.error("[LLM_ANALYSIS] %s", msg, exc_info=True)
            raise RetryablePostToolReasoningError(
                msg,
                error_code=_RETRYABLE_PARSE_ERROR_CODE,
                diagnostics=diagnostics,
            ) from fallback_exc
    except Exception as exc:
        msg = f"LLM call failed: {exc}"
        logger.error(f"[LLM_ANALYSIS] {msg}", exc_info=True)
        raise PostToolReasoningError(msg) from exc

    try:
        output = _parse_decision_output_from_response(response, payload)
    except Exception as exc:
        if provider_recovery_diagnostics is not None:
            provider_recovery_diagnostics["fallback_parse_error"] = str(exc)
            raise RetryablePostToolReasoningError(
                f"Failed to recover provider structured response: {exc}",
                error_code=_RETRYABLE_PARSE_ERROR_CODE,
                diagnostics=provider_recovery_diagnostics,
            ) from exc
        if isinstance(exc, PostToolReasoningError):
            raise
        logger.error(
            f"[LLM_ANALYSIS] Failed to parse structured LLM response: {exc}",
            exc_info=True,
        )
        raise PostToolReasoningError(f"Failed to parse structured response: {exc}") from exc

    # Hardening: if schema validation returns call_tool without a usable intent,
    # force a safe non-tool action so routing cannot proceed without a
    # structured target/focus contract.
    if output.next_action == "call_tool" and output.tool_intent is None:
        logger.warning(
            "[LLM_ANALYSIS] call_tool decision missing tool_intent; "
            "forcing reflect to avoid un-actionable tool routing."
        )
        output.next_action = "reflect"
        output.action_reasoning = (
            "(FORCED: call_tool requires tool_intent) "
            f"Original reasoning: {output.action_reasoning}"
        )
    
    logger.debug(
        f"[LLM_ANALYSIS] Completed analysis: "
        f"action={output.next_action}, "
        f"failure_detected={output.failure_detected}, "
        f"retry_suggested={output.retry_suggested}"
    )
    
    return output


def _is_retryable_post_tool_contract_error(exc: BaseException) -> bool:
    """Return whether a decision-stage error is retryable as a contract failure."""
    return isinstance(exc, RetryablePostToolReasoningError) or contains_retryable_llm_timeout(exc)


async def analyze_tool_result_with_retry(
    llm_client: "LLMClient",
    system_prompt: str,
    user_prompt: str,
    failure_context: Optional[FailureContext] = None,
    interactive: Optional[Any] = None,
    reasoning_effort: Optional[str] = None,
) -> PostToolReasoningDecisionOutput:
    """Analyze tool output with bounded silent retry for retryable contract failures."""
    return await run_structured_contract_retry(
        operation=lambda: analyze_tool_result(
            llm_client=llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            failure_context=failure_context,
            interactive=interactive,
            reasoning_effort=reasoning_effort,
        ),
        logger=logger,
        stage="post_tool_reasoning",
        contract=POST_TOOL_DECISION_STRUCTURED_OUTPUT.name,
        max_attempts=2,
        backoff_seconds=0.25,
        is_retryable_error=_is_retryable_post_tool_contract_error,
    )


def build_analysis_context(
    failure_detected: bool,
    failure_category: Optional[str],
    retry_count: int,
    max_retries: int,
) -> dict:
    """Build context dictionary for LLM analysis.
    
    Helper function to package failure information for inclusion in prompts.
    
    Args:
        failure_detected: Whether a failure was detected
        failure_category: Category of failure (if detected)
        retry_count: Current retry attempt count
        max_retries: Maximum allowed retries
        
    Returns:
        Dictionary with analysis context
    """
    return {
        "failure_detected": failure_detected,
        "failure_category": failure_category,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "retry_budget_remaining": max(0, max_retries - retry_count),
    }


__all__ = [
    "MAX_REASONING_TOKENS",
    "DEFAULT_TEMPERATURE",
    "analyze_tool_result",
    "analyze_tool_result_with_retry",
    "build_analysis_context",
]
