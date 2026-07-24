"""Provider-neutral compatibility harness for DrowAI's real agent contracts.

The harness sends production classifier and tool schemas through an LLM client,
validates returned tool arguments locally, and uses a synthetic tool result for
post-tool reasoning. It never executes a security tool.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.tools.information_gathering.network_discovery.nmap import NmapArgs
from agent.tools.tool_call_specs import build_function_tool_spec_for
from backend.services.langgraph_chat.intent.classifier import (
    IntentClassifierRequest,
    build_intent_classifier_request,
)
from backend.services.langgraph_chat.model_role_registry import RoleCallSettings


NMAP_TOOL_ID = "information_gathering.network_discovery.nmap"


@dataclass(frozen=True, slots=True)
class AgentCompatibilityResult:
    """Non-sensitive evidence returned by one complete compatibility run."""

    intent_label: str
    tool_name: str
    final_answer: str


def build_real_intent_request(
    *,
    provider: str,
    model: str,
) -> IntentClassifierRequest:
    """Build the wired classifier request for a deterministic Nmap user turn."""

    message = "Check whether PostgreSQL port 5432 is open on 127.0.0.1."
    bundle = build_conversation_context_bundle(
        conversation_id="agent-compatibility",
        turn_id="turn-1",
        turn_sequence=1,
        messages=[{"role": "user", "content": message}],
    )
    metadata: dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: bundle,
        "eligible_routes": ["simple_tool_execution", "normal_chat"],
        "intent_hints": {
            "tool_hints": [NMAP_TOOL_ID],
            "targets": ["127.0.0.1"],
            "risk_flags": [],
        },
    }
    return build_intent_classifier_request(
        metadata=metadata,
        call_settings=RoleCallSettings(
            provider=provider,
            model=model,
            reasoning_effort="low",
            source="conversation",
        ),
        environment="",
        temperature=0,
        max_tokens=32_000,
    )


def build_real_nmap_tool_spec():
    """Return DrowAI's production Nmap function schema."""

    return build_function_tool_spec_for(NMAP_TOOL_ID)


def validate_nmap_arguments(arguments: dict[str, Any]) -> NmapArgs:
    """Validate model-generated Nmap arguments without executing Nmap."""

    local_arguments = dict(arguments)
    local_arguments.pop("_builder_intent", None)
    return NmapArgs.model_validate(local_arguments)


def normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Normalize provider-neutral tool arguments from mapping or JSON text."""

    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            return parsed
    raise AssertionError("agent returned invalid tool arguments")


async def run_agent_compatibility_harness(
    client: Any,
    *,
    provider: str,
    model: str,
    tool_choice: str = "required",
    reasoning_effort: str | None = None,
) -> AgentCompatibilityResult:
    """Run classifier, tool generation, local validation, and final reasoning."""

    request = build_real_intent_request(provider=provider, model=model)
    intent = await client.chat_messages_with_usage(
        [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ],
        structured_output=request.structured_output,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        **(
            {"reasoning_effort": reasoning_effort}
            if reasoning_effort is not None
            else {}
        ),
    )
    parsed_intent = intent.structured_output
    if not isinstance(parsed_intent, dict) or not parsed_intent.get("label"):
        raise AssertionError("intent classifier returned no structured label")

    tool_spec = build_real_nmap_tool_spec()
    tool_response = await client.chat_with_tools_with_usage(
        "Select and call the provided tool. Do not answer directly.",
        (
            "Check PostgreSQL port 5432 on 127.0.0.1. The ports field carries "
            "the port; scan_types contains only scan techniques such as -sT."
        ),
        [tool_spec],
        tool_choice=tool_choice,
        max_tokens=4_096,
        temperature=0,
        **(
            {"reasoning_effort": reasoning_effort}
            if reasoning_effort is not None
            else {}
        ),
    )
    if not tool_response.tool_calls:
        raise AssertionError("agent returned no Nmap tool call")
    tool_call = tool_response.tool_calls[0]
    if tool_call.name != tool_spec.name:
        raise AssertionError("agent selected an unexpected tool")
    validate_nmap_arguments(normalize_tool_arguments(tool_call.arguments))

    final = await client.chat_messages_with_usage(
        [
            {
                "role": "system",
                "content": "Answer from the supplied synthetic tool result only.",
            },
            {
                "role": "user",
                "content": (
                    "Synthetic Nmap result (the tool was not executed): "
                    '{"target":"127.0.0.1","port":5432,"state":"closed"}. '
                    "State the result in one sentence."
                ),
            },
        ],
        max_tokens=1_024,
        temperature=0,
    )
    final_answer = str(final.content or "").strip()
    if not final_answer:
        raise AssertionError("post-tool reasoning returned an empty answer")
    return AgentCompatibilityResult(
        intent_label=str(parsed_intent["label"]),
        tool_name=tool_call.name,
        final_answer=final_answer,
    )


def fake_tool_call(*, name: str, arguments: dict[str, Any]) -> Any:
    """Build a tiny normalized tool-call double for harness unit tests."""

    return SimpleNamespace(name=name, arguments=arguments)
