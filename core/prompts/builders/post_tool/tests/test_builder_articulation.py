"""Tests for post-tool articulation prompt builder behavior.

These tests keep the articulation prompt's tool context tied to the shared
last-tool section projection instead of a builder-local duplicate formatter.
"""

from __future__ import annotations

from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder


def test_articulation_prompt_renders_shared_last_tool_sections() -> None:
    metadata = {
        "last_tool_result": {
            "parameters": {"target": "10.0.0.5"},
            "was_truncated": True,
            "chars_truncated": 1250,
            "suggest_file_reading": False,
        },
        "last_artifact_path": "artifacts/nmap.txt",
        "last_tool_result_compact_batch": {
            "status": "completed_with_errors",
            "success": False,
            "results": [
                {
                    "tool_id": "nmap.scan",
                    "status": "success",
                    "success": True,
                    "intent": "scan reachable ports",
                    "compact_tool_result": {
                        "summary": "Port 22 is open.",
                        "key_findings": ["ssh open"],
                        "errors": ["partial UDP timeout"],
                        "structured_signals": [{"port": 22, "state": "open"}],
                        "decision_evidence": ["tcp/22 accepted"],
                        "artifact_refs": [
                            {
                                "artifact_id": "art-1",
                                "artifact_kind": "scan_output",
                                "label": "Nmap scan",
                                "path": "artifacts/nmap.txt",
                                "tool_name": "nmap.scan",
                            }
                        ],
                        "lossiness_risk": "low",
                    },
                }
            ],
        },
    }
    interactive = {
        "facts": {
            "message": "Scan the target.",
            "current_goal": "Identify reachable services.",
            "metadata": metadata,
            "selected_tool": "nmap.scan",
        }
    }

    prompt = PostToolReasoningPromptBuilder().build_articulation_user_prompt(
        interactive=interactive,
        synthesized={"tool": "fallback.tool", "summary": "fallback summary"},
        decision_output={
            "next_action": "finalize",
            "action_reasoning": "The scan produced enough evidence.",
            "user_goal_achieved": True,
            "failure_detected": False,
            "retry_suggested": False,
        },
    )

    assert "## Tool Executed\nTool: nmap.scan\nParameters: target=10.0.0.5" in prompt
    assert "## Tool Output Summary\nPort 22 is open." in prompt
    assert "## Batch Tool Results\nbatch_status: completed_with_errors" in prompt
    assert "- nmap.scan: success; intent=scan reachable ports; summary=Port 22 is open." in prompt
    assert "## Key Findings\n• ssh open" in prompt
    assert "## Tool Errors\n• partial UDP timeout" in prompt
    assert "## Structured Signals\n" in prompt
    assert '{"port": 22,"state": "open"}' in prompt
    assert "## Decision Evidence\n• tcp/22 accepted" in prompt
    assert "## Compression Lossiness\nlossiness_risk: low" in prompt
    assert "## Artifact References\n- Nmap scan (artifact_id=art-1)" in prompt
    assert "Saved output path: `artifacts/nmap.txt`" in prompt
    assert "fallback summary" not in prompt
