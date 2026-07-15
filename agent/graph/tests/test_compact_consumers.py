"""Integration tests for compact-tool-output consumer migration.

This module verifies consumer-facing behavior after compact envelope adoption:
- tool synthesizer consumes compact payloads without LLM fallback
- failure detection prefers compact error fields
- prompt builders render compact summaries and avoid raw excerpts
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.graph.nodes.post_tool_reasoning.core.failure_detection import (
    build_failure_context_from_state,
    detect_failure,
)
from agent.graph.nodes.tool_synthesizer import synthesize_tool_output
from agent.graph.state import InteractiveState
from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from core.prompts.builders.simple_tool import SimpleToolPromptBuilder


def _build_state(metadata: dict[str, Any]) -> InteractiveState:
    return InteractiveState.from_mapping(
        {
            "facts": {
                "task_id": 7,
                "message": "Validate compact output consumers",
                "selected_tool": "nmap",
                "metadata": metadata,
            },
            "trace": {"reasoning": []},
        }
    )


@pytest.mark.asyncio
async def test_tool_synthesizer_uses_compact_envelope_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShouldNotBeCalledProcessor:  # pragma: no cover - guard rail only
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("UniversalToolProcessor must not be constructed in compact path")

    monkeypatch.setattr(
        "agent.context.tool_processor.UniversalToolProcessor",
        ShouldNotBeCalledProcessor,
    )

    state = _build_state(
        {
            "last_tool_result": {
                "tool": "nmap",
                "status": "success",
                "success": True,
                "exit_code": 0,
                "stdout": "legacy raw output that should be ignored",
                "stderr": "legacy stderr",
            },
            "last_tool_result_compact": {
                "schema_version": "2.0",
                "tool": "nmap",
                "status": "failed",
                "success": False,
                "exit_code": 124,
                "summary": "Scan timed out while probing host.",
                "key_findings": ["Host is reachable."],
                "errors": ["connection timeout after 10s"],
                "report_recommendations": ["Retry with a longer timeout budget."],
                "structured_signals": [
                    {"type": "error_context", "message": "connection timeout after 10s"}
                ],
                "decision_evidence": ["connection timeout after 10s"],
                "lossiness_risk": "medium",
                "artifact_refs": [],
                "compression": {"source": "llm"},
            },
        }
    )

    update = await synthesize_tool_output(state)
    updated = InteractiveState.from_mapping(update)
    synthesized = updated.facts.metadata["synthesized_output"]

    assert synthesized["summary"] == "Scan timed out while probing host."
    assert synthesized["key_findings"] == ["Host is reachable."]
    assert synthesized["vulnerabilities"] == ["connection timeout after 10s"]
    assert synthesized["next_actions"] == ["Retry with a longer timeout budget."]
    assert synthesized["structured_signals"] == [
        {"type": "error_context", "message": "connection timeout after 10s"}
    ]
    assert synthesized["decision_evidence"] == ["connection timeout after 10s"]
    assert synthesized["lossiness_risk"] == "medium"
    assert synthesized["status"] == "failed"
    assert synthesized["success"] is False


def test_failure_detection_prefers_compact_errors() -> None:
    state = _build_state(
        {
            "last_tool_result": {
                "status": "success",
                "success": True,
                "exit_code": 0,
                "stdout": "legacy success output",
                "stderr": "",
            },
            "synthesized_output": {
                "summary": "legacy synthesized summary",
                "key_findings": ["legacy finding"],
                "success": True,
                "status": "success",
            },
            "last_tool_result_compact": {
                "status": "failed",
                "success": False,
                "exit_code": 124,
                "summary": "Compact summary indicates timeout.",
                "key_findings": ["target reachable before timeout"],
                "errors": ["connection timeout after 10s"],
            },
        }
    )

    context = build_failure_context_from_state(state)
    failure_detected, failure_category = detect_failure(context)

    assert context.status == "failed"
    assert context.success_flag is False
    assert context.stderr == "connection timeout after 10s"
    assert context.summary == "Compact summary indicates timeout."
    assert failure_detected is True
    assert failure_category == "timeout"


def test_post_tool_prompt_renders_compact_fields_without_raw_excerpts() -> None:
    builder = PostToolReasoningPromptBuilder()
    state = _build_state(
        {
            "last_tool_result": {
                "parameters": {"target": "127.0.0.1"},
                "stdout_excerpt": "legacy stdout excerpt should never appear",
                "stderr_excerpt": "legacy stderr excerpt should never appear",
                "stdout": "legacy full stdout should never appear",
                "stderr": "legacy full stderr should never appear",
            },
            "last_tool_result_compact": {
                "summary": "Compact summary from compressor.",
                "key_findings": ["22/tcp open ssh", "80/tcp open http"],
                "errors": ["timeout while probing udp/161"],
                "report_recommendations": ["Run service version detection."],
                "structured_signals": [
                    {"type": "service", "port": 22, "service": "ssh"},
                    {"type": "service", "port": 80, "service": "http"},
                ],
                "decision_evidence": ["22/tcp open ssh"],
                "lossiness_risk": "low",
            },
        }
    )

    prompt = builder.build_user_prompt(
        state,
        synthesized={"tool": "nmap"},
    )

    assert "## Tool Output Summary\nCompact summary from compressor." in prompt
    assert "## Key Findings\n• 22/tcp open ssh" in prompt
    assert "## Tool Errors\n• timeout while probing udp/161" in prompt
    assert "## Structured Signals" in prompt
    assert "\"type\": \"service\"" in prompt
    assert "## Decision Evidence\n• 22/tcp open ssh" in prompt
    assert "lossiness_risk: low" in prompt
    assert "## Report Recommendations" not in prompt
    assert "Raw Output Excerpt" not in prompt
    assert "legacy stdout excerpt should never appear" not in prompt
    assert "legacy stderr excerpt should never appear" not in prompt


def test_deep_reasoning_tool_summary_prompt_uses_compact_payload() -> None:
    builder = DeepReasoningPromptBuilder()
    prompt = builder.build_tool_summary_prompt(
        {
            "tool": "nmap",
            "summary": "legacy summary",
            "key_findings": ["legacy finding"],
            "errors": ["legacy error"],
            "compact_tool_result": {
                "summary": "Compact DR summary.",
                "key_findings": ["Open port 443", "TLS enabled"],
                "errors": ["No banner from 8443/tcp"],
                "structured_signals": [{"type": "service", "port": 443, "service": "https"}],
                "decision_evidence": [],
                "lossiness_risk": "low",
            },
        }
    )

    assert "Open port 443" in prompt
    assert "TLS enabled" in prompt
    assert "No banner from 8443/tcp" in prompt
    assert "legacy finding" not in prompt


def test_simple_tool_summary_prompt_uses_compact_payload() -> None:
    builder = SimpleToolPromptBuilder()
    prompt = builder.build_tool_summary_prompt(
        {
            "summary": "legacy summary",
            "errors": ["legacy error"],
            "compact_tool_result": {
                "summary": "Compact simple-tool summary.",
                "errors": ["Permission denied on raw socket"],
                "status": "failed",
            },
        }
    )

    assert "Compact simple-tool summary." in prompt
    assert "stderr: Permission denied on raw socket" in prompt
    assert "Status: failed" in prompt
    assert "legacy summary" not in prompt
