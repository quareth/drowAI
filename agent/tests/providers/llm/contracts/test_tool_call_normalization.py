"""Tests for fail-closed normalization of content-encoded LLM tool calls."""

from __future__ import annotations

import pytest

from agent.providers.llm.contracts.tool_call_normalization import (
    ContentEncodedToolCallError,
    normalize_content_encoded_tool_calls,
)
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec


def _tool_spec(
    name: str = "tool__network_nmap",
    *,
    required: tuple[str, ...] = ("target",),
) -> FunctionToolSpec:
    """Build one provider-neutral function contract for normalization tests."""

    return FunctionToolSpec(
        tool_id="information_gathering.network_discovery.nmap",
        name=name,
        description="Run a bounded network scan",
        parameters_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "ports": {"type": "string"},
            },
            "required": list(required),
            "additionalProperties": False,
        },
    )


def test_normalizes_namespaced_single_content_call() -> None:
    """A recognized leaked function namespace becomes one canonical call."""

    calls = normalize_content_encoded_tool_calls(
        '{"name":"functions.tool__network_nmap",'
        '"arguments":{"target":"127.0.0.1","ports":"5432"}}',
        tools=[_tool_spec()],
    )

    assert calls is not None
    assert len(calls) == 1
    assert calls[0].id == "content_tool_call_1"
    assert calls[0].name == "tool__network_nmap"
    assert calls[0].arguments == '{"target":"127.0.0.1","ports":"5432"}'


def test_normalizes_parallel_content_call_envelope_in_order() -> None:
    """Multiple encoded calls preserve provider order and separate arguments."""

    calls = normalize_content_encoded_tool_calls(
        """
        {
          "tool_calls": [
            {
              "type": "function",
              "function": {
                "name": "functions.tool__network_nmap",
                "arguments": "{\\"target\\":\\"127.0.0.1\\",\\"ports\\":\\"80\\"}"
              }
            },
            {
              "name": "tool__network_nmap",
              "arguments": {"target": "127.0.0.1", "ports": "443"}
            }
          ]
        }
        """,
        tools=[_tool_spec()],
    )

    assert calls is not None
    assert [call.id for call in calls] == [
        "content_tool_call_1",
        "content_tool_call_2",
    ]
    assert [call.name for call in calls] == [
        "tool__network_nmap",
        "tool__network_nmap",
    ]
    assert [call.arguments for call in calls] == [
        '{"target":"127.0.0.1","ports":"80"}',
        '{"target":"127.0.0.1","ports":"443"}',
    ]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            '{"name":"functions.tool__unknown",'
            '"arguments":{"target":"127.0.0.1"}}',
            "unknown_tool_name",
        ),
        (
            '{"name":"tool__network_nmap","arguments":{"ports":"5432"}}',
            "arguments_schema_validation",
        ),
        (
            '{"name":"tool__network_nmap",'
            '"arguments":{"target":"127.0.0.1"},"execute":true}',
            "unsupported_call_shape",
        ),
        ('{"tool_calls":[]}', "empty_tool_calls"),
    ],
)
def test_rejects_unsafe_or_invalid_content_envelopes(content: str, reason: str) -> None:
    """Unknown tools, invalid arguments and ambiguous shapes fail closed."""

    with pytest.raises(ContentEncodedToolCallError) as exc_info:
        normalize_content_encoded_tool_calls(content, tools=[_tool_spec()])

    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    "content",
    [
        "I cannot call that tool.",
        "```json\n{\"name\":\"tool__network_nmap\",\"arguments\":{}}\n```",
        "",
    ],
)
def test_ignores_non_envelope_assistant_content(content: str) -> None:
    """Ordinary prose and Markdown never become executable calls."""

    assert normalize_content_encoded_tool_calls(content, tools=[_tool_spec()]) is None
