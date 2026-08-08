"""Contract tests for the versioned generic subagent runtime prompt family."""

from __future__ import annotations

from core.prompts.builders.subagent_runtime import (
    SubagentRuntimePromptBuilder,
)
from core.prompts.tests._golden import assert_golden


def test_subagent_runtime_system_prompt_uses_versioned_canonical_guidance() -> None:
    builder = SubagentRuntimePromptBuilder()

    prompt = builder.build_system_prompt(
        definition_id="pathfinder",
        display_name="Pathfinder",
        role_prompt=(
            "You are Pathfinder, a bounded recon subagent.\n"
            "Use tools when needed; otherwise return the parent handoff."
        ),
        definition_instructions=(
            "You are Pathfinder, a bounded reconnaissance subagent.\n"
            "Own only the assigned reconnaissance objective and hand the result "
            "to the parent agent."
        ),
        ownership_boundary=(
            "Own host discovery, port scanning, and service enumeration only."
        ),
        boundary_rules=(
            "Use only the targets, objective, scope, and constraints in the assignment context.",
            "Do not exploit, authenticate, mutate files unless explicitly allowed by assignment and tool scope, manage agents, or request credentials.",
        ),
        max_committed_tools_per_batch=3,
        callable_tool_ids=(
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
        ),
    )

    assert_golden("subagent_runtime__system.txt", prompt)
    assert "Definition Instructions:" in prompt
    assert "Ownership and Runtime Boundary:" in prompt
    assert "Use tools only when more evidence is needed" in prompt
    assert "return a concise parent handoff" in prompt
    assert "Emit native tool calls only." not in prompt
    assert "Remaining tool budget is permission, not a requirement" in prompt
    assert "Never repeat an equivalent successful tool call" in prompt
    assert "When more evidence is required, call between 1 and 3" in prompt
    assert "Selector Decision" not in prompt
    assert prompt.count("Use shell.utility for ordinary") == 1
    assert prompt.count("Use shell.assessment for commands") == 1


def test_subagent_runtime_user_prompt_injects_assignment_tools_observations_and_limits() -> None:
    builder = SubagentRuntimePromptBuilder()
    long_observation = "observed-service " * 200

    prompt = builder.build_user_prompt(
        display_name="Pathfinder",
        assignment={
            "assignment_id": "assign-1",
            "agent_run_id": "run-1",
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "task_id": 42,
            "tenant_id": 7,
            "conversation_id": "conversation-1",
            "parent_turn_id": "turn-1",
            "parent_graph_thread_id": "parent-thread-1",
            "objective": "Map live hosts on the approved target.",
            "targets": ["10.0.0.10"],
            "suggested_capabilities": ["host_discovery", "port_scan"],
            "scope_summary": "Approved internal test host only.",
            "relevant_context": {"ticket": "ENG-123"},
        },
        tool_ids=[
            "information_gathering.network_discovery.fping",
            "information_gathering.network_discovery.nmap",
        ],
        previous_tool_summary={
            "tool": "information_gathering.network_discovery.fping",
            "summary": "10.0.0.10 responded",
            "key_findings": [long_observation],
        },
        working_memory={
            "findings": ["prior ping sweep found one host"],
            "todos": ["confirm exposed services"],
        },
        remaining_limits={
            "completed_iterations": 1,
            "max_iterations": 3,
            "remaining_iterations": 2,
            "max_tool_calls_per_iteration": 3,
            "remaining_tool_calls_this_iteration": 3,
        },
    )

    assert_golden("subagent_runtime__user.txt", prompt)
    assert (
        "Candidate Tools (complete Pathfinder runtime profile; "
        "no separate selection step):"
    ) in prompt
    assert "Remaining Limits:" in prompt
    assert "Bounded Prior Observations:" in prompt
    assert "...[truncated]" in prompt
