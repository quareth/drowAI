"""Contract tests for the unified finalizer prompt assembly.

These tests assert structural invariants of the unified
``build_finalize_prompts`` output and locks the simple-tool baseline via
golden-file snapshot. The DR path is exercised separately to guarantee
capability-conditional sections render correctly.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from agent.graph.utils import iteration_memory as _iteration_memory
from core.prompts.builders.finalize import (
    ADDENDUM_ANALYST,
    ADDENDUM_DR,
    ADDENDUM_RETRY,
    SYSTEM_BASE,
    build_finalize_prompts,
)
from core.prompts.tests._golden import assert_golden


def _render_messages(system_prompt: str, user_prompt: str) -> str:
    return (
        json.dumps(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_finalize_results_prompt_contract_synthesizer_fallback() -> None:
    system_prompt, user_prompt = build_finalize_prompts(
        user_message="Enumerate 10.0.0.5 services",
        synthesized={
            "tool": "nmap.scan",
            "summary": "443/tcp open https",
            "key_findings": ["443/tcp open https"],
            "vulnerabilities": [],
            "next_actions": ["Inspect TLS service"],
        },
        last_result={},
        capability="simple_tool_execution",
    )

    # Golden snapshot covers the operator-voice baseline rendering.
    assert_golden(
        "finalize_results_prompt__synthesizer_fallback_messages.json",
        _render_messages(system_prompt, user_prompt),
    )

    # Structural anchors for the four-part operator skeleton.
    assert SYSTEM_BASE.strip() in system_prompt
    assert "## User Request" in user_prompt
    assert "## Tool Summary (nmap.scan)" in user_prompt
    assert "## Action" in user_prompt
    assert "## Recommended Next Action" in user_prompt


def test_finalize_results_prompt_contract_no_intent_classifier_leak() -> None:
    """The unified system prompt must not echo classifier internals."""
    system_prompt, user_prompt = build_finalize_prompts(
        user_message="enumerate target",
        synthesized={"tool": "nmap.scan", "summary": "open"},
        last_result={},
        capability="simple_tool_execution",
    )
    combined = system_prompt.lower() + "\n" + user_prompt.lower()
    assert "intent_classifier_decision" not in combined
    assert "intent classifier decision" not in combined
    # Closer must be the operator-voice instructions, not the legacy summarizer.
    assert "summarize the engagement results above" not in combined


def test_finalize_results_prompt_contract_retry_addendum_present() -> None:
    """Retry addendum is appended only when more than one attempt happened."""
    retry_attempts = [
        {"attempt_number": 0, "tool_id": "nmap.scan", "synthesized_output": {"status": "failed", "summary": "timeout"}},
        {"attempt_number": 1, "tool_id": "nmap.scan", "synthesized_output": {"status": "success", "summary": "open"}},
    ]
    system_prompt, _ = build_finalize_prompts(
        user_message="x",
        synthesized={"tool": "nmap.scan", "summary": "open"},
        retry_attempts=retry_attempts,
    )
    assert ADDENDUM_RETRY.strip() in system_prompt

    # Single-attempt path skips the addendum.
    system_prompt_solo, _ = build_finalize_prompts(
        user_message="x",
        synthesized={"tool": "nmap.scan", "summary": "open"},
        retry_attempts=[retry_attempts[0]],
    )
    assert ADDENDUM_RETRY.strip() not in system_prompt_solo


def test_finalize_results_prompt_contract_analyst_addendum_present() -> None:
    """Analyst addendum activates when candidate findings are supplied."""
    candidate_findings = [
        {
            "kind": "finding.vulnerability_candidate",
            "target": "10.0.0.5:80",
            "subject": "10.0.0.5",
            "assertion_level": "candidate",
            "confidence": 0.35,
            "details": {
                "vulnerability": "AUTHZ-CANDIDATE-EXPOSED-ENDPOINTS",
                "vulnerability_confidence": 0.35,
                "rationale": "Linked operational endpoints may be unauthenticated.",
            },
        }
    ]
    system_prompt, user_prompt = build_finalize_prompts(
        user_message="enum",
        synthesized={"tool": "http.request", "summary": "200 OK"},
        relevant_findings=candidate_findings,
        current_goal="Verify auth on endpoints",
    )
    assert ADDENDUM_ANALYST.strip() in system_prompt
    assert "### Key Findings (analyst-derived)" in user_prompt
    assert "### Vulnerabilities" in user_prompt


def test_finalize_results_prompt_contract_analyst_context_sections() -> None:
    metadata: Dict[str, Any] = {
        "working_memory": {
            "active_decision": {
                "status": "active",
                "next_action": "call_tool",
                "tool_intent": {
                    "description": "Enumerate discovered routes",
                    "target": "10.0.0.5",
                    "focus": "endpoint discovery",
                },
                "effective_next_goal": "Crawl discovered application paths",
            }
        }
    }
    _iteration_memory.append(
        metadata,
        turn_sequence=12,
        source="tool",
        payload={
            "kind": "http_request",
            "target": "http://10.0.0.5/",
            "action": "GET /",
            "status": "success",
            "result": "positive",
            "summary": "Homepage reveals navigation links.",
            "terminal_for_hypothesis": False,
        },
    )
    system_prompt, user_prompt = build_finalize_prompts(
        user_message="Enumerate endpoints on 10.0.0.5",
        synthesized={
            "tool": "information_gathering.web_enumeration.http_request",
            "summary": "HTTP 200 from dashboard with linked routes.",
            "observation_text": "Homepage links indicate additional routes requiring verification.",
            "key_findings": ["HTTP 200 from /"],
            "vulnerabilities": ["placeholder vuln"],
            "next_actions": ["Fallback action"],
        },
        last_result={},
        aggregated_findings={
            "all_findings": ["HTTP 200 from /"],
            "all_vulnerabilities": ["placeholder vuln"],
            "all_actions": ["Fallback action"],
            "retry_narrative": "",
        },
        metadata=metadata,
        capability="simple_tool_execution",
        relevant_findings=[
            {
                "kind": "finding.vulnerability_candidate",
                "target": "10.0.0.5:80",
                "subject": "10.0.0.5",
                "assertion_level": "candidate",
                "confidence": 0.35,
                "details": {
                    "attributes": {
                        "service": "gunicorn",
                        "discovered_paths_from_home": ["/", "/capture", "/netstat"],
                    },
                    "rationale": "Linked operational endpoints may be unauthenticated.",
                    "evidence_refs": [
                        "artifact://http-output#a href=/capture",
                        "artifact://http-output#a href=/netstat",
                    ],
                    "vulnerability": "AUTHZ-CANDIDATE-EXPOSED-ENDPOINTS",
                    "vulnerability_confidence": 0.35,
                },
            }
        ],
        current_goal="Validate discovered endpoints for authentication controls",
        turn_sequence=12,
    )

    assert ADDENDUM_ANALYST.strip() in system_prompt
    assert "## Effective Goal" in user_prompt
    assert "## Active Decision (advisory)" in user_prompt
    assert "## Prior Current-Turn Phase Memory" in user_prompt
    assert "## PTR Analyst Observation" in user_prompt
    assert "### Key Findings (analyst-derived)" in user_prompt
    assert "Raw-tool key_findings (compressed by the synthesizer):" in user_prompt
    assert "### Vulnerabilities" in user_prompt
    assert "### Recommended Actions" in user_prompt


def test_finalize_results_prompt_contract_deep_reasoning_sections() -> None:
    """DR capability activates the DR addendum and DR-only user sections."""
    system_prompt, user_prompt = build_finalize_prompts(
        user_message="ignored-when-DR",
        capability="deep_reasoning",
        plan=["Recon services", "Enumerate web tier", "Identify exploit path"],
        todo_list=[
            {"description": "Verify SSH banner", "status": "pending"},
            {"description": "Capture HTTP banners", "status": "completed"},
        ],
        dr_iteration_records={
            "1": {
                "tool": {
                    "tool": "nmap.scan",
                    "status": "success",
                    "summary": "22/tcp ssh, 80/tcp http",
                },
                "observation": "Two services exposed.",
            }
        },
        observations=["Port 22 likely OpenSSH 8.x", "Port 80 returns nginx 1.18"],
        executed_tools=[
            {"tool": "nmap.scan", "status": "success", "summary": "ports open"},
        ],
        transcript_text='<turn id="1" latest="true">enumerate 10.0.0.5</turn>',
        runtime_state_text="task_state: running",
        targets=["10.0.0.5"],
    )
    assert ADDENDUM_DR.strip() in system_prompt
    # DR mode does not emit the explicit `## User Request` heading; the
    # transcript section carries the in-flight turn instead.
    assert "## User Request" not in user_prompt
    assert "## Conversation" in user_prompt
    assert "## Runtime State" in user_prompt
    assert "## Targets / Scope" in user_prompt
    assert "## Plan" in user_prompt
    assert "## Todo Status" in user_prompt
    assert "## Iterations Overview" in user_prompt
    assert "## Key Observations" in user_prompt
    assert "## Tool Activity" in user_prompt
    # Closer is shared across capabilities.
    assert "## Recommended Next Action" in user_prompt
    assert "summarize the engagement chronologically" not in user_prompt.lower()
