"""Focused tests for the working-memory LangGraph node wrapper."""

from __future__ import annotations

from agent.graph.state import FactsState, InteractiveState, TodoStatus, TraceState
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.working_memory import (
    apply_post_tool_candidate_findings,
    apply_post_tool_active_decision,
    update_working_memory_node,
)


def _base_state() -> dict:
    return {
        "facts": {
            "task_id": 42,
            "conversation_id": "conv-1",
            "message": "scan the host now",
            "capability": "simple_tool_execution",
            "metadata": {
                "conversation_history": [
                    {"role": "user", "content": "hello", "turn_sequence": 1},
                    {"role": "assistant", "content": "hi", "turn_sequence": 2},
                    {"role": "user", "content": "please scan", "turn_sequence": 3},
                ],
                "constraints": {"scope": ["lab-only"]},
            },
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }


def test_update_working_memory_node_writes_memory_and_scratchpad() -> None:
    state = _base_state()
    context = GraphRuntimeContext(task_id=42, turn_sequence=4, turn_id="turn-4")
    result = update_working_memory_node(state, context=context)

    assert "facts" in result
    metadata = result["facts"]["metadata"]
    assert "working_memory" in metadata
    wm = metadata["working_memory"]
    assert wm["schema"] == "drowai.working_memory.v1"
    assert wm["ids"]["task_id"] == 42
    assert wm["ids"]["turn_sequence"] == 4
    assert wm["stage"] == "tool_selection"
    assert wm["constraints"]["scope"] == ["lab-only"]
    assert "validation" in wm
    assert "required_inputs" in wm

    assert "trace" in result
    scratchpad = result["trace"]["scratchpad"]
    assert isinstance(scratchpad, str)
    assert "stage: tool_selection" in scratchpad
    assert "objective:" in scratchpad


def test_update_working_memory_node_does_not_mutate_conversation_history() -> None:
    state = _base_state()
    original_history = list(state["facts"]["metadata"]["conversation_history"])

    result = update_working_memory_node(state)
    updated_history = result["facts"]["metadata"]["conversation_history"]

    assert updated_history == original_history
    assert len(updated_history) == 3


def test_update_working_memory_node_handles_missing_metadata_defaults() -> None:
    state = {
        "facts": {
            "task_id": 9,
            "message": "hello",
            "capability": "respond_only",
            "metadata": {},
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }
    result = update_working_memory_node(state)
    wm = result["facts"]["metadata"]["working_memory"]
    assert wm["stage"] == "chat"
    assert wm["input"]["user_message_excerpt"] == "hello"
    assert isinstance(result["trace"]["scratchpad"], str)


def test_update_working_memory_node_does_not_seed_target_from_metadata_targets() -> None:
    state = {
        "facts": {
            "task_id": 10,
            "conversation_id": "conv-10",
            "message": "scan it then",
            "capability": "simple_tool_execution",
            "metadata": {
                "targets": ["172.17.0.1"],
            },
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }

    result = update_working_memory_node(state)
    wm = result["facts"]["metadata"]["working_memory"]

    assert wm["active"]["target_id"] is None
    assert "intent:target" not in wm["referents"]


def test_update_working_memory_node_folds_intent_brief_seed_into_working_memory() -> None:
    state = _base_state()
    state["facts"]["current_goal"] = "Previous live focus"
    state["facts"]["todo_list"] = []
    state["facts"]["metadata"]["intent_turn_interpretation"] = {
        "next_operational_goal": "Run nmap service discovery",
        "execution_readiness": "ready",
    }
    state["facts"]["metadata"]["intent_brief_seed"] = {
        "original_goal": "Scan target for open ports, then identify exposed services",
        "task_seed": [],
        "resolved_user_intent": "Scan target for open ports",
        "next_operational_goal": "Run nmap service discovery",
        "execution_readiness": "ready",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": [],
        "suggested_category_focus": [],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
    }

    result = update_working_memory_node(state)
    metadata = result["facts"]["metadata"]
    wm = metadata["working_memory"]

    assert wm["intent_brief"]["resolved_user_intent"] == "Scan target for open ports"
    assert wm["intent_brief"]["original_goal"] == (
        "Scan target for open ports, then identify exposed services"
    )
    assert result["facts"]["todo_list"] == []
    assert result["facts"]["current_goal"] == "Run nmap service discovery"
    assert "intent_brief_seed" not in metadata


def test_update_working_memory_node_seeds_simple_tool_todos_from_task_seed() -> None:
    state = _base_state()
    state["facts"]["current_goal"] = "Previous live focus"
    state["facts"]["todo_list"] = []
    state["facts"]["metadata"]["intent_turn_interpretation"] = {
        "next_operational_goal": "Discover and inspect PostgreSQL exposure",
        "execution_readiness": "ready",
    }
    state["facts"]["metadata"]["intent_brief_seed"] = {
        "original_goal": (
            "Scan the current network to find online hosts, then scan one host "
            "for PostgreSQL"
        ),
        "task_seed": [
            "Scan the current network to find online hosts",
            "Choose one online host",
            "Scan the online host for PostgreSQL",
        ],
        "resolved_user_intent": "Find an online host and inspect PostgreSQL exposure",
        "next_operational_goal": "Discover and inspect PostgreSQL exposure",
        "execution_readiness": "ready",
        "target_status": "resolved",
        "target_source": "environment",
        "explicit_constraints": [],
        "suggested_category_focus": [],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
    }

    result = update_working_memory_node(state)

    todos = result["facts"]["todo_list"]
    assert [todo["description"] for todo in todos] == [
        "Scan the current network to find online hosts",
        "Choose one online host",
        "Scan the online host for PostgreSQL",
    ]
    assert todos[0]["status"] == TodoStatus.IN_PROGRESS.value
    assert todos[1]["status"] == TodoStatus.PENDING.value
    assert result["facts"]["current_goal"] == (
        "Scan the current network to find online hosts"
    )
    wm = result["facts"]["metadata"]["working_memory"]
    assert wm["objective"]["text"] == "Scan the current network to find online hosts"
    assert wm["objective"]["source"] == "intent_task_seed"
    assert wm["intent_brief"]["task_seed"] == [
        "Scan the current network to find online hosts",
        "Choose one online host",
        "Scan the online host for PostgreSQL",
    ]


def test_update_working_memory_node_preserves_existing_todo_list() -> None:
    state = _base_state()
    state["facts"]["current_goal"] = "Existing goal"
    state["facts"]["todo_list"] = ["existing todo"]
    state["facts"]["metadata"]["intent_turn_interpretation"] = {
        "next_operational_goal": "New projected goal",
        "execution_readiness": "ready",
    }
    state["facts"]["metadata"]["intent_brief_seed"] = {
        "original_goal": "New full goal",
        "task_seed": ["New seeded todo"],
        "execution_readiness": "ready",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": [],
        "suggested_category_focus": [],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {},
    }

    result = update_working_memory_node(state)

    assert result["facts"]["todo_list"] == ["existing todo"]
    assert result["facts"]["current_goal"] == "New projected goal"


def test_update_working_memory_node_does_not_seed_task_seed_for_deep_reasoning() -> None:
    state = _base_state()
    state["facts"]["capability"] = "deep_reasoning"
    state["facts"]["todo_list"] = []
    state["facts"]["metadata"]["intent_turn_interpretation"] = {
        "next_operational_goal": "Planner-owned goal",
        "execution_readiness": "ready",
    }
    state["facts"]["metadata"]["intent_brief_seed"] = {
        "original_goal": "Plan the work",
        "task_seed": ["Should not seed"],
        "execution_readiness": "ready",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": [],
        "suggested_category_focus": [],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {},
    }

    result = update_working_memory_node(state)

    assert result["facts"]["todo_list"] == []
    assert result["facts"]["current_goal"] == "Planner-owned goal"


def test_update_working_memory_node_does_not_seed_task_seed_for_plan_label() -> None:
    state = _base_state()
    state["facts"]["todo_list"] = []
    state["facts"]["metadata"]["intent_classifier_label"] = "plan_executor"
    state["facts"]["metadata"]["intent_turn_interpretation"] = {
        "next_operational_goal": "Plan-classified objective",
        "execution_readiness": "ready",
    }
    state["facts"]["metadata"]["intent_brief_seed"] = {
        "original_goal": "Plan-classified objective",
        "task_seed": ["Should not seed"],
        "execution_readiness": "ready",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": [],
        "suggested_category_focus": [],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {},
    }

    result = update_working_memory_node(state)

    assert result["facts"]["todo_list"] == []
    assert result["facts"]["current_goal"] == "Plan-classified objective"


def test_update_working_memory_node_does_not_seed_when_execution_not_ready() -> None:
    state = _base_state()
    state["facts"]["todo_list"] = []
    state["facts"]["metadata"]["intent_turn_interpretation"] = {
        "next_operational_goal": None,
        "execution_readiness": "blocked",
    }
    state["facts"]["metadata"]["intent_brief_seed"] = {
        "original_goal": None,
        "task_seed": ["Should not seed"],
        "execution_readiness": "blocked",
        "target_status": "unresolved",
        "target_source": "none",
        "explicit_constraints": [],
        "suggested_category_focus": [],
        "retrieval_hints": [],
        "relevant_memory_fragments": [],
        "request_contract": {},
    }

    result = update_working_memory_node(state)

    assert result["facts"]["todo_list"] == []
    assert result["facts"]["current_goal"] == ""


def test_update_working_memory_node_reuses_prior_active_target_when_continuity_allow() -> None:
    state = {
        "facts": {
            "task_id": 12,
            "conversation_id": "conv-12",
            "message": "scan it then",
            "capability": "simple_tool_execution",
            "metadata": {
                "intent_target_continuity": {"status": "allow", "source": "classifier"},
                "working_memory": {
                    "schema": "drowai.working_memory.v1",
                    "active": {"target_id": "target:intent:target"},
                    "referents": {"intent:target": {"value": "172.17.0.1", "kind": "ip"}},
                },
            },
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }

    result = update_working_memory_node(state)
    wm = result["facts"]["metadata"]["working_memory"]
    assert wm["active"]["target_id"] == "target:intent:target"
    assert wm["referents"]["intent:target"]["value"] == "172.17.0.1"


def test_apply_post_tool_active_decision_updates_working_memory() -> None:
    interactive = InteractiveState(
        facts=FactsState(
            task_id=11,
            message="scan",
            metadata={"working_memory": {"ids": {"task_id": 11, "turn_sequence": 1}}},
        ),
        trace=TraceState(),
    )

    apply_post_tool_active_decision(
        interactive,
        {
            "source": "post_tool_reasoning",
            "authority": "llm_proposal",
            "status": "active",
            "next_action": "call_tool",
            "tool_intent": {
                "description": "Scan port 5432",
                "target": "172.17.0.1",
                "focus": "postgres",
            },
            "action_reasoning": "Only viable target remains.",
            "todo_delta": [{"index": 1, "status": "in_progress"}],
        },
    )

    wm = interactive.facts.metadata["working_memory"]
    assert wm["active_decision"] is not None
    assert wm["active_decision"]["status"] == "active"
    assert wm["active_decision"]["authority"] == "llm_proposal"
    assert wm["active_decision"]["tool_intent"]["target"] == "172.17.0.1"


def test_apply_post_tool_candidate_findings_updates_available_findings() -> None:
    interactive = InteractiveState(
        facts=FactsState(
            task_id=12,
            message="enumerate http",
            metadata={
                "working_memory": {
                    "ids": {"task_id": 12, "turn_sequence": 2},
                    "active": {"target_id": "target:intent:target"},
                    "referents": {"intent:target": {"value": "10.10.10.5"}},
                }
            },
        ),
        trace=TraceState(),
    )

    apply_post_tool_candidate_findings(
        interactive,
        [
            {
                "observation_type": "finding.vulnerability",
                "subject_key_hint": "http/nginx",
                "confidence": 0.61,
                "attributes": [{"key": "service", "value": "http"}],
                "rationale": "Banner suggests nginx.",
                "evidence_refs": [{"source_artifact_id": "artifact-1", "excerpt": "Server: nginx"}],
            }
        ],
    )

    wm = interactive.facts.metadata["working_memory"]
    assert wm["available_findings"]
    assert wm["available_findings"][-1]["target"] == "10.10.10.5"
    assert wm["available_findings"][-1]["assertion_level"] == "candidate"


def test_update_working_memory_bootstraps_shared_runtime_budgets() -> None:
    """Turn-start working memory seeds shared runtime budgets for all graphs."""
    state = _base_state()
    assert "runtime_budgets" not in state["facts"]["metadata"]

    updated = update_working_memory_node(state)
    runtime_budgets = updated["facts"]["metadata"]["runtime_budgets"]

    assert runtime_budgets["remaining_iterations"] == 15
    assert runtime_budgets["remaining_tool_calls"] == 10
    assert runtime_budgets["time_budget_ms"] == 300_000


def test_update_working_memory_preserves_existing_runtime_budgets() -> None:
    """Bootstrap must not clobber decremented budgets on resume."""
    state = _base_state()
    state["facts"]["metadata"]["runtime_budgets"] = {
        "remaining_iterations": 4,
        "remaining_tool_calls": 2,
        "time_budget_ms": 120_000,
    }

    updated = update_working_memory_node(state)
    runtime_budgets = updated["facts"]["metadata"]["runtime_budgets"]

    assert runtime_budgets == {
        "remaining_iterations": 4,
        "remaining_tool_calls": 2,
        "time_budget_ms": 120_000,
    }
