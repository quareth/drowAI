"""Subagent shell ownership tests for child graph configuration.

Child execution must replace inherited parent graph context so shell sessions
are owned by the agent run identifier that settlement later cleans up.
"""

from agent.subagents.definition import load_subagent_definitions
from agent.subagents.runtime.state import build_subagent_initial_state
from backend.services.agent_runs.worker import prepare_subagent_child_config
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_runtime_identity,
)


def _pathfinder_definition():
    return next(
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    )


def test_child_initial_state_uses_agent_run_shell_owner() -> None:
    assignment = build_agent_assignment(
        runtime_identity=build_runtime_identity(user_id=3),
        agent_run_id="run-shell-owner",
        relevant_context={"agent_mode": "full_access"},
    )

    graph_input = build_subagent_initial_state(
        definition=_pathfinder_definition(),
        assignment=assignment,
        graph_thread_id="child-thread",
    )

    context = graph_input["facts"]["metadata"]["graph_runtime_context"]
    assert context["execution_owner_id"] == "subagent:run-shell-owner"


def test_child_config_replaces_inherited_parent_shell_owner() -> None:
    assignment = build_agent_assignment(
        runtime_identity=build_runtime_identity(user_id=3),
        agent_run_id="run-shell-owner",
        parent_turn_id="parent-turn",
        relevant_context={"turn_sequence": 4},
    )
    config = prepare_subagent_child_config(
        {
            "configurable": {
                "runtime_projection": {
                    "tenant_id": assignment.tenant_id,
                    "task_id": assignment.task_id,
                    "execution_owner_id": "main:stale-parent",
                },
                "graph_runtime_context": {
                    "tenant_id": assignment.tenant_id,
                    "task_id": assignment.task_id,
                    "graph_thread_id": "parent-thread",
                    "execution_owner_id": "main:stale-parent",
                },
            }
        },
        assignment=assignment,
        graph_thread_id="child-thread",
    )

    context = config["configurable"]["graph_runtime_context"]
    assert context["execution_owner_id"] == "subagent:run-shell-owner"
    assert context["graph_thread_id"] == "child-thread"
    assert context["turn_id"] == "parent-turn"
