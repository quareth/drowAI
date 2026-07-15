"""LLM-backed intent classifier for LangGraph routing.

Reads the recent-transcript window from the shared hot-path
``ConversationContextBundle`` (assembled once per turn by the context
builder) via the classifier projection. This module no longer owns a
local transcript formatter — continuity decisions here see the same
verbatim recent turns as every other prompt-authoritative role.

Phase 5 cutover: the bundle is now the sole prompt-authority. When the
bundle is missing from metadata the classifier raises ``RuntimeError``
rather than silently falling back to a local formatter; a missing
bundle indicates an upstream wiring bug. ``LangGraphContextBuilder.build_runtime_config``
is the single assembly authority — it populates
``metadata[METADATA_CONTEXT_BUNDLE_KEY]`` once per turn and every
downstream consumer (classifier, graph nodes, facade_helpers) reads
that one bundle.
"""

from __future__ import annotations

import asyncio
import time
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.projections import (
    project_for_intent_classifier,
)
from agent.graph.context.serialization import (
    SECTION_RECENT_TRANSCRIPT,
    serialize_projection_to_section_map,
)
from agent.graph.infrastructure.state_models import CapabilityType, IntentSignals
from agent.providers.llm.core.base import LLMClient
from agent.providers.llm.core.exceptions import LLMRefusalError
from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    ProviderModelRef,
)
from agent.providers.llm.profiles.registry import resolve_context_window_tokens
from core.prompts.builders.intent_classifier import build_classifier_user_prompt
from core.prompts.constants import CLASSIFIER_SYSTEM_PROMPT
from core.llm import LLM_TIMEOUT_INTENT_CLASSIFIER_SEC, wait_for_with_timeout
from core.llm.structured_schemas import INTENT_CLASSIFIER_STRUCTURED_OUTPUT
from core.prompts.route_labels import llm_facing_route_label
from agent.graph.config.token_limits import LIMITS

from backend.services.langgraph_chat.contracts import (
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.intent.briefs import (
    write_intent_brief_seed,
)
from backend.services.langgraph_chat.model_role_registry import (
    ModelRoleRegistry,
    ROLE_INTENT_CLASSIFIER,
    RoleCallSettings,
)
from backend.services.metrics.utils import safe_inc

if TYPE_CHECKING:
    from backend.services.usage_tracking.models import UsageData

logger = logging.getLogger("backend.services.langgraph_chat.intent_classifier")

ANTHROPIC_INTENT_CLASSIFIER_MAX_TOKENS = 8_192

_BINARY_CHECK_HINTS = (
    "determine if",
    "whether",
    "open or closed",
    "is port",
    "is the port",
)
_SHORT_STYLE_HINTS = (
    "short answer",
    "brief answer",
    "one line",
    "just answer",
    "yes or no",
    "yes/no",
)

_TARGET_STATUS_VALUES = frozenset({"resolved", "unresolved", "ambiguous"})
_TARGET_SOURCE_VALUES = frozenset(
    {"explicit_current_message", "referential_history", "environment", "none"}
)
_TARGET_CONTINUITY_VALUES = frozenset({"allow", "disallow", "ambiguous"})
_PRIOR_TURN_REFERENCE_OPERATIONS = frozenset(
    {
        "reference_resolution",
        "continuation",
        "revision",
        "comparison",
        "quote_or_recall",
        "none",
    }
)
_PRIOR_TURN_REFERENCE_STATUSES = frozenset(
    {"resolved", "ambiguous", "unresolved", "none"}
)
_PRIOR_TURN_REFERENCE_KINDS = frozenset(
    {"rendered_turn", "relative_turn", "anchor_text", "unknown"}
)
_PRIOR_TURN_REFERENCE_SPEAKERS = frozenset(
    {"user", "assistant", "system", "tool", "unknown"}
)

_ROUTING_LABEL_ALIASES: Dict[str, str] = {
    "simple_chat": "simple_chat",
    "chat": "simple_chat",
    "normal_chat": "simple_chat",
    "respond": "simple_chat",
    "respond_only": "simple_chat",
    "direct_executor": "direct_executor",
    "tool_call": "direct_executor",
    "tool": "direct_executor",
    "tool_execution": "direct_executor",
    "simple_tool": "direct_executor",
    "simple_tool_execution": "direct_executor",
    "plan_executor": "plan_executor",
    "deep_reasoning": "plan_executor",
    "deep": "plan_executor",
    "dr": "plan_executor",
    "deepreasoning": "plan_executor",
    "multi_step_execution": "plan_executor",
    "multi_step": "plan_executor",
    "multistep": "plan_executor",
}


def _classifier_label_to_internal_route(label: Any) -> str | None:
    """Map canonical classifier labels onto internal graph route identifiers."""
    normalized = _canonicalize_routing_label(label, fallback_label="")
    if normalized == "direct_executor":
        return "simple_tool_execution"
    if normalized == "plan_executor":
        return "deep_reasoning"
    if normalized == "simple_chat":
        return "normal_chat"
    return None


def _load_environment_section(
    task_id: Optional[int],
    environment_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Load container environment info for the classifier prompt.

    Reuses the existing environment_collector to format network interfaces,
    routes, and DNS — giving the classifier grounded data to resolve
    environment-relative targets (e.g. "vpn network" → tun0 CIDR).
    """
    if isinstance(environment_info, dict):
        try:
            from backend.services.workspace.environment_collector import format_environment_for_prompt

            formatted = format_environment_for_prompt(environment_info)
            return f"\nContainer Environment:\n{formatted}" if formatted else ""
        except Exception as exc:
            logger.debug("Failed to format shared environment for classifier: %s", exc)
            return ""
    if task_id is None:
        return ""
    try:
        from backend.services.runtime_provider.environment_metadata import (
            load_runtime_environment_metadata,
        )
        from backend.services.workspace.environment_collector import format_environment_for_prompt

        env_info = load_runtime_environment_metadata(
            task_id=int(task_id),
            actor_id="intent_classifier",
        )
        if env_info is None:
            return ""
        formatted = format_environment_for_prompt(env_info)
        return f"\nContainer Environment:\n{formatted}" if formatted else ""
    except Exception as exc:
        logger.debug("Failed to load environment for classifier: %s", exc)
        return ""


def _is_binary_check_message(message: str) -> bool:
    lowered = message.lower()
    if any(token in lowered for token in _BINARY_CHECK_HINTS):
        return True
    if re.search(r"\bis\s+.+\s+(?:open|closed|up|down)\b", lowered):
        return True
    return False


def _build_request_contract(parsed: Dict[str, Any], message: str) -> Dict[str, str]:
    """Build lightweight request contract for routing/finalization policy."""
    contract: Dict[str, str] = {}

    question_type = parsed.get("question_type")
    if isinstance(question_type, str):
        question_type = question_type.strip().lower()
        if question_type in {"binary_check", "multi_step", "open_ended"}:
            contract["question_type"] = question_type

    answer_style = parsed.get("answer_style")
    if isinstance(answer_style, str):
        answer_style = answer_style.strip().lower()
        if answer_style in {"short", "normal"}:
            contract["answer_style"] = answer_style

    terminal_when = parsed.get("terminal_when")
    if isinstance(terminal_when, str):
        terminal_when = terminal_when.strip().lower()
        if terminal_when in {"determined", "all_steps_done"}:
            contract["terminal_when"] = terminal_when

    # Heuristic fallback when classifier does not emit contract fields.
    if "question_type" not in contract and _is_binary_check_message(message):
        contract["question_type"] = "binary_check"

    if "answer_style" not in contract:
        lowered = message.lower()
        if any(token in lowered for token in _SHORT_STYLE_HINTS):
            contract["answer_style"] = "short"
        elif contract.get("question_type") == "binary_check":
            contract["answer_style"] = "short"
        else:
            contract["answer_style"] = "normal"

    if "terminal_when" not in contract:
        if contract.get("question_type") == "binary_check":
            contract["terminal_when"] = "determined"
        else:
            contract["terminal_when"] = "all_steps_done"

    return contract


def _normalize_intent_target_resolution(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize classifier-provided target resolution into strict metadata shape."""
    raw_status = parsed.get("target_status")
    status = str(raw_status or "").strip().lower()
    if status not in _TARGET_STATUS_VALUES:
        status = "unresolved"

    raw_source = parsed.get("target_source")
    source = str(raw_source or "").strip().lower()
    if source not in _TARGET_SOURCE_VALUES:
        source = "none"

    resolved_target: Optional[str] = None
    raw_target = parsed.get("resolved_target")
    if isinstance(raw_target, str):
        stripped_target = raw_target.strip()
        if stripped_target:
            resolved_target = stripped_target

    confidence: Optional[float] = None
    raw_confidence = parsed.get("target_confidence")
    if isinstance(raw_confidence, (int, float)):
        normalized_confidence = float(raw_confidence)
        if 0.0 <= normalized_confidence <= 1.0:
            confidence = normalized_confidence

    evidence: Optional[str] = None
    raw_evidence = parsed.get("target_evidence")
    if isinstance(raw_evidence, str):
        stripped_evidence = raw_evidence.strip()
        if stripped_evidence:
            evidence = stripped_evidence

    # Enforce strict contract: resolved requires a concrete non-empty target.
    if status == "resolved" and not resolved_target:
        status = "unresolved"
        source = "none"
        confidence = None
        evidence = None

    # Non-resolved statuses must not carry concrete target values.
    if status != "resolved":
        resolved_target = None

    # If source says no target, ensure status is unresolved.
    if source == "none" and status == "resolved":
        status = "unresolved"
        resolved_target = None
        confidence = None
        evidence = None

    return {
        "target_status": status,
        "resolved_target": resolved_target,
        "target_source": source,
        "target_confidence": confidence,
        "target_evidence": evidence,
    }


def _normalize_intent_target_continuity(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize classifier-provided target continuity into strict metadata shape."""
    raw_status = parsed.get("prior_target_reuse")
    status = str(raw_status or "").strip().lower()
    if status not in _TARGET_CONTINUITY_VALUES:
        status = "disallow"

    evidence: Optional[str] = None
    raw_evidence = parsed.get("prior_target_reuse_evidence")
    if isinstance(raw_evidence, str):
        stripped = raw_evidence.strip()
        if stripped:
            evidence = stripped

    if status == "disallow":
        # Keep disallow deterministic and compact unless classifier provided a reason.
        evidence = evidence or None

    return {
        "status": status,
        "evidence": evidence,
        "source": "classifier",
    }


def _prior_turn_reference_none() -> Dict[str, Any]:
    """Return the safe empty prior-turn reference shape."""
    return {
        "required": False,
        "operation": "none",
        "status": "none",
        "confidence": None,
        "hints": [],
    }


def _optional_str(value: Any) -> Optional[str]:
    """Return a stripped non-empty string, otherwise ``None``."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _confidence_or_none(value: Any) -> Optional[float]:
    """Normalize confidence values to the closed interval [0.0, 1.0]."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        confidence = float(value)
        if 0.0 <= confidence <= 1.0:
            return confidence
    return None


def _normalize_prior_turn_reference(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize classifier prior-turn resolver hints into metadata shape."""
    raw = parsed.get("prior_turn_reference")
    if not isinstance(raw, dict):
        return _prior_turn_reference_none()

    required = raw.get("required") is True
    operation = str(raw.get("operation") or "").strip().lower()
    if operation not in _PRIOR_TURN_REFERENCE_OPERATIONS:
        operation = "none"

    status = str(raw.get("status") or "").strip().lower()
    if status not in _PRIOR_TURN_REFERENCE_STATUSES:
        status = "unresolved" if required else "none"

    confidence = _confidence_or_none(raw.get("confidence"))
    hints: List[Dict[str, Any]] = []
    raw_hints = raw.get("hints")
    if isinstance(raw_hints, list):
        for raw_hint in raw_hints:
            if not isinstance(raw_hint, dict):
                continue

            reference_kind = str(raw_hint.get("reference_kind") or "").strip().lower()
            if reference_kind not in _PRIOR_TURN_REFERENCE_KINDS:
                reference_kind = "unknown"

            raw_turn_number = raw_hint.get("turn_number")
            turn_number: Optional[int] = None
            if (
                isinstance(raw_turn_number, int)
                and not isinstance(raw_turn_number, bool)
                and raw_turn_number >= 1
            ):
                turn_number = raw_turn_number

            speaker = None
            raw_speaker = raw_hint.get("speaker")
            if isinstance(raw_speaker, str):
                normalized_speaker = raw_speaker.strip().lower()
                if normalized_speaker in _PRIOR_TURN_REFERENCE_SPEAKERS:
                    speaker = normalized_speaker

            hints.append(
                {
                    "reference_kind": reference_kind,
                    "turn_number": turn_number,
                    "speaker": speaker,
                    "anchor_text": _optional_str(raw_hint.get("anchor_text")),
                    "reason": _optional_str(raw_hint.get("reason")),
                    "confidence": _confidence_or_none(raw_hint.get("confidence")),
                }
            )

    if not required:
        return _prior_turn_reference_none()

    if operation == "none":
        operation = "reference_resolution"

    if status == "none":
        status = "unresolved"

    if status == "resolved" and not hints:
        status = "unresolved"

    return {
        "required": True,
        "operation": operation,
        "status": status,
        "confidence": confidence,
        "hints": hints,
    }


def _resolve_history_text(metadata: Dict[str, Any]) -> str:
    """Return the classifier's transcript section from the shared bundle.

    Reads ``metadata[METADATA_CONTEXT_BUNDLE_KEY]``, projects it for
    the intent classifier role, and extracts the
    ``recent_transcript`` section produced by the shared serializer —
    the same verbatim recent-turn window every other prompt-authoritative
    role observes.

    After the Phase 5 authority cutover the bundle is required: both the
    ``LangGraphContextBuilder.build_runtime_config`` and
    ``facade_helpers.build_metadata`` populate it at turn setup. A
    missing bundle is an invariant violation and raises
    ``RuntimeError`` rather than silently falling back.
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, dict):
        raise RuntimeError(
            "intent_classifier: metadata[context_bundle] is missing; "
            "the hot-path ConversationContextBundle must be populated "
            "upstream (see LangGraphContextBuilder.build_runtime_config)."
        )

    projection = project_for_intent_classifier(bundle)
    # Unified conversation stream: the projection already carries the
    # in-flight user turn (presence-based), so the shared serializer
    # appends it as the final ``<turn … latest=true>…</turn>`` block
    # and the classifier prompt reads one timeline without a separate
    # ``{message}`` slot.
    section_map = serialize_projection_to_section_map(projection)
    return section_map.get(SECTION_RECENT_TRANSCRIPT, "") or ""


def _collect_hints(metadata: Dict[str, Any]) -> Dict[str, Any]:
    hints = metadata.get("intent_hints") or {}
    return {
        "tool_hints": hints.get("tool_hints") or [],
        "targets": hints.get("targets") or [],
        "eligible_routes": metadata.get("eligible_routes") or [],
        "risk_flags": metadata.get("risk_flags") or [],
    }


def _canonicalize_routing_label(raw_label: Any, *, fallback_label: str) -> str:
    """Normalize classifier routing labels into canonical branch labels."""
    fallback = str(fallback_label or "simple_chat").strip().lower()
    if not isinstance(raw_label, str):
        return fallback

    normalized = raw_label.strip().lower()
    if not normalized:
        return fallback

    normalized = normalized.replace("-", "_").replace(" ", "_")
    return _ROUTING_LABEL_ALIASES.get(normalized, fallback)


def _parse_classifier_response(raw: str) -> Dict[str, Any]:
    candidates: List[str] = []
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            candidates.append(stripped)

            fence_match = re.search(
                r"```(?:json)?\s*(\{.*?\})\s*```",
                stripped,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if fence_match:
                candidates.insert(0, fence_match.group(1).strip())

            first_brace = stripped.find("{")
            last_brace = stripped.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                candidates.append(stripped[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    logger.debug("Intent classifier returned non-JSON response: %s", raw)
    return {}


def _build_intent_signals(
    parsed: Dict[str, Any],
    *,
    fallback_label: str,
) -> IntentSignals:
    raw_label = parsed.get("label")
    label = _canonicalize_routing_label(raw_label, fallback_label=fallback_label)
    confidence = parsed.get("confidence")
    capabilities = parsed.get("suggested_capabilities") or []
    risk_flags = parsed.get("risk_flags") or []

    # NOTE: Routing labels (simple_chat, direct_executor, plan_executor) are NOT
    # pentesting capabilities - they control graph routing. Do NOT normalize them.
    # Only normalize suggested_capabilities to CapabilityType enum values.

    logger.info(
        "[INTENT] Classifier label raw='%s' canonical='%s', confidence=%s",
        raw_label,
        label,
        confidence,
    )

    # Validate and normalize suggested capabilities (these ARE pentesting capabilities)
    normalized_suggested = []
    for cap in capabilities:
        try:
            normalized = CapabilityType.from_intent(str(cap))
            normalized_suggested.append(normalized.value)
            logger.debug(
                f"[CAPABILITY] Normalized suggested capability '{cap}' → '{normalized.value}'"
            )
        except Exception as exc:
            logger.debug(
                f"[CAPABILITY] Could not normalize suggested capability '{cap}': {exc}"
            )

    # Build metadata: intent_capability comes from normalized suggestions, not routing label
    metadata = {
        "raw_classifier_output": parsed,
        "raw_classifier_label": raw_label,
        "reasoning": parsed.get("reasoning"),
    }

    # Optional: requested output format for the final assistant response (NOT tool execution).
    requested_output_format = parsed.get("requested_output_format")
    if isinstance(requested_output_format, str):
        normalized_format = requested_output_format.strip().lower()
        if normalized_format in {"json", "csv", "markdown"}:
            metadata["requested_output_format"] = normalized_format
    elif requested_output_format is None:
        # Explicit null is valid; do nothing.
        pass

    # Set intent_capability to first normalized suggestion (if any)
    if normalized_suggested:
        metadata["intent_capability"] = normalized_suggested[0]
        logger.debug(
            f"[CAPABILITY] Primary intent capability: '{normalized_suggested[0]}'"
        )

    return IntentSignals(
        classifier_label=str(label),
        classifier_confidence=float(confidence)
        if isinstance(confidence, (int, float))
        else None,
        heuristic_labels=[],
        suggested_capabilities=normalized_suggested
        if normalized_suggested
        else list(map(str, capabilities)),
        safety=None,
        risk_flags=list(map(str, risk_flags)),
        metadata=metadata,
    )


_FORCED_EXECUTION_MODE_MAP: Dict[str, ExecutionMode] = {
    "deep_reasoning": ExecutionMode.DEEP_REASONING,
    "normal_chat": ExecutionMode.NORMAL_CHAT,
    "simple_tool_execution": ExecutionMode.SIMPLE_TOOL,
}


def _apply_route_policy_to_runtime_mode(runtime_config: LangGraphRuntimeConfig) -> None:
    """Force `runtime_config.execution_mode` to the route-policy target.

    Phase 3 Task 3.1: `execution_route_policy` is the single durable
    forced-route authority for user-surface tier selection (`plan` /
    `chat`). When present, it overrides whatever branch the classifier
    label implied — the classifier label remains the authority for
    interpretation briefs but not for backend branch selection.

    When no policy is present this is a no-op: the classifier-derived
    mode stands. When the policy's `forced_execution_mode` is
    unrecognized (defensive branch for a miswired policy payload), the
    runtime mode is left untouched and a marker is recorded so the
    failure is auditable.
    """
    metadata = runtime_config.metadata
    policy = metadata.get("execution_route_policy")
    if not isinstance(policy, dict):
        return
    forced_raw = str(policy.get("forced_execution_mode") or "").strip().lower()
    forced_mode = _FORCED_EXECUTION_MODE_MAP.get(forced_raw)
    if forced_mode is None:
        metadata["execution_route_policy_applied"] = False
        metadata["execution_route_policy_error"] = (
            f"unrecognized forced_execution_mode={forced_raw!r}"
        )
        return
    runtime_config.execution_mode = forced_mode
    metadata["execution_route_policy_applied"] = True


def _apply_direct_executor_runtime_mode(
    runtime_config: LangGraphRuntimeConfig,
) -> None:
    """Route direct-executor labels to the simple-tool graph."""
    runtime_config.execution_mode = ExecutionMode.SIMPLE_TOOL


def _resolve_classifier_output_budget(
    call_settings: RoleCallSettings,
    requested_max_tokens: int,
) -> int:
    """Return provider-safe intent-classifier output budget."""
    provider = str(call_settings.provider or "").strip().lower()
    if provider == ANTHROPIC_PROVIDER_ID:
        return min(int(requested_max_tokens), ANTHROPIC_INTENT_CLASSIFIER_MAX_TOKENS)
    return int(requested_max_tokens)


def resolve_intent_classifier_context_limit(
    call_settings: RoleCallSettings,
) -> int:
    """Resolve the strict hard context limit for the classifier's selected model."""
    return resolve_context_window_tokens(
        ProviderModelRef(call_settings.provider, call_settings.model)
    )


@dataclass(frozen=True, slots=True)
class IntentClassifierRequest:
    """Complete, resolved input for one intent-classifier LLM call."""

    call_settings: RoleCallSettings
    system_prompt: str
    user_prompt: str
    temperature: float
    max_tokens: int
    structured_output: Dict[str, Any]


def build_intent_classifier_request(
    *,
    metadata: Dict[str, Any],
    call_settings: RoleCallSettings,
    environment: str,
    temperature: float,
    max_tokens: int,
) -> IntentClassifierRequest:
    """Build the exact side-effect-free request consumed by the classifier."""
    hints = _collect_hints(metadata)
    history_text = _resolve_history_text(metadata)
    llm_facing_routes = [
        llm_facing_route_label(route) for route in hints["eligible_routes"]
    ]
    return IntentClassifierRequest(
        call_settings=call_settings,
        system_prompt=CLASSIFIER_SYSTEM_PROMPT,
        user_prompt=build_classifier_user_prompt(
            history=history_text,
            tool_hints=hints["tool_hints"],
            targets=hints["targets"],
            eligible_routes=llm_facing_routes,
            risk_flags=hints["risk_flags"],
            environment=environment,
            execution_route_policy=metadata.get("execution_route_policy"),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        structured_output=INTENT_CLASSIFIER_STRUCTURED_OUTPUT,
    )


def _set_execution_mode_from_hints(runtime_config: LangGraphRuntimeConfig) -> None:
    metadata = runtime_config.metadata
    forced = str(metadata.get("forced_capability") or "").lower()
    if forced == "respond_only":
        runtime_config.execution_mode = ExecutionMode.NORMAL_CHAT
        return
    if forced == "simple_tool_execution":
        runtime_config.execution_mode = ExecutionMode.SIMPLE_TOOL
        # forced_capability outranks route policy per Task 1.3 — do not
        # apply route policy here.
        return
    if forced == "deep_reasoning":
        runtime_config.execution_mode = ExecutionMode.DEEP_REASONING
        return

    if runtime_config.execution_mode != ExecutionMode.NORMAL_CHAT:
        _apply_route_policy_to_runtime_mode(runtime_config)
        return

    runtime_config.execution_mode = ExecutionMode.NORMAL_CHAT
    # Phase 3 Task 3.1: route policy overrides conservative fallback too
    # so skip paths (missing runtime services, timeout, llm_error, ...) still
    # honor the user-surface tier selection.
    _apply_route_policy_to_runtime_mode(runtime_config)


@dataclass(slots=True)
class IntentClassifierResult:
    signals: IntentSignals
    reasoning: str
    usage: Optional["UsageData"] = (
        None  # Token usage from classification LLM call (Phase 7)
    )


class IntentClassifier:
    """Invoke LLM intent classifier with graceful fallbacks."""

    def __init__(
        self,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        client_timeout: float = LLM_TIMEOUT_INTENT_CLASSIFIER_SEC,
        client_factory: Optional[Callable[[RoleCallSettings], LLMClient]] = None,
        model_role_registry: Optional[ModelRoleRegistry] = None,
    ) -> None:
        self._temperature = temperature
        self._max_tokens = (
            max_tokens if max_tokens is not None else LIMITS.intent_classifier
        )
        self._client_timeout = client_timeout
        self._client_factory = client_factory
        self._model_role_registry = model_role_registry or ModelRoleRegistry()

    def resolve_call_settings(
        self,
        runtime_config: LangGraphRuntimeConfig,
    ) -> RoleCallSettings:
        """Resolve the existing role-policy settings for this classifier turn."""
        chat_inputs = runtime_config.chat_inputs
        runtime_selection = runtime_config.llm_runtime_selection
        return self._model_role_registry.resolve_call_settings(
            ROLE_INTENT_CLASSIFIER,
            conversation_provider=(
                runtime_selection.get("provider")
                if isinstance(runtime_selection, dict)
                else chat_inputs.provider
            ),
            conversation_model=(
                runtime_selection.get("model")
                if isinstance(runtime_selection, dict)
                else chat_inputs.model
            ),
            reasoning_effort=chat_inputs.reasoning_effort,
        )

    def prepare_request(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        call_settings: RoleCallSettings,
    ) -> IntentClassifierRequest:
        """Build the exact resolved request used for accounting and invocation."""
        env_section = _load_environment_section(
            runtime_config.chat_inputs.task_id,
            runtime_config.metadata.get("environment_info"),
        )
        return build_intent_classifier_request(
            metadata=runtime_config.metadata,
            call_settings=call_settings,
            environment=env_section,
            temperature=self._temperature,
            max_tokens=_resolve_classifier_output_budget(
                call_settings,
                self._max_tokens,
            ),
        )

    async def enrich_runtime_config(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        call_settings: Optional[RoleCallSettings] = None,
        prepared_request: Optional[IntentClassifierRequest] = None,
    ) -> Optional[IntentClassifierResult]:
        metadata = runtime_config.metadata
        metadata["intent_prior_turn_reference"] = _prior_turn_reference_none()
        metadata["intent_target_continuity"] = {
            "status": "disallow",
            "evidence": None,
            "source": "classifier",
        }
        metadata.setdefault(
            "request_contract",
            _build_request_contract({}, runtime_config.chat_inputs.message),
        )
        start_time = time.perf_counter()
        hints = _collect_hints(metadata)
        eligible_routes = hints["eligible_routes"]
        forced_capability = metadata.get("forced_capability")

        if forced_capability:
            _set_execution_mode_from_hints(runtime_config)
            metadata["intent_classifier_skipped"] = "forced_capability"
            safe_inc("intent_classifier_skipped")
            logger.warning(
                "[INTENT] Classifier skipped for task %s in %.2f ms (forced_capability)",
                runtime_config.chat_inputs.task_id,
                (time.perf_counter() - start_time) * 1000,
            )
            write_intent_brief_seed(metadata)
            return None

        chat_inputs = runtime_config.chat_inputs
        runtime_selection = runtime_config.llm_runtime_selection
        runtime_services = runtime_config.runtime_services
        if not runtime_services and self._client_factory is None:
            _set_execution_mode_from_hints(runtime_config)
            metadata["intent_classifier_skipped"] = "missing_llm_runtime"
            safe_inc("intent_classifier_skipped")
            logger.warning(
                "[INTENT] Classifier skipped for task %s in %.2f ms (missing_llm_runtime)",
                runtime_config.chat_inputs.task_id,
                (time.perf_counter() - start_time) * 1000,
            )
            write_intent_brief_seed(metadata)
            return None

        call_settings = call_settings or self.resolve_call_settings(runtime_config)
        if (
            prepared_request is not None
            and prepared_request.call_settings != call_settings
        ):
            raise ValueError(
                "prepared intent-classifier request does not match call settings"
            )
        try:
            if runtime_services is None:
                if self._client_factory is None:
                    raise ValueError("missing LLM runtime service")
                client = self._client_factory(call_settings)
            elif not runtime_selection:
                raise ValueError("missing LLM runtime selection")
            else:
                client = runtime_services.client_resolver.get_client(
                    runtime_selection,
                    target=call_settings,
                    runtime_user_id=chat_inputs.user_id,
                    task_id=chat_inputs.task_id,
                    purpose="intent_classifier",
                    resolution_role=ROLE_INTENT_CLASSIFIER,
                    resolution_source=call_settings.source,
                )
        except Exception as exc:
            _set_execution_mode_from_hints(runtime_config)
            metadata["intent_classifier_skipped"] = "client_init_failed"
            metadata["intent_classifier_error"] = str(exc)
            logger.debug("Failed to init LLM client for intent classifier: %s", exc)
            safe_inc("intent_classifier_skipped")
            logger.warning(
                "[INTENT] Classifier skipped for task %s in %.2f ms (client_init_failed)",
                runtime_config.chat_inputs.task_id,
                (time.perf_counter() - start_time) * 1000,
            )
            write_intent_brief_seed(metadata)
            return None

        request = prepared_request or self.prepare_request(
            runtime_config,
            call_settings=call_settings,
        )

        # Use chat_with_usage to capture token usage (Phase 7)
        classifier_usage: Optional["UsageData"] = None
        structured_classifier_output: Optional[Dict[str, Any]] = None
        try:
            llm_start = time.perf_counter()
            if hasattr(client, "chat_with_usage"):
                llm_response = await wait_for_with_timeout(
                    client.chat_with_usage(
                        system_prompt=request.system_prompt,
                        user_prompt=request.user_prompt,
                        temperature=request.temperature,
                        max_tokens=request.max_tokens,
                        structured_output=request.structured_output,
                    ),
                    timeout_sec=self._client_timeout,
                    component="INTENT",
                    operation="classifier_llm_call",
                    logger=logger,
                    task_id=runtime_config.chat_inputs.task_id,
                    outcome="fallback=heuristic_routing",
                    details="skip_reason=timeout",
                )
                response = llm_response.content
                classifier_usage = llm_response.usage
                parsed_payload = getattr(llm_response, "structured_output", None)
                if isinstance(parsed_payload, dict):
                    structured_classifier_output = parsed_payload
            else:
                response = await wait_for_with_timeout(
                    client.chat(
                        system_prompt=request.system_prompt,
                        user_prompt=request.user_prompt,
                        temperature=request.temperature,
                        max_tokens=request.max_tokens,
                        structured_output=request.structured_output,
                    ),
                    timeout_sec=self._client_timeout,
                    component="INTENT",
                    operation="classifier_llm_call",
                    logger=logger,
                    task_id=runtime_config.chat_inputs.task_id,
                    outcome="fallback=heuristic_routing",
                    details="skip_reason=timeout",
                )
                classifier_usage = None
            logger.warning(
                "[INTENT] Classifier LLM call completed for task %s in %.2f ms",
                runtime_config.chat_inputs.task_id,
                (time.perf_counter() - llm_start) * 1000,
            )

            if classifier_usage:
                logger.debug(
                    f"[INTENT] Token usage: {classifier_usage.total_tokens} "
                    f"(prompt={classifier_usage.prompt_tokens}, "
                    f"completion={classifier_usage.completion_tokens})"
                )
        except asyncio.TimeoutError:
            _set_execution_mode_from_hints(runtime_config)
            timeout_message = (
                f"Intent classifier timed out after {self._client_timeout:.2f}s"
            )
            metadata["intent_classifier_error"] = timeout_message
            metadata["intent_classifier_error_type"] = "timeout"
            metadata["intent_classifier_timeout_sec"] = self._client_timeout
            metadata["intent_classifier_skipped"] = "timeout"
            safe_inc("intent_classifier_skipped")
            safe_inc("intent_classifier_timeout")
            logger.warning(
                "[INTENT] Classifier timed out for task %s in %.2f ms "
                "(timeout, timeout_sec=%.2f)",
                runtime_config.chat_inputs.task_id,
                (time.perf_counter() - start_time) * 1000,
                self._client_timeout,
            )
            write_intent_brief_seed(metadata)
            return None
        except LLMRefusalError:
            raise
        except Exception as exc:
            _set_execution_mode_from_hints(runtime_config)
            metadata["intent_classifier_error"] = str(exc)
            metadata["intent_classifier_error_type"] = type(exc).__name__
            metadata["intent_classifier_skipped"] = "llm_error"
            logger.debug("Intent classifier call failed: %s", exc)
            safe_inc("intent_classifier_skipped")
            logger.warning(
                "[INTENT] Classifier failed for task %s in %.2f ms (llm_error)",
                runtime_config.chat_inputs.task_id,
                (time.perf_counter() - start_time) * 1000,
            )
            write_intent_brief_seed(metadata)
            return None

        safe_inc("intent_classifier_runs")
        parse_start = time.perf_counter()
        parsed = (
            structured_classifier_output
            if isinstance(structured_classifier_output, dict)
            else _parse_classifier_response(response)
        )
        logger.warning(
            "[INTENT] Classifier response parsed for task %s in %.2f ms",
            runtime_config.chat_inputs.task_id,
            (time.perf_counter() - parse_start) * 1000,
        )
        reasoning = parsed.get("reasoning")
        signals_start = time.perf_counter()
        signals = _build_intent_signals(parsed, fallback_label="simple_chat")
        logger.warning(
            "[INTENT] Intent signals built for task %s in %.2f ms",
            runtime_config.chat_inputs.task_id,
            (time.perf_counter() - signals_start) * 1000,
        )

        metadata["intent_classifier_raw_response"] = parsed
        metadata["intent_classifier_reasoning"] = (
            reasoning or "Intent classifier executed."
        )
        metadata["intent_classifier_label"] = signals.classifier_label
        # Phase 3 Task 3.2: preserve the raw, uncanonicalized classifier
        # label separately so audit / debug / brief consumers can see
        # exactly what the LLM emitted, even if the canonicalized
        # `intent_classifier_label` and the route-policy-forced
        # `execution_mode` disagree. Never overwrite this value with the
        # forced label — that would lose the signal that the classifier
        # disobeyed.
        raw_label_value = (
            signals.metadata.get("raw_classifier_label")
            if isinstance(signals.metadata, dict)
            else None
        )
        metadata["intent_classifier_raw_label"] = raw_label_value
        # Mark whether the user-surface route policy was applied to the
        # final backend `execution_mode` and where it came from. These
        # markers are flat top-level keys (no nesting) so brief builders
        # and the facade can read them without parsing
        # `execution_route_policy`.
        policy = metadata.get("execution_route_policy")
        if isinstance(policy, dict):
            metadata["intent_classifier_route_forced"] = True
            metadata["intent_classifier_route_force_source"] = (
                f"{policy.get('source', 'agent_mode')}={policy.get('agent_mode', '')}"
            )
        else:
            metadata["intent_classifier_route_forced"] = False
            metadata.pop("intent_classifier_route_force_source", None)
        metadata["intent_signal_cache"] = signals.model_dump()
        metadata["intent_signals"] = signals.model_dump()
        metadata["request_contract"] = _build_request_contract(
            parsed, chat_inputs.message
        )
        target_resolution = _normalize_intent_target_resolution(parsed)
        target_continuity = _normalize_intent_target_continuity(parsed)
        prior_turn_reference = _normalize_prior_turn_reference(parsed)
        metadata["intent_target_resolution"] = target_resolution
        metadata["intent_target_continuity"] = target_continuity
        metadata["intent_prior_turn_reference"] = prior_turn_reference
        write_intent_brief_seed(metadata)
        logger.warning(
            "[INTENT] Classifier total for task %s in %.2f ms",
            runtime_config.chat_inputs.task_id,
            (time.perf_counter() - start_time) * 1000,
        )

        # Merge risk flags and suggested capabilities with existing hints
        if signals.risk_flags:
            combined_flags = set(metadata.get("risk_flags") or [])
            combined_flags.update(signals.risk_flags)
            metadata["risk_flags"] = sorted(combined_flags)

        if signals.suggested_capabilities or signals.classifier_label:
            # Start with existing routes (from heuristics)
            combined_routes = set(eligible_routes or [])

            classifier_route = _classifier_label_to_internal_route(
                signals.classifier_label
            )
            if classifier_route:
                combined_routes.add(classifier_route)

            for capability in signals.suggested_capabilities:
                hinted_route = _classifier_label_to_internal_route(capability)
                if hinted_route:
                    combined_routes.add(hinted_route)
                    continue

                # Try normalizing descriptive capabilities (e.g., "network scanning" -> "scan_ports")
                from backend.services.langgraph_chat.intent.signals import (
                    _normalize_action_type,
                )

                normalized_action = _normalize_action_type(capability)
                if normalized_action:
                    # It's a valid action type (tool capability), so enable tool route
                    combined_routes.add("simple_tool_execution")

            metadata["eligible_routes"] = sorted(combined_routes) or ["normal_chat"]

        hints["classifier_label"] = signals.classifier_label
        hints["classifier_confidence"] = signals.classifier_confidence
        resolved_target = target_resolution.get("resolved_target")
        if (
            target_resolution.get("target_status") == "resolved"
            and isinstance(resolved_target, str)
            and resolved_target.strip()
        ):
            hints["targets"] = [resolved_target.strip()]
        else:
            hints["targets"] = []
        metadata["intent_hints"] = hints

        label_lower = (signals.classifier_label or "").lower()
        if label_lower == "direct_executor":
            _apply_direct_executor_runtime_mode(runtime_config)
        elif label_lower == "plan_executor":
            runtime_config.execution_mode = ExecutionMode.DEEP_REASONING
        elif label_lower == "simple_chat":
            runtime_config.execution_mode = ExecutionMode.NORMAL_CHAT

        # Phase 3 Task 3.1: resolve effective `execution_mode` from the
        # user-surface route policy once, immediately after classifier
        # output is parsed. The facade's `select_branch` then consumes a
        # single already-resolved mode — there is no second route
        # resolver in the facade. This keeps the classifier label as
        # authoritative for turn interpretation while making the
        # user-tier selection authoritative for the backend branch.
        #
        # The raw classifier label is preserved separately (Task 3.2) in
        # `intent_classifier_raw_label` so audit logs and downstream
        # briefs can see classifier disagreement without it changing the
        # executed branch.
        _apply_route_policy_to_runtime_mode(runtime_config)

        return IntentClassifierResult(
            signals=signals,
            reasoning=metadata["intent_classifier_reasoning"],
            usage=classifier_usage,  # Token usage from classification (Phase 7)
        )


__all__ = [
    "IntentClassifier",
    "IntentClassifierRequest",
    "IntentClassifierResult",
    "build_intent_classifier_request",
    "resolve_intent_classifier_context_limit",
]
