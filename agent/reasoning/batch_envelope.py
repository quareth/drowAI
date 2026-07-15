"""Project validated planner outputs into the commit envelope shape.

Keeps ``enhanced_planner_impl.py`` thin by housing the small projection that
turns validated parameter maps or already validated ordered calls back into
the minimal shape consumed by
:func:`agent.reasoning.batch_commit.commit_tool_batch`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence


def project_validated_envelope(
    *,
    envelope: Optional[Mapping[str, Any]],
    selected_tools: Sequence[str],
    tool_parameters: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a builder envelope keyed off the validated parameter map.

    The commit is authoritative for which tools execute; builder-listed
    calls without validated parameters are dropped so
    :func:`commit_tool_batch` only sees ready calls.
    """
    if isinstance(envelope, Mapping) and isinstance(envelope.get("tool_calls"), list):
        committed: List[Dict[str, Any]] = []
        for raw_call in envelope.get("tool_calls", []):
            if not isinstance(raw_call, Mapping):
                continue
            tool_id = str(raw_call.get("tool_id") or "").strip()
            if not tool_id or tool_id not in tool_parameters:
                continue
            committed_call: Dict[str, Any] = {
                "tool_id": tool_id,
                "parameters": dict(tool_parameters[tool_id]),
            }
            intent = raw_call.get("intent")
            if isinstance(intent, str) and intent:
                committed_call["intent"] = intent
            committed.append(committed_call)
        if not committed:
            committed = [
                {"tool_id": tid, "parameters": dict(tool_parameters[tid])}
                for tid in selected_tools
                if tid in tool_parameters
            ]
        return {"tool_calls": committed}

    committed = [
        {"tool_id": tid, "parameters": dict(tool_parameters[tid])}
        for tid in selected_tools
        if tid in tool_parameters
    ]
    return {"tool_calls": committed}


def project_validated_envelope_from_calls(
    *,
    envelope: Mapping[str, Any],
    tool_calls: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return an envelope from already validated per-call payloads.

    Unlike :func:`project_validated_envelope`, this helper is keyed by call
    order instead of ``tool_id``. It preserves duplicate tool ids such as two
    separate ``nmap`` calls with different ports.
    """
    committed: List[Dict[str, Any]] = []
    for raw_call in tool_calls:
        if not isinstance(raw_call, Mapping):
            continue
        tool_id = str(raw_call.get("tool_id") or "").strip()
        parameters = raw_call.get("parameters")
        if not tool_id or not isinstance(parameters, Mapping):
            continue
        committed_call: Dict[str, Any] = {
            "tool_id": tool_id,
            "parameters": dict(parameters),
        }
        intent = raw_call.get("intent")
        if isinstance(intent, str) and intent:
            committed_call["intent"] = intent
        committed.append(committed_call)

    return {"tool_calls": committed}


__all__ = ["project_validated_envelope", "project_validated_envelope_from_calls"]
