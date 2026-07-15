"""Response parsing for post-tool reasoning.

This module handles parsing and validation of LLM responses, including:
- Decision-only pure-JSON format (no delimiter), where observation is
  synthesized from decision fields so downstream validation remains stable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Tuple

from pydantic import ValidationError

from core.llm.json_extraction import extract_json_object
from core.prompts.constants import VALID_POST_TOOL_ACTIONS

from .models import (
    CandidateObservation,
    PostToolReasoningError,
    PostToolReasoningOutput,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Re-export the canonical PTR action vocabulary so existing parser callers and
# the public ``post_tool_reasoning`` package surface keep working without
# duplicating the set literal here. ``synthesis`` is intentionally absent; it
# belongs to the higher-level router action contract only.

# Valid statuses for todo progress tracking
VALID_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "skipped"})
_CANONICAL_VULNERABILITY_OBSERVATION_TYPE = "finding.vulnerability_candidate"
_VULNERABILITY_OBSERVATION_PATTERN = re.compile(r"^finding\.vulnerability(?:[._]|$)")
_LEGACY_VULNERABILITY_OBSERVATION_ALIASES = {
    "vulnerability_candidate",
    "vulnerability_candidates",
    "vulnerability_detected",
    "vulnerability_detected_candidate",
}


# =============================================================================
# JSON Extraction
# =============================================================================


def extract_json_from_text(text: str) -> str:
    """Extract JSON object from text that may contain surrounding content.
    
    Handles common patterns:
    - Pure JSON: {"key": "value"}
    - Markdown wrapped: ```json\\n{...}\\n```
    - Text with embedded JSON
    
    Args:
        text: Text that may contain JSON.
        
    Returns:
        Extracted JSON string.
        
    Raises:
        PostToolReasoningError: If no valid JSON object found.
    """
    text = text.strip()
    extracted = extract_json_object(text)
    if not extracted:
        # Preserve truncated-JSON recovery signal consumed by
        # split_observation_and_decision().
        if text.startswith("{"):
            raise PostToolReasoningError(
                f"Unbalanced braces in text: {text[:200]}"
            )
        raise PostToolReasoningError(
            f"No JSON object found in text: {text[:200]}"
        )

    return json.dumps(extracted)


# =============================================================================
# Response Splitting
# =============================================================================


def _build_decision_only_observation(data: dict) -> str:
    """Build a minimal synthetic observation for decision-only payloads.

    Decision-only outputs (new format) do not include free-form observation
    text. Build a short, readable fallback so downstream validation and
    storage still receive non-empty observation content.
    """
    next_action = data.get("next_action")
    action_reasoning = data.get("action_reasoning")
    parts: list[str] = []

    if isinstance(next_action, str):
        parts.append(f"Decision: {next_action}")
    if isinstance(action_reasoning, str):
        reason = action_reasoning.strip()
        if reason:
            parts.append(f"Reasoning: {reason}")

    if not parts:
        return ""

    observation = ". ".join(parts).strip()
    if not observation.endswith("."):
        observation += "."
    return observation


def split_observation_and_decision(response: str) -> Tuple[str, str]:
    """Split response into synthesized observation text and decision JSON.

    Current contract is decision-only JSON (no delimiters):
        {"next_action": "...", "action_reasoning": "..."}
    
    Args:
        response: Raw LLM response text.
        
    Returns:
        Tuple of (observation_text, decision_json_str).
        
    Raises:
        PostToolReasoningError: If format is invalid.
    """
    response = response.strip()
    
    # Parse pure JSON response.
    try:
        json_str = extract_json_from_text(response)
        data = json.loads(json_str)
        if "next_action" not in data:
            raise PostToolReasoningError(
                f"Decision JSON missing required 'next_action' field. Got: {response[:300]}"
            )
        fallback_observation = _build_decision_only_observation(data)
        return fallback_observation, json_str
    except PostToolReasoningError as exc:
        if response.startswith("{") and "Unbalanced braces" in str(exc):
            logger.warning(
                "[PARSER] Decision JSON appears truncated (unbalanced braces); "
                "attempting partial recovery."
            )
            return "", response
        raise
    except json.JSONDecodeError:
        # Allow truncated pure JSON fallback when response starts like a JSON object.
        if response.startswith("{"):
            logger.warning(
                "[PARSER] Decision JSON appears truncated (json decode error); "
                "attempting partial recovery."
            )
            return "", response

    raise PostToolReasoningError(
        f"Response is not valid decision JSON. Got: {response[:300]}"
    )


# =============================================================================
# Main Parser
# =============================================================================


def _normalize_decision_fields(data: dict) -> dict:
    """Normalize decision payload to satisfy schema validation.

    - Empty/whitespace failure_category values are treated as absent so
      Pydantic can apply the default None (prevents literal validation errors
      when LLM returns "").
    """
    failure_category = data.get("failure_category")
    if isinstance(failure_category, str):
        trimmed = failure_category.strip()
        if not trimmed:
            data.pop("failure_category", None)
        else:
            data["failure_category"] = trimmed

    candidate_rows = data.get("candidate_observations")
    if isinstance(candidate_rows, list):
        data["candidate_observations"] = _sanitize_candidate_observations(candidate_rows)
    return data


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


def _sanitize_candidate_observations(rows: list[Any]) -> list[dict[str, Any]]:
    """Canonicalize candidate observation types and drop invalid rows."""
    sanitized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        candidate_row = dict(row)
        canonical_type = _canonicalize_observation_type(candidate_row.get("observation_type"))
        if canonical_type:
            candidate_row["observation_type"] = canonical_type
        try:
            CandidateObservation.model_validate(candidate_row)
        except Exception as exc:
            logger.warning(
                "[PARSER] Dropping invalid candidate_observations[%s]: %s",
                index,
                exc,
            )
            continue
        sanitized_rows.append(candidate_row)
    return sanitized_rows


def _decode_partial_json_string(value: str) -> str:
    """Decode a JSON string fragment with best-effort fallback.

    The fragment may be truncated because streaming stopped mid-token.
    """
    value = value.replace("\r", " ").replace("\n", " ").strip()
    if not value:
        return ""

    # Truncated responses can end with a dangling escape.
    if value.endswith("\\"):
        value = value[:-1]

    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        # Fall back to minimal unescape for common sequences.
        return (
            value.replace('\\"', '"')
            .replace("\\n", " ")
            .replace("\\t", " ")
            .replace("\\r", " ")
        )


def _extract_partial_json_string_field(decision_json: str, field_name: str) -> str | None:
    """Extract a possibly-truncated JSON string field value by key."""
    field_pattern = rf'"{re.escape(field_name)}"\s*:\s*"'
    match = re.search(field_pattern, decision_json)
    if not match:
        return None

    chars: list[str] = []
    idx = match.end()
    escaped = False

    while idx < len(decision_json):
        ch = decision_json[idx]
        if escaped:
            chars.append(ch)
            escaped = False
            idx += 1
            continue

        if ch == "\\":
            chars.append(ch)
            escaped = True
            idx += 1
            continue

        if ch == '"':
            break

        chars.append(ch)
        idx += 1

    raw_value = "".join(chars).strip()
    if not raw_value:
        return None

    decoded_value = _decode_partial_json_string(raw_value)
    normalized = re.sub(r"\s+", " ", decoded_value).strip()
    return normalized or None


def _recover_truncated_decision_payload(decision_json: str) -> dict | None:
    """Recover required decision fields from truncated JSON output.

    This recovery is intentionally narrow: only fields required for schema
    validity are extracted, and only when values are clearly present.
    """
    action_match = re.search(r'"next_action"\s*:\s*"([^"]+)"', decision_json)
    if not action_match:
        return None

    next_action = action_match.group(1).strip()
    if next_action not in VALID_POST_TOOL_ACTIONS:
        return None

    action_reasoning = _extract_partial_json_string_field(
        decision_json,
        "action_reasoning",
    )
    if not action_reasoning:
        return None

    return {
        "next_action": next_action,
        "action_reasoning": action_reasoning,
    }


def parse_reasoning_response(response: str) -> PostToolReasoningOutput:
    """Parse LLM response into structured PostToolReasoningOutput.

    Expects decision-only JSON response with the current post-tool contract.
    If response is truncated, recovers the minimal required fields when possible.
    
    Args:
        response: Raw LLM response text.
        
    Returns:
        Validated PostToolReasoningOutput instance.
        
    Raises:
        PostToolReasoningError: If response cannot be parsed or validated.
    """
    if not response or not response.strip():
        raise PostToolReasoningError("Empty response from LLM")
    
    # Split observation and decision
    try:
        observation_text, decision_json = split_observation_and_decision(response)
    except PostToolReasoningError:
        raise  # Re-raise extraction errors
    
    # Parse decision JSON
    try:
        data = json.loads(decision_json)
    except json.JSONDecodeError as e:
        recovered = _recover_truncated_decision_payload(decision_json)
        if recovered is None:
            raise PostToolReasoningError(
                f"Invalid JSON in decision: {e}. JSON string: {decision_json[:200]}"
            ) from e
        logger.warning(
            "[PARSER] Recovered truncated decision JSON (len=%s): %s",
            len(decision_json),
            e,
        )
        data = recovered
    
    # Observation is synthesized from decision fields under the new contract.
    if "observation" in data and isinstance(data.get("observation"), str):
        provided_observation = str(data.get("observation") or "").strip()
        if provided_observation:
            data["observation"] = provided_observation
        else:
            data.pop("observation", None)

    if "observation" not in data:
        if observation_text:
            data["observation"] = observation_text
        else:
            fallback_observation = _build_decision_only_observation(data)
            if fallback_observation:
                data["observation"] = fallback_observation
            else:
                raise PostToolReasoningError(
                    "Could not determine observation text for decision payload"
                )
    
    # Normalize fields before validation (handles empty strings, etc.)
    data = _normalize_decision_fields(data)
    
    # Validate with Pydantic
    try:
        output = PostToolReasoningOutput.model_validate(data)
    except ValidationError as e:
        # Extract useful error message
        errors = e.errors()
        error_details = "; ".join(
            f"{err['loc']}: {err['msg']}" for err in errors[:3]
        )
        raise PostToolReasoningError(
            f"Response validation failed: {error_details}. Data: {data}"
        ) from e
    
    # Additional validation: ensure next_action is in valid set
    if output.next_action not in VALID_POST_TOOL_ACTIONS:
        raise PostToolReasoningError(
            f"Invalid next_action '{output.next_action}'. "
            f"Valid actions: {VALID_POST_TOOL_ACTIONS}"
        )
    
    # STRICT: Enforce tool_intent when next_action is call_tool
    # NO FALLBACK - if LLM doesn't provide tool_intent, force reflect action
    # This prevents silent failures and stale target bugs
    if output.next_action == "call_tool" and not output.tool_intent:
        logger.error(
            "[PARSER] LLM decided call_tool but didn't provide tool_intent. "
            "This is a schema violation - forcing reflect action to re-evaluate."
        )
        # Force reflect instead of creating fake data
        output.next_action = "reflect"
        output.action_reasoning = (
            "(FORCED: LLM must provide tool_intent when choosing call_tool. "
            f"Original reasoning: {output.action_reasoning})"
        )
    
    # CRITICAL: Enforce consistency between user_goal_achieved and next_action
    # If the LLM says goal is achieved but chose a non-finalize action, override
    # This ensures we don't continue looping when the goal is satisfied
    if output.user_goal_achieved and output.next_action != "finalize":
        logger.warning(
            f"[PARSER] user_goal_achieved=True but next_action='{output.next_action}'. "
            "Overriding to 'finalize' for consistency. The goal is achieved, so we should stop."
        )
        output.next_action = "finalize"
        output.action_reasoning = (
            f"(Override: goal achieved) {output.action_reasoning}"
        )
    
    # Validate todo_progress indices and statuses if provided
    if output.todo_progress:
        for progress in output.todo_progress:
            if progress.status not in VALID_TODO_STATUSES:
                logger.warning(
                    f"[PARSER] Invalid todo status '{progress.status}' at index {progress.index}. "
                    f"Valid statuses: {VALID_TODO_STATUSES}"
                )
    
    return output


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "VALID_POST_TOOL_ACTIONS",
    "VALID_TODO_STATUSES",
    # Functions
    "extract_json_from_text",
    "split_observation_and_decision",
    "parse_reasoning_response",
    "_normalize_decision_fields",
    "_decode_partial_json_string",
    "_extract_partial_json_string_field",
    "_recover_truncated_decision_payload",
]








