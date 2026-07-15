"""Input-contract tests for the synthesis graph node."""

from __future__ import annotations

import pytest

from agent.graph.nodes.synthesis import synthesis_node
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.providers.llm.core.exceptions import LLMConfigurationError


@pytest.mark.asyncio
async def test_synthesis_node_accepts_interactive_state_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct ``InteractiveState`` callers should not crash during prompt assembly."""

    def _raise_no_llm(*_args: object, **_kwargs: object) -> None:
        raise LLMConfigurationError("missing test LLM")

    monkeypatch.setattr(
        "agent.graph.nodes.synthesis.resolve_llm_client",
        _raise_no_llm,
    )
    interactive = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="Enumerate services on 10.0.0.5",
            iterations=1,
        ),
        trace=TraceState(),
    )

    result = await synthesis_node(interactive, context=None)
    updated = InteractiveState.from_mapping(result)

    assert "reasoning loop" in updated.trace.final_text.lower()
    assert "Reasoning loop detected - synthesizing findings" in updated.trace.reasoning
