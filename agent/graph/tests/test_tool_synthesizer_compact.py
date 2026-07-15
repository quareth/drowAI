"""Tests for compact-first behavior in the tool synthesizer node."""

from __future__ import annotations

import pytest

from agent.graph.nodes.tool_synthesizer import synthesize_tool_output
from agent.graph.state import InteractiveState


def _build_state(metadata: dict) -> InteractiveState:
    return InteractiveState.from_mapping(
        {
            "facts": {
                "task_id": 1,
                "message": "test",
                "selected_tool": "nmap",
                "metadata": metadata,
            },
            "trace": {"reasoning": []},
        }
    )


@pytest.mark.asyncio
async def test_synthesizer_prefers_compact_envelope_without_api_key() -> None:
    state = _build_state(
        {
            "last_tool_result": {
                "tool": "nmap",
                "status": "success",
                "success": True,
                "exit_code": 0,
            },
            "last_tool_result_compact": {
                "schema_version": "2.0",
                "tool": "nmap",
                "status": "success",
                "success": True,
                "exit_code": 0,
                "summary": "Found open ports.",
                "key_findings": ["22/tcp open ssh", "80/tcp open http"],
                "errors": [],
                "report_recommendations": ["Run service version detection."],
                "structured_signals": [{"type": "service", "port": 22, "service": "ssh"}],
                "decision_evidence": ["22/tcp open ssh"],
                "lossiness_risk": "low",
                "artifact_refs": [],
                "compression": {"source": "llm"},
            },
        }
    )

    update = await synthesize_tool_output(state)
    updated = InteractiveState.from_mapping(update)
    synthesized = updated.facts.metadata["synthesized_output"]

    assert synthesized["summary"] == "Found open ports."
    assert synthesized["key_findings"] == ["22/tcp open ssh", "80/tcp open http"]
    assert synthesized["vulnerabilities"] == []
    assert synthesized["next_actions"] == ["Run service version detection."]
    assert synthesized["structured_signals"] == [{"type": "service", "port": 22, "service": "ssh"}]
    assert synthesized["decision_evidence"] == ["22/tcp open ssh"]
    assert synthesized["lossiness_risk"] == "low"
    assert synthesized["success"] is True
