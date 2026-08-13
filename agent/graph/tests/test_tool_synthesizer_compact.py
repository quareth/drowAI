"""Tests for compact-first behavior in the tool synthesizer node."""

from __future__ import annotations

import pytest

from agent.graph.nodes.tool_synthesizer import synthesize_tool_output
from agent.graph.state import InteractiveState
from core.prompts.builders.post_tool.evidence import register_runtime_compact_evidence


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


@pytest.mark.asyncio
async def test_synthesizer_allows_runtime_only_compact_evidence_without_persisting_it() -> None:
    batch_id = "tb-runtime-only-synthesizer"
    compact = {
        "schema_version": "2.0",
        "tool": "shell.write_stdin",
        "status": "success",
        "success": True,
        "summary": "Created /workspace/boris.txt.",
        "key_findings": [],
        "errors": [],
        "report_recommendations": [],
        "structured_signals": [],
        "decision_evidence": [],
        "lossiness_risk": "low",
    }
    register_runtime_compact_evidence(
        {
            "tool_batch_id": batch_id,
            "status": "completed",
            "success": True,
            "results": [
                {
                    "tool_call_id": "tc-runtime-only",
                    "tool_id": "shell.write_stdin",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": compact,
                }
            ],
        },
        single_compact=compact,
    )
    state = _build_state({"tool_batch_id": batch_id})

    update = await synthesize_tool_output(state)
    updated = InteractiveState.from_mapping(update)

    assert updated.facts.metadata == {"tool_batch_id": batch_id}
    assert updated.trace.reasoning == []
