"""Deterministic safety and context signal collection for LangGraph turns."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from agent.graph.infrastructure.state_models import IntentSignals
from agent.models import ActionType

from backend.services.metrics.utils import safe_inc

SAFE_COMPLETION_CAPABILITY = "respond_only"

SAFETY_PATTERNS: Sequence[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-rf\b", re.IGNORECASE), "dangerous_shell_command"),
    (re.compile(r"\bdrop\s+database\b", re.IGNORECASE), "destructive_database_command"),
    (re.compile(r"\bformat\s+c[:\\]?", re.IGNORECASE), "dangerous_system_command"),
    (re.compile(r"\bshutdown\s+-h\b", re.IGNORECASE), "service_disruption"),
]

SUGGESTED_CAPABILITY_ALIASES: Dict[str, str] = {
    "network scanning": ActionType.SCAN_PORTS.value,
    "network scan": ActionType.SCAN_PORTS.value,
    "port scanning": ActionType.SCAN_PORTS.value,
    "port scan": ActionType.SCAN_PORTS.value,
    "port discovery": ActionType.SCAN_PORTS.value,
    "vulnerability assessment": ActionType.TEST_EXPLOIT.value,
    "vuln assessment": ActionType.TEST_EXPLOIT.value,
    "vulnerability scanning": ActionType.TEST_EXPLOIT.value,
    "brute forcing": ActionType.TEST_EXPLOIT.value,
    "credential attack": ActionType.TEST_EXPLOIT.value,
    "web scanning": ActionType.SCAN_WEB.value,
    "web enumeration": ActionType.SCAN_WEB.value,
    "web enum": ActionType.SCAN_WEB.value,
    "web discovery": ActionType.SCAN_WEB.value,
    "web proxy": ActionType.SCAN_WEB.value,
    "reconnaissance": ActionType.GATHER_INFO.value,
}


@dataclass(slots=True)
class IntentSignalBundle:
    """Convenience wrapper used internally for metadata stitching."""

    signals: IntentSignals
    eligible_routes: List[str]
    intent_hints: Dict[str, object]
    risk_flags: List[str]
    forced_capability: Optional[str] = None
    suggested_capabilities: List[str] = field(default_factory=list)


def _normalize_action_type(value: str | None) -> Optional[str]:
    if not value:
        return None
    lowered = value.strip().lower()
    if not lowered:
        return None

    for action in ActionType:
        if lowered == action.value or lowered == action.name.lower():
            return action.value

    alias_match = SUGGESTED_CAPABILITY_ALIASES.get(lowered)
    if alias_match:
        return alias_match

    if lowered.endswith("ing"):
        singular = lowered[:-3]
        alias_match = SUGGESTED_CAPABILITY_ALIASES.get(singular)
        if alias_match:
            return alias_match

    return None


def _run_safety_checks(message: str) -> Optional[str]:
    for pattern, code in SAFETY_PATTERNS:
        if pattern.search(message):
            safe_inc("intent_signal_guardrail_triggered")
            return code
    return None


def collect_intent_signals(
    *,
    message: str,
    history: Sequence[Dict[str, object]] | None = None,
    metadata: Optional[Dict[str, object]] = None,
) -> IntentSignalBundle:
    """Return deterministic non-LLM signals and derived metadata for the turn."""

    safe_inc("intent_signal_runs")
    history = history or []
    metadata = dict(metadata or {})

    force_simple_chat = bool(metadata.get("force_simple_chat"))

    targets: List[str] = []
    tool_hints: List[str] = []
    safety_issue = _run_safety_checks(message)

    suggested_capabilities: List[str] = []
    eligible_routes = {"normal_chat"}
    forced_capability: Optional[str] = None

    risk_flags: List[str] = []
    intent_hints: Dict[str, object] = {
        "targets": targets,
        "tool_hints": tool_hints,
        "history_turns": len(history),
    }

    for route in metadata.get("eligible_routes") or []:
        normalized = _normalize_action_type(str(route))
        if normalized and normalized not in suggested_capabilities:
            suggested_capabilities.append(normalized)

    if safety_issue:
        risk_flags.append(safety_issue)
        intent_hints["safety"] = "restricted"
        intent_hints["safety_reason"] = safety_issue
        suggested_capabilities = [SAFE_COMPLETION_CAPABILITY]
        eligible_routes = {"normal_chat"}
        forced_capability = SAFE_COMPLETION_CAPABILITY
    elif force_simple_chat:
        intent_hints["forced_route"] = "simple_chat"
        forced_capability = SAFE_COMPLETION_CAPABILITY
        suggested_capabilities = []
        eligible_routes = {"normal_chat"}

    signals = IntentSignals(
        classifier_label=None,
        classifier_confidence=None,
        heuristic_labels=[*tool_hints],
        suggested_capabilities=list(suggested_capabilities),
        safety=intent_hints.get("safety"),
        risk_flags=risk_flags,
        metadata={
            "targets": targets,
            "tool_hints": tool_hints,
            "deep_reasoning_requested": False,
        },
    )

    return IntentSignalBundle(
        signals=signals,
        eligible_routes=sorted(eligible_routes),
        intent_hints=intent_hints,
        risk_flags=risk_flags,
        forced_capability=forced_capability,
        suggested_capabilities=list(suggested_capabilities),
    )


def embed_intent_signals(
    metadata: Dict[str, object], bundle: IntentSignalBundle
) -> None:
    """Mutate metadata with signal cache, hints, and derived flags."""

    metadata["intent_signal_cache"] = bundle.signals.model_dump()
    metadata["intent_hints"] = bundle.intent_hints
    metadata["risk_flags"] = bundle.risk_flags
    metadata.setdefault("intent_signals", bundle.signals.model_dump())

    if bundle.forced_capability:
        metadata["forced_capability"] = bundle.forced_capability

    combined_suggestions: List[str] = []

    for value in bundle.signals.suggested_capabilities:
        normalized = _normalize_action_type(value)
        if normalized and normalized not in combined_suggestions:
            combined_suggestions.append(normalized)

    for value in bundle.suggested_capabilities:
        normalized = _normalize_action_type(value)
        if normalized and normalized not in combined_suggestions:
            combined_suggestions.append(normalized)

    existing = metadata.get("suggested_capabilities") or []
    for value in existing:
        normalized = _normalize_action_type(str(value))
        if normalized and normalized not in combined_suggestions:
            combined_suggestions.append(normalized)

    if combined_suggestions:
        metadata["suggested_capabilities"] = combined_suggestions
        metadata["intent_capability_candidates"] = combined_suggestions
        metadata.setdefault("intent_capability", combined_suggestions[0])
        metadata.setdefault("intent_hints", {})
        metadata["intent_hints"].setdefault(
            "suggested_capabilities", combined_suggestions
        )
    else:
        metadata.pop("suggested_capabilities", None)
        metadata.pop("intent_capability_candidates", None)
        metadata.pop("intent_capability", None)


__all__ = ["IntentSignalBundle", "collect_intent_signals", "embed_intent_signals"]
