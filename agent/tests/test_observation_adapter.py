"""Unit tests for the observation adapter node."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.graph.nodes.observation_adapter import adapt_to_observations


@pytest.mark.asyncio
async def test_adapter_respects_pre_streamed_articulation():
    """Adapter should skip streaming when articulation already streamed."""
    articulated_text = "I rescanned the subnet and confirmed only 10.0.0.5 is online."
    state = {
        "facts": {
            "task_id": 5,
            "message": "Run deep scan",
            "capability": "deep_reasoning",
            "metadata": {
                "api_key": "test",
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "Only one host responded",
                    "key_findings": ["10.0.0.5 online"],
                    "vulnerabilities": [],
                    "next_actions": [],
                    "observation_text": articulated_text,
                },
                "articulated_observation_streamed": True,
            },
        },
        "trace": {
            "observations": [articulated_text],
            "reasoning": [],
            "scratchpad": "",
        },
    }

    mock_writer = MagicMock()
    result = await adapt_to_observations(state, writer=mock_writer)

    # Writer should not be called because articulation already streamed
    assert mock_writer.call_count == 0
    # Observation list should not duplicate entries
    assert result["trace"]["observations"].count(articulated_text) == 1


@pytest.mark.asyncio
async def test_adapter_streams_when_articulation_missing():
    """Adapter should emit observation events when articulation not provided."""
    state = {
        "facts": {
            "task_id": 6,
            "message": "Deep reasoning run",
            "capability": "deep_reasoning",
            "metadata": {
                "api_key": "test",
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "Multiple ports open",
                    "key_findings": ["Port 22 open", "Port 80 open"],
                    "vulnerabilities": ["SSH may be outdated"],
                    "next_actions": ["Run version scan"],
                },
            },
        },
        "trace": {
            "observations": [],
            "reasoning": [],
            "scratchpad": "",
        },
    }

    mock_writer = MagicMock()
    result = await adapt_to_observations(state, writer=mock_writer)

    # Writer should be invoked for streaming events
    assert mock_writer.call_count > 0
    event_types = [call[0][0]["type"] for call in mock_writer.call_args_list]
    assert "observation_start" in event_types
    assert "observation_delta" in event_types
    assert "observation_section_end" in event_types
    assert len(result["trace"]["observations"]) == 1

