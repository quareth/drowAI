"""Unit tests for pure chat mode parsing and normalization policy."""

from __future__ import annotations

import pytest

from backend.services.langgraph_chat.contracts import AgentMode, ExecutionMode
from backend.services.langgraph_chat.routing.mode_policy import (
    ModePolicyError,
    normalize_agent_plan_pair,
    parse_agent_mode,
    parse_execution_mode,
)


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        ("normal", ExecutionMode.NORMAL_CHAT),
        ("normal_chat", ExecutionMode.NORMAL_CHAT),
        ("default", ExecutionMode.NORMAL_CHAT),
        ("deep", ExecutionMode.DEEP_REASONING),
        ("deep_reasoning", ExecutionMode.DEEP_REASONING),
        ("dr", ExecutionMode.DEEP_REASONING),
        ("simple_tool", ExecutionMode.SIMPLE_TOOL),
        ("simple_tool_execution", ExecutionMode.SIMPLE_TOOL),
        ("quick_tool", ExecutionMode.SIMPLE_TOOL),
        ("unknown", None),
        (None, None),
    ],
)
def test_parse_execution_mode_aliases(raw_mode: str | None, expected: ExecutionMode | None) -> None:
    assert parse_execution_mode(raw_mode) == expected


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        ("full_access", AgentMode.FULL_ACCESS),
        ("agent_full", AgentMode.FULL_ACCESS),
        ("full", AgentMode.FULL_ACCESS),
        ("agent", AgentMode.AGENT),
        ("plan", AgentMode.PLAN),
        ("chat", AgentMode.CHAT),
        ("unknown", None),
        (None, None),
    ],
)
def test_parse_agent_mode_aliases(raw_mode: str | None, expected: AgentMode | None) -> None:
    assert parse_agent_mode(raw_mode) == expected


def test_normalize_agent_plan_pair_legacy_plan_collapses_to_agent_overlay() -> None:
    agent_mode, plan_mode = normalize_agent_plan_pair(
        agent_mode=AgentMode.PLAN,
        plan_mode=None,
    )
    assert agent_mode == AgentMode.AGENT
    assert plan_mode is True


def test_normalize_agent_plan_pair_rejects_chat_plus_plan_overlay() -> None:
    with pytest.raises(ModePolicyError, match="mutually exclusive"):
        normalize_agent_plan_pair(
            agent_mode=AgentMode.CHAT,
            plan_mode=True,
        )
