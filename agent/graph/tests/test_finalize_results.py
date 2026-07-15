"""Tests for retry-aggregation logic and the unified finalizer prompt builder.

These cover the simple-tool half of the unified finalizer:

- ``_deduplicate_findings_across_attempts`` (deduplicates findings/vulns/actions
  across retry attempts, builds a narrative)
- ``_format_retry_narrative`` (canonical retry-history renderer)
- ``build_finalize_prompts`` (capability-aware prompt builder for the unified
  finalizer node)

The DR-half of the unified builder is exercised in
``agent/graph/nodes/tests/test_articulation_and_finalizer_bundle.py``.
"""

from __future__ import annotations

import inspect

import pytest

from agent.graph.nodes._finalize_helpers import (
    _deduplicate_findings_across_attempts as _deduplicate_findings,
)
from agent.graph.nodes.finalizer import _format_retry_narrative
from core.prompts.builders.finalize import build_finalize_prompts


def test_deduplicate_findings_single_attempt():
    """Verify no deduplication when only 1 attempt."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Host is up",
                "key_findings": ["Host responds to ICMP"],
                "vulnerabilities": [],
                "next_actions": ["Run port scan"],
                "status": "success",
                "success": True,
            },
        }
    ]

    result = _deduplicate_findings(retry_attempts)

    assert result["all_findings"] == ["Host responds to ICMP"]
    assert result["all_vulnerabilities"] == []
    assert result["all_actions"] == ["Run port scan"]
    assert result["retry_narrative"] == ""


def test_deduplicate_findings_multiple_attempts():
    """Verify deduplication across 3 attempts with overlapping findings."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Connection refused",
                "key_findings": ["Host unreachable", "Network error"],
                "vulnerabilities": [],
                "next_actions": ["Check firewall"],
                "status": "failed",
                "success": False,
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "traceroute",
            "synthesized_output": {
                "summary": "Route traced",
                "key_findings": ["Host unreachable", "Gateway responds"],
                "vulnerabilities": [],
                "next_actions": ["Check firewall", "Try alternative route"],
                "status": "success",
                "success": True,
            },
        },
        {
            "attempt_number": 2,
            "tool_id": "nmap",
            "synthesized_output": {
                "summary": "Scan complete",
                "key_findings": ["Gateway responds", "Port 80 open"],
                "vulnerabilities": ["Outdated web server"],
                "next_actions": ["Investigate web server"],
                "status": "success",
                "success": True,
            },
        },
    ]

    result = _deduplicate_findings(retry_attempts)

    assert len(result["all_findings"]) == 4
    assert "Host unreachable" in result["all_findings"]
    assert "Network error" in result["all_findings"]
    assert "Gateway responds" in result["all_findings"]
    assert "Port 80 open" in result["all_findings"]

    assert result["all_vulnerabilities"] == ["Outdated web server"]

    assert len(result["all_actions"]) == 3
    assert "Check firewall" in result["all_actions"]

    assert "Attempt 1" in result["retry_narrative"]
    assert "Attempt 2" in result["retry_narrative"]
    assert "Attempt 3" in result["retry_narrative"]


def test_deduplicate_findings_all_failed():
    """Verify handling when all attempts failed."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Connection refused",
                "key_findings": ["Host unreachable"],
                "vulnerabilities": [],
                "next_actions": ["Check network"],
                "status": "failed",
                "success": False,
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "traceroute",
            "synthesized_output": {
                "summary": "Timeout",
                "key_findings": ["Network timeout"],
                "vulnerabilities": [],
                "next_actions": ["Verify connectivity"],
                "status": "failed",
                "success": False,
            },
        },
    ]

    result = _deduplicate_findings(retry_attempts)

    assert len(result["all_findings"]) == 2
    assert "Host unreachable" in result["all_findings"]
    assert "Network timeout" in result["all_findings"]

    assert "failed" in result["retry_narrative"]


def test_deduplicate_findings_empty_findings():
    """Verify handling when attempts have no findings."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "No output",
                "key_findings": [],
                "vulnerabilities": [],
                "next_actions": [],
                "status": "success",
                "success": True,
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "nmap",
            "synthesized_output": {
                "summary": "Scan complete",
                "key_findings": [],
                "vulnerabilities": [],
                "next_actions": [],
                "status": "success",
                "success": True,
            },
        },
    ]

    result = _deduplicate_findings(retry_attempts)

    assert result["all_findings"] == []
    assert result["all_vulnerabilities"] == []
    assert result["all_actions"] == []

    assert "Attempt 1" in result["retry_narrative"]
    assert "Attempt 2" in result["retry_narrative"]


def test_format_retry_narrative():
    """Verify narrative generation with various attempt combinations."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Connection refused",
                "status": "failed",
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "traceroute",
            "synthesized_output": {
                "summary": "Route traced successfully to target host",
                "status": "success",
            },
        },
    ]

    narrative = _format_retry_narrative(retry_attempts)

    assert "Attempt 1: ping (failed)" in narrative
    assert "Attempt 2: traceroute (success)" in narrative

    assert "Route traced successfully to target host" in narrative


def test_format_retry_narrative_single_attempt():
    """Verify narrative is empty for single attempt."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Success",
                "status": "success",
            },
        }
    ]

    narrative = _format_retry_narrative(retry_attempts)

    assert narrative == ""


def test_format_retry_narrative_empty():
    """Verify narrative is empty for empty attempts list."""
    narrative = _format_retry_narrative([])
    assert narrative == ""


def test_build_finalize_prompts_with_retry_attempts():
    """Verify prompt includes retry history + retry-aware system addendum."""
    synthesized = {
        "tool": "nmap",
        "summary": "Scan complete",
        "key_findings": ["Port 80 open"],
        "vulnerabilities": [],
        "next_actions": ["Investigate web server"],
    }

    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Failed",
                "key_findings": [],
                "vulnerabilities": [],
                "next_actions": [],
                "status": "failed",
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "nmap",
            "synthesized_output": synthesized,
        },
    ]

    aggregated_findings = _deduplicate_findings(retry_attempts)

    system_prompt, user_prompt = build_finalize_prompts(
        user_message="Scan the target",
        synthesized=synthesized,
        last_result={},
        retry_attempts=retry_attempts,
        aggregated_findings=aggregated_findings,
        requested_output_format=None,
        capability="simple_tool_execution",
    )

    # System prompt picks up the retry addendum once retry_attempts > 1.
    assert "multiple tool attempts" in system_prompt.lower()

    assert "## Retry History" in user_prompt
    assert "Attempt 1" in user_prompt


def test_build_finalize_prompts_without_retry_attempts():
    """Verify prompt is standard when no retries occurred."""
    synthesized = {
        "tool": "ping",
        "summary": "Host is up",
        "key_findings": ["Host responds"],
        "vulnerabilities": [],
        "next_actions": [],
    }

    system_prompt, user_prompt = build_finalize_prompts(
        user_message="Ping the host",
        synthesized=synthesized,
        last_result={},
        retry_attempts=None,
        aggregated_findings=None,
        requested_output_format=None,
        capability="simple_tool_execution",
    )

    assert "multiple tool attempts" not in system_prompt.lower()

    assert "## Retry History" not in user_prompt
    assert "## Referenced Prior Turns" not in user_prompt


def test_build_finalize_prompts_includes_referenced_prior_turns_when_present():
    """Verify finalizer prompt includes canonical referenced prior turns."""
    synthesized = {
        "tool": "nmap",
        "summary": "Scan complete",
        "key_findings": [],
        "vulnerabilities": [],
        "next_actions": [],
    }

    _, user_prompt = build_finalize_prompts(
        user_message="What did you mean by that earlier note?",
        synthesized=synthesized,
        last_result={},
        retry_attempts=None,
        aggregated_findings=None,
        requested_output_format=None,
        referenced_prior_turns=(
            "Referenced Prior Turns:\n"
            "- Turn 3 (assistant): Canonical assistant note."
        ),
        capability="simple_tool_execution",
    )

    assert "## Referenced Prior Turns" in user_prompt
    assert user_prompt.count("Referenced Prior Turns") == 1
    assert "Canonical assistant note." in user_prompt


def test_build_finalize_prompts_signature_excludes_intent_classifier_decision():
    """Guardrail: the unified builder must NOT accept ``intent_classifier_decision``.

    Removing the JSON leak from the system prompt is a runner_control cutover
    requirement; the parameter being absent from the public signature is the
    cheap, structural guarantee that callers can no longer pipe it through.
    """
    signature = inspect.signature(build_finalize_prompts)
    assert "intent_classifier_decision" not in signature.parameters, (
        "build_finalize_prompts must not expose intent_classifier_decision; "
        "classifier hints belong to resolver metadata, not finalizer prompts."
    )


def test_build_finalize_prompts_aggregates_findings():
    """Verify prompt uses aggregated findings when provided."""
    synthesized = {
        "tool": "nmap",
        "summary": "Scan complete",
        "key_findings": ["Port 80 open"],
        "vulnerabilities": [],
        "next_actions": [],
    }

    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "key_findings": ["Network reachable"],
                "vulnerabilities": [],
                "next_actions": [],
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "nmap",
            "synthesized_output": synthesized,
        },
    ]

    aggregated_findings = {
        "all_findings": ["Network reachable", "Port 80 open"],
        "all_vulnerabilities": [],
        "all_actions": [],
        "retry_narrative": "- Attempt 1: ping\n- Attempt 2: nmap",
    }

    _, user_prompt = build_finalize_prompts(
        user_message="Scan target",
        synthesized=synthesized,
        last_result={},
        retry_attempts=retry_attempts,
        aggregated_findings=aggregated_findings,
        requested_output_format=None,
        capability="simple_tool_execution",
    )

    assert "Network reachable" in user_prompt
    assert "Port 80 open" in user_prompt


def test_aggregation_failure_fallback():
    """Verify _format_retry_narrative still works when used as fallback narrative."""
    retry_attempts = [
        {
            "attempt_number": 0,
            "tool_id": "ping",
            "synthesized_output": {
                "summary": "Failed",
                "key_findings": ["Host unreachable"],
                "vulnerabilities": [],
                "next_actions": [],
                "status": "failed",
            },
        },
        {
            "attempt_number": 1,
            "tool_id": "traceroute",
            "synthesized_output": {
                "summary": "Success",
                "key_findings": ["Route found"],
                "vulnerabilities": [],
                "next_actions": [],
                "status": "success",
            },
        },
    ]

    narrative = _format_retry_narrative(retry_attempts)

    assert "Attempt 1: ping (failed)" in narrative
    assert "Attempt 2: traceroute (success)" in narrative


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
