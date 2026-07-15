"""Regression tests for articulation parameter source selection."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from agent.graph.nodes.tool_articulation import articulate_tool_intent


class _FakeLLMClient:
    """Capture prompts used by articulation without making network calls."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.model = "gpt-4o-mini"

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> Any:
        self.calls.append((system_prompt, user_prompt, dict(kwargs)))
        return type(
            "_Response",
            (),
            {
                "content": "To meet your request, I will execute shell.exec.",
                "usage": None,
            },
        )()


@pytest.mark.asyncio
async def test_articulation_uses_canonical_tool_batch_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Articulation should describe canonical ToolBatch call args."""
    fake_llm = _FakeLLMClient()
    monkeypatch.setattr("agent.graph.nodes.tool_articulation.resolve_llm_client", lambda *_args, **_kwargs: fake_llm)
    monkeypatch.setattr("agent.graph.nodes.tool_articulation.get_stream_writer", lambda: None)

    state = {
        "facts": {
            "task_id": 1,
            "message": "ok run the ls command in working directory",
                "metadata": {
                    "api_key": "test-key",
                    "model": "gpt-4o-mini",
                    "planner_plan": {
                        "tool_batch": {
                            "tool_batch_id": "tb_test",
                            "requested_execution_strategy": "sequential",
                            "tool_calls": [
                                {
                                    "tool_call_id": "tc_test",
                                    "tool_id": "shell.exec",
                                    "parameters": {"command": "ls"},
                                }
                            ],
                        },
                        "execution_strategy": "sequential",
                    },
                },
        },
        "trace": {"reasoning": []},
    }

    await articulate_tool_intent(state)

    assert fake_llm.calls
    _, prompt, _ = fake_llm.calls[0]
    assert "'command': 'ls'" in prompt
    assert "192.168.0.0/24" not in prompt
    assert "timeout_sec" not in prompt


@pytest.mark.asyncio
async def test_articulation_uses_nmap_tool_batch_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Articulation should use nmap params from the ToolBatch manifest."""
    fake_llm = _FakeLLMClient()
    monkeypatch.setattr("agent.graph.nodes.tool_articulation.resolve_llm_client", lambda *_args, **_kwargs: fake_llm)
    monkeypatch.setattr("agent.graph.nodes.tool_articulation.get_stream_writer", lambda: None)

    state = {
        "facts": {
            "task_id": 1,
            "message": "run a quick scan",
                "metadata": {
                    "api_key": "test-key",
                    "model": "gpt-4o-mini",
                    "planner_plan": {
                        "tool_batch": {
                            "tool_batch_id": "tb_test",
                            "requested_execution_strategy": "sequential",
                            "tool_calls": [
                                {
                                    "tool_call_id": "tc_test",
                                    "tool_id": "information_gathering.network_discovery.nmap",
                                    "parameters": {"target": "10.0.0.1", "ports": "80,443"},
                                }
                            ],
                        },
                        "execution_strategy": "sequential",
                    },
                },
            },
            "trace": {"reasoning": []},
        }

    await articulate_tool_intent(state)

    assert fake_llm.calls
    _, prompt, _ = fake_llm.calls[0]
    assert "10.0.0.1" in prompt
    assert "80,443" in prompt
