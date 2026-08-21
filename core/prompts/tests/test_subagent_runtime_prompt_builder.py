"""Contract tests for the versioned generic subagent runtime prompt family."""

from __future__ import annotations

from core.prompts.builders.subagent_runtime import (
    SubagentRuntimePromptBuilder,
)
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder
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
    assert "Remaining tool budget" not in prompt
    assert "per-turn batch boundary, not a total tool budget" in prompt
    assert "Evaluate each tool call independently" in prompt
    assert "retry only the failed call" in prompt
    assert "Increasing a connection timeout" in prompt
    assert "repeat the same unresolved failure" in prompt
    assert "Deduplicate equivalent findings" in prompt
    assert "directly observed evidence from inferred or unconfirmed leads" in prompt
    assert "applicable to the assigned task" in prompt
    assert "When no provided native tool is applicable" in prompt
    assert "When more evidence is required, call between 1 and 3" in prompt
    assert "Execute dependent commands as separate tool calls" in prompt
    assert "Never append an interactive or persistent program" in prompt
    assert "Keep each shell invocation to one logical operation" in prompt
    assert "Do not bundle independent commands merely to reduce tool calls" in prompt
    assert "`cat file | grep pattern` is one operation" in prompt
    assert "`command-a; command-b` are separate operations" in prompt
    assert "Selector Decision" not in prompt
    assert prompt.count("Use shell.utility for ordinary") == 1
    assert prompt.count("Use shell.assessment for commands") == 1
    assert prompt.count("Leave interactive=false for ordinary commands") == 1
    assert prompt.count("never resend the originating") == 1


def test_subagent_runtime_reuses_main_execution_strategy_guidance() -> None:
    """All declarative subagents receive the main agent's batch semantics."""

    tool_planning = ToolPlanningPromptBuilder()
    main_prompt = tool_planning.build_tool_parameters_system_prompt(
        max_committed_tools_per_batch=3,
    )
    subagent_prompt = SubagentRuntimePromptBuilder().build_system_prompt(
        definition_id="test_agent",
        display_name="Test Agent",
        role_prompt="Perform one bounded assignment.",
        definition_instructions="Return evidence to the parent.",
        ownership_boundary="Own only the assigned objective.",
        boundary_rules=("Stay within the assignment.",),
        max_committed_tools_per_batch=3,
    )

    marker = "<execution_strategy_guidance>"
    end_marker = "</execution_strategy_guidance>"
    main_guidance = main_prompt.split(marker, 1)[1].split(end_marker, 1)[0]
    subagent_guidance = subagent_prompt.split(marker, 1)[1].split(end_marker, 1)[0]

    assert subagent_guidance == main_guidance
    assert "Parallel execution:" in subagent_guidance
    assert "Sequential execution:" in subagent_guidance


def test_subagent_runtime_budget_finalization_omits_tool_guidance() -> None:
    builder = SubagentRuntimePromptBuilder()
    assignment = {
        "objective": "Map the approved target.",
        "targets": ["10.0.0.10"],
    }

    system_prompt = builder.build_system_prompt(
        definition_id="pathfinder",
        display_name="Pathfinder",
        role_prompt="Perform one bounded assignment.",
        definition_instructions="Return evidence to the parent.",
        ownership_boundary="Own only the assigned objective.",
        boundary_rules=("Stay within the assignment.",),
        max_committed_tools_per_batch=3,
        callable_tool_ids=("shell.assessment",),
        finalization_only=True,
    )
    user_prompt = builder.build_user_prompt(
        display_name="Pathfinder",
        assignment=assignment,
        tool_ids=("shell.assessment",),
        finalization_only=True,
    )

    assert "Finalization Mode — Tool Budget Exhausted" in system_prompt
    assert "using only the accumulated observations and working memory" in system_prompt
    assert "Do not emit or simulate a tool call" in system_prompt
    assert "Native Tool and Shell Choice:" not in system_prompt
    assert "<execution_strategy_guidance>" not in system_prompt
    assert "Runtime Status: Tool budget exhausted" in user_prompt
    assert "Candidate Tools" not in user_prompt
    assert "shell.assessment" not in user_prompt


def test_subagent_runtime_user_prompt_preserves_precompressed_phase_context() -> None:
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
            "current_turn_phase_memory": (
                "## Prior Current-Turn Phase Memory\n"
                f"## Tool Output Summary\n{long_observation}"
            ),
        },
        working_memory={
            "findings": ["prior ping sweep found one host"],
            "todos": ["confirm exposed services"],
        },
    )

    assert_golden("subagent_runtime__user.txt", prompt)
    assert (
        "Candidate Tools (complete Pathfinder runtime profile; "
        "no separate selection step):"
    ) in prompt
    assert "Remaining Limits:" not in prompt
    assert "remaining_tool_calls" not in prompt
    assert "Bounded Prior Observations:" in prompt
    assert "Accumulated tool context" in prompt
    assert "Working memory snapshot" in prompt
    assert "Assignment:" in prompt
    assert "Prior Tool Outcomes:" not in prompt
    assert "...[truncated]" not in prompt
    assert long_observation in prompt
    assert "runtime_identity" not in prompt
