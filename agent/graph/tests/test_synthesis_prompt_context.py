"""Prompt-context tests for ``build_synthesis_prompt``.

These tests cover the canonical-projection composition introduced in
Phase 1 of the synthesize-shared-context plan. They focus on the
sections that the new builder reads from canonical runtime state and
from the keyword-only context arguments supplied by the wired
``synthesis`` node:

- compact last-tool cluster (Tool Output Summary, Key Findings, Tool
  Errors, Structured Signals, Decision Evidence, Artifact References),
- request contract,
- active decision (only when ``status == "active"``),
- relevant prior findings (only when the caller passes matches),
- section-snapshot phase memory rendered from
  ``metadata["working_memory"]["current_turn_phases"]``,
- runtime turn/phase counters supplied by the node,
- environment context and scope hints,
- the synthesis-only ``## Loop Details`` section conditional rendering,
- the always-on plain-text ``## Your Task`` tail (no JSON schema).

The legacy placeholder-absence assertions remain pinned by
``core/prompts/tests/test_synthesis_legacy_trace_characterization.py``
and are not duplicated here.

Tests use only the public ``build_synthesis_prompt`` API and assert on
section headers and substrings rather than golden snapshots.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from core.prompts.builders.synthesis import build_synthesis_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_state(
    *,
    facts: Mapping[str, Any] | None = None,
    trace: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a minimal ``state`` mapping accepted by ``build_synthesis_prompt``.

    The builder only reads ``state["facts"]`` (which may carry ``metadata``,
    ``plan``, ``todo_list``, ``current_goal``, ``selected_tool``,
    ``tool_parameters``, and ``message``); ``state["trace"]`` is included
    for parity with the think_more characterization fixtures but is
    intentionally ignored by the synthesis builder (legacy trace reads
    were removed in Phase 0).
    """
    state: Dict[str, Any] = {
        "facts": dict(facts) if facts else {"plan": [], "todo_list": []},
        "trace": dict(trace) if trace else {"observations": [], "executed_tools": []},
    }
    return state


def _facts_with_metadata(metadata: Mapping[str, Any], **extra: Any) -> Dict[str, Any]:
    """Build a ``facts`` dict carrying ``metadata`` plus optional fields."""
    facts: Dict[str, Any] = {
        "plan": [],
        "todo_list": [],
        "metadata": dict(metadata),
    }
    facts.update(extra)
    return facts


def _build(state: Mapping[str, Any], **kwargs: Any) -> str:
    """Convenience wrapper around ``build_synthesis_prompt``."""
    return build_synthesis_prompt(state, **kwargs)


# ---------------------------------------------------------------------------
# Empty state and task-tail anchoring
# ---------------------------------------------------------------------------


def test_empty_state_renders_only_task_tail() -> None:
    """No facts/metadata/kwargs collapses to the always-on ``## Your Task`` tail."""
    prompt = _build(_base_state())

    assert "## Your Task" in prompt
    # The task tail keeps the existing graceful-exit framing.
    assert "Generate a graceful final response that:" in prompt

    # No conditional sections.
    for header in (
        "## User Input",
        "## User Goal",
        "## Current Execution Context",
        "## Prior Current-Turn Phase Memory",
        "## Current Focus",
        "## Prior Active Decision (Advisory)",
        "## Relevant Prior Findings",
        "## Container Environment",
        "## Tool Executed",
        "## Request Contract",
        "## Tool Output Summary",
        "## Key Findings",
        "## Tool Errors",
        "## Structured Signals",
        "## Decision Evidence",
        "## Artifact References",
        "## Current Plan",
        "## Todo List",
        "## Scope Hints",
        "## Loop Details",
    ):
        assert header not in prompt, f"unexpected header rendered: {header}"


def test_task_tail_is_plain_text_with_no_json_schema_block() -> None:
    """The synthesis task tail must remain plain text — no JSON code block."""
    prompt = _build(_base_state())

    # No JSON code fence anywhere in the prompt body.
    assert "```json" not in prompt
    # No structured-output schema fields think_more uses.
    assert "Required Response Format" not in prompt
    assert '"reasoning":' not in prompt
    assert "Provide your analysis as valid JSON" not in prompt


# ---------------------------------------------------------------------------
# Plan/todo conditional rendering
# ---------------------------------------------------------------------------


def test_plan_and_todo_render_without_legacy_placeholders() -> None:
    """Plan/todo render with no leakage of the legacy synthesis placeholders."""
    state = _base_state(
        facts={
            "plan": ["Recon the host", "Enumerate services"],
            "todo_list": [{"text": "Check open ports"}],
        }
    )

    prompt = _build(state)

    assert "## Current Plan" in prompt
    assert "1. Recon the host" in prompt
    assert "2. Enumerate services" in prompt

    assert "## Todo List" in prompt
    assert "Check open ports" in prompt

    # Legacy synthesis placeholders must never appear.
    assert "No plan" not in prompt
    assert "No tools were successfully executed" not in prompt
    assert "No observations recorded" not in prompt
    assert "No detailed reasoning recorded" not in prompt
    assert "complete the task" not in prompt
    assert "Reasoning History" not in prompt


# ---------------------------------------------------------------------------
# Cleanup re-pin: trace observations / executed tools are ignored
# ---------------------------------------------------------------------------


def test_trace_observations_and_executed_tools_are_ignored() -> None:
    """``trace.observations`` and ``trace.executed_tools`` never reach the prompt."""
    long_observation = "Q" * 400
    state = _base_state(
        trace={
            "observations": ["obs-A", "obs-B", "obs-C"],
            "executed_tools": [
                {"tool_id": "older.tool", "observation": "older-output"},
                {"tool_id": "nmap.scan", "observation": long_observation},
            ],
        }
    )

    prompt = _build(state)

    for needle in (
        "obs-A",
        "obs-B",
        "obs-C",
        "older.tool",
        "older-output",
        "nmap.scan",
        "Q" * 50,
        "Recent Observations",
        "Last Tool Result",
    ):
        assert needle not in prompt


# ---------------------------------------------------------------------------
# Last-tool compact cluster
# ---------------------------------------------------------------------------


def test_last_tool_compact_cluster_renders_when_compact_present() -> None:
    """Compact last-tool result drives Tool Output Summary, Key Findings,
    Tool Errors, Structured Signals, Decision Evidence, and Artifact References."""
    metadata = {
        "last_tool_result_compact": {
            "summary": "Discovered two open ports on the target host.",
            "key_findings": [
                "tcp/22 ssh open",
                "tcp/80 http open",
            ],
            "errors": ["transient timeout on udp/53"],
            "structured_signals": [
                {"signal": "port_open", "port": 22, "protocol": "tcp"},
            ],
            "decision_evidence": ["scan completed without filtered ports"],
            "artifact_refs": [
                {
                    "artifact_id": "art-001",
                    "label": "nmap raw output",
                    "tool_name": "nmap.scan",
                    "artifact_kind": "scan_log",
                },
            ],
        },
    }
    facts = _facts_with_metadata(
        metadata,
        selected_tool="nmap.scan",
        tool_parameters={"target": "10.0.0.5", "ports": "1-1024"},
    )
    state = _base_state(facts=facts)

    prompt = _build(state)

    assert "## Tool Executed" in prompt
    assert "Tool: nmap.scan" in prompt
    assert "target=10.0.0.5" in prompt
    assert "ports=1-1024" in prompt

    assert "## Tool Output Summary" in prompt
    assert "Discovered two open ports" in prompt

    assert "## Key Findings" in prompt
    assert "tcp/22 ssh open" in prompt
    assert "tcp/80 http open" in prompt

    assert "## Tool Errors" in prompt
    assert "transient timeout on udp/53" in prompt

    assert "## Structured Signals" in prompt
    assert "port_open" in prompt

    assert "## Decision Evidence" in prompt
    assert "scan completed without filtered ports" in prompt

    assert "## Artifact References" in prompt
    assert "art-001" in prompt
    assert "nmap raw output" in prompt


def test_last_tool_cluster_omitted_when_metadata_empty() -> None:
    """Without last-tool data, none of the compact-cluster sections render."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state)

    for header in (
        "## Tool Executed",
        "## Tool Output Summary",
        "## Key Findings",
        "## Tool Errors",
        "## Structured Signals",
        "## Decision Evidence",
        "## Artifact References",
    ):
        assert header not in prompt


# ---------------------------------------------------------------------------
# Request contract
# ---------------------------------------------------------------------------


def test_request_contract_renders_only_when_populated() -> None:
    """``## Request Contract`` appears only when the contract has fields."""
    metadata_populated = {
        "request_contract": {
            "question_type": "is_port_open",
            "answer_style": "yes_no",
            "terminal_when": "port confirmed open or closed",
        }
    }
    state_pop = _base_state(facts=_facts_with_metadata(metadata_populated))
    prompt_pop = _build(state_pop)

    assert "## Request Contract" in prompt_pop
    assert "question_type: is_port_open" in prompt_pop
    assert "answer_style: yes_no" in prompt_pop
    assert "terminal_when: port confirmed open or closed" in prompt_pop


def test_request_contract_omitted_when_absent() -> None:
    """No ``request_contract`` key means no ``## Request Contract`` section."""
    state_empty = _base_state(facts=_facts_with_metadata({}))
    prompt_empty = _build(state_empty)
    assert "## Request Contract" not in prompt_empty


# ---------------------------------------------------------------------------
# Active decision
# ---------------------------------------------------------------------------


def test_active_decision_renders_when_status_is_active() -> None:
    """Active decision section appears only when ``status == 'active'``."""
    metadata = {
        "working_memory": {
            "active_decision": {
                "status": "active",
                "next_action": "call_tool",
                "tool_intent": {
                    "description": "scan target ports",
                    "target": "10.0.0.5",
                },
                "effective_next_goal": "Identify reachable services",
                "action_reasoning": "Need port visibility before service probes",
            },
        },
    }
    state = _base_state(facts=_facts_with_metadata(metadata))

    prompt = _build(state)

    assert "## Prior Active Decision (Advisory)" in prompt
    assert "next_action: call_tool" in prompt
    assert "tool_intent.description: scan target ports" in prompt
    assert "tool_intent.target: 10.0.0.5" in prompt
    assert "effective_next_goal: Identify reachable services" in prompt
    assert "decision_rationale:" in prompt


def test_active_decision_omitted_when_status_is_not_active() -> None:
    """Non-active decision (e.g. ``superseded``) is not rendered."""
    metadata = {
        "working_memory": {
            "active_decision": {
                "status": "superseded",
                "next_action": "should_not_appear",
            },
        },
    }
    state = _base_state(facts=_facts_with_metadata(metadata))

    prompt = _build(state)

    assert "## Prior Active Decision (Advisory)" not in prompt
    assert "should_not_appear" not in prompt


# ---------------------------------------------------------------------------
# Relevant findings
# ---------------------------------------------------------------------------


def test_relevant_findings_render_when_caller_supplies_matches() -> None:
    """``## Relevant Prior Findings`` renders only when caller passes findings."""
    findings: List[Mapping[str, Any]] = [
        {
            "kind": "port_open",
            "target": "10.0.0.5",
            "subject": "10.0.0.5:80/tcp",
            "details": {"service": "http", "product": "nginx"},
            "assertion_level": "observed",
            "state": "fresh",
        }
    ]
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, relevant_findings=findings)

    assert "## Relevant Prior Findings" in prompt
    assert "[fresh] port_open 10.0.0.5:80/tcp" in prompt
    assert "service=http" in prompt
    assert "product=nginx" in prompt


def test_relevant_findings_omitted_when_selector_empty_or_none() -> None:
    """Empty/None ``relevant_findings`` keeps the section out of the prompt."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt_none = _build(state)
    prompt_empty = _build(state, relevant_findings=[])

    assert "## Relevant Prior Findings" not in prompt_none
    assert "## Relevant Prior Findings" not in prompt_empty


# ---------------------------------------------------------------------------
# Phase memory
# ---------------------------------------------------------------------------


def test_phase_memory_renders_from_current_turn_phases_metadata() -> None:
    """Phase memory section reads ``working_memory['current_turn_phases']``.

    The ``turn_sequence`` kwarg filters which records render; matching records
    appear as tagged section-snapshot blocks under
    ``## Prior Current-Turn Phase Memory``.
    """
    metadata = {
        "working_memory": {
            "current_turn_phases": [
                {
                    "turn_sequence": 7,
                    "phase_sequence": 0,
                    "source": "tool",
                    "sections": [
                        {
                            "heading": "Tool Executed",
                            "body": "tool: nmap\nstatus: success",
                        },
                        {
                            "heading": "Tool Output Summary",
                            "body": "ran nmap scan against target",
                        },
                    ],
                },
                {
                    "turn_sequence": 7,
                    "phase_sequence": 1,
                    "source": "synthesis",
                    "sections": [
                        {
                            "heading": "Synthesis",
                            "body": "status: completed",
                        },
                        {
                            "heading": "Final Response",
                            "body": "open ports identified, plan refined",
                        },
                    ],
                },
                # Different turn — must be filtered out by turn_sequence kwarg.
                {
                    "turn_sequence": 6,
                    "phase_sequence": 0,
                    "source": "tool",
                    "sections": [
                        {
                            "heading": "Tool Output Summary",
                            "body": "older-turn-summary-should-not-appear",
                        }
                    ],
                },
            ],
        },
    }
    state = _base_state(facts=_facts_with_metadata(metadata))

    prompt = _build(state, turn_sequence=7)

    assert "## Prior Current-Turn Phase Memory" in prompt
    assert "<phase turn=7 phase=0 source=tool>" in prompt
    assert "## Tool Executed\ntool: nmap\nstatus: success" in prompt
    assert "## Tool Output Summary\nran nmap scan against target" in prompt
    assert "<phase turn=7 phase=1 source=synthesis>" in prompt
    assert "## Synthesis\nstatus: completed" in prompt
    assert "## Final Response\nopen ports identified, plan refined" in prompt
    assert "</phase>" in prompt
    assert "older-turn-summary-should-not-appear" not in prompt
    assert "[turn=7 phase=0 source=tool]" not in prompt
    for old_key in ("kind:", "summary:", "result:", "target:", "hypothesis:"):
        assert old_key not in prompt


def test_phase_memory_omitted_when_ledger_empty() -> None:
    """Empty/missing ledger means the phase-memory section is not rendered."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, turn_sequence=1)

    assert "## Prior Current-Turn Phase Memory" not in prompt


# ---------------------------------------------------------------------------
# Runtime turn / phase counters
# ---------------------------------------------------------------------------


def test_execution_context_renders_when_turn_and_phase_counters_supplied() -> None:
    """``## Current Execution Context`` reflects the kwargs the node supplies."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(
        state,
        turn_sequence=4,
        current_phase_sequence=2,
        latest_recorded_phase_sequence=1,
    )

    assert "## Current Execution Context" in prompt
    assert "turn_sequence: 4" in prompt
    assert "current_phase_sequence: 2" in prompt
    assert "latest_recorded_phase_sequence: 1" in prompt
    # The PTR-specific label must not leak.
    assert "current_ptr_phase_sequence" not in prompt


def test_execution_context_omitted_when_no_counters_supplied() -> None:
    """Without integer counters, the execution-context section disappears."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state)

    assert "## Current Execution Context" not in prompt


# ---------------------------------------------------------------------------
# Environment context and scope hints
# ---------------------------------------------------------------------------


def test_environment_context_renders_when_supplied() -> None:
    """Environment context text passed via kwarg lands under its section header."""
    env_text = "OS: Kali\nNetwork: lab-bridge\nReachable: yes"
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, environment_context=env_text)

    assert "## Container Environment" in prompt
    assert "OS: Kali" in prompt
    assert "lab-bridge" in prompt


def test_environment_context_omitted_when_blank() -> None:
    """Blank/whitespace environment context is treated as empty."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt_default = _build(state)
    prompt_blank = _build(state, environment_context="   \n\t ")

    assert "## Container Environment" not in prompt_default
    assert "## Container Environment" not in prompt_blank


def test_scope_hints_render_when_user_scope_present() -> None:
    """``## Scope Hints`` renders fallback host, boundaries, and targets."""
    metadata = {
        "user_scope": {
            "conditional_targets": {"fallback_host": "10.0.0.5"},
            "boundaries": ["10.0.0.0/24", "no-prod"],
            "targets": ["10.0.0.5", "10.0.0.6"],
        },
    }
    state = _base_state(facts=_facts_with_metadata(metadata))

    prompt = _build(state)

    assert "## Scope Hints" in prompt
    assert "Fallback host: 10.0.0.5" in prompt
    assert "Boundaries: 10.0.0.0/24, no-prod" in prompt
    assert "Targets: 10.0.0.5, 10.0.0.6" in prompt


def test_scope_hints_omitted_when_user_scope_absent() -> None:
    """No ``user_scope`` key means no ``## Scope Hints`` section."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state)

    assert "## Scope Hints" not in prompt


# ---------------------------------------------------------------------------
# User input and user goal
# ---------------------------------------------------------------------------


def test_user_input_renders_from_facts_message() -> None:
    """``## User Input`` reads the verbatim ``facts.message`` value."""
    state = _base_state(
        facts={
            "plan": [],
            "todo_list": [],
            "message": "Find every open SMB share on 10.0.0.0/24",
        }
    )

    prompt = _build(state)

    assert "## User Input" in prompt
    assert "Find every open SMB share on 10.0.0.0/24" in prompt


def test_user_goal_renders_from_intent_brief_when_available() -> None:
    """``## User Goal`` reads ``intent_brief.resolved_user_intent`` when present."""
    metadata = {
        "working_memory": {
            "intent_brief": {
                "resolved_user_intent": "Map externally reachable services",
            },
        },
    }
    state = _base_state(facts=_facts_with_metadata(metadata, message="scan the host"))

    prompt = _build(state)

    assert "## User Goal" in prompt
    assert "Map externally reachable services" in prompt


# ---------------------------------------------------------------------------
# Synthesis-only ``## Loop Details`` section
# ---------------------------------------------------------------------------


def test_loop_details_omitted_when_both_counters_zero() -> None:
    """``## Loop Details`` is fully omitted when both counters are zero."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, reflection_count=0, iterations=0)

    assert "## Loop Details" not in prompt
    assert "Reflection cycles" not in prompt
    assert "Total iterations" not in prompt


def test_loop_details_renders_only_reflection_cycles_when_iterations_zero() -> None:
    """Only the reflection-cycles line renders when ``iterations == 0``."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, reflection_count=3, iterations=0)

    assert "## Loop Details" in prompt
    assert "Reflection cycles: 3" in prompt
    assert "Total iterations" not in prompt


def test_loop_details_renders_only_iterations_when_reflection_count_zero() -> None:
    """Only the iterations line renders when ``reflection_count == 0``."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, reflection_count=0, iterations=7)

    assert "## Loop Details" in prompt
    assert "Total iterations: 7" in prompt
    assert "Reflection cycles" not in prompt


def test_loop_details_renders_both_lines_when_both_counters_positive() -> None:
    """``## Loop Details`` renders both counter lines when both are positive."""
    state = _base_state(facts=_facts_with_metadata({}))

    prompt = _build(state, reflection_count=3, iterations=7)

    assert "## Loop Details" in prompt
    assert "Reflection cycles: 3" in prompt
    assert "Total iterations: 7" in prompt
