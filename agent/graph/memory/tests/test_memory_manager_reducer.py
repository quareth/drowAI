"""Focused tests for deterministic working-memory reducer behavior."""

from __future__ import annotations

from agent.graph.memory.memory_manager import MemoryManager
from agent.graph.memory.working_memory import (
    CAP_COVERAGE,
    CAP_RECENT_TURNS,
    CAP_REFERENTS,
    create_working_memory,
)


def test_reduce_turn_start_uses_only_recent_tail_and_keeps_objective_unknown() -> None:
    previous = create_working_memory(ids={"task_id": 12})
    history_tail = [
        {"role": "user", "turn_sequence": 5, "content": "older"},
        {"role": "assistant", "turn_sequence": 6, "content": "mid"},
        {"role": "user", "turn_sequence": 7, "content": "latest"},
    ]
    updated = MemoryManager.reduce_turn_start(
        previous=previous,
        user_message="scan target host",
        conversation_history_tail=history_tail,
        runtime_ids={"turn_sequence": 8, "turn_id": "t-8"},
        route="simple_tool_execution",
        constraints={"scope": ["lab-only"]},
        intent_hints={},
    )

    assert updated["stage"] == "tool_selection"
    assert updated["objective"]["text"] == "unknown"
    assert updated["ids"]["turn_sequence"] == 8
    # capped to recent-turn contract size; last should be current user turn
    assert len(updated["recent_turns"]) == CAP_RECENT_TURNS
    assert updated["recent_turns"][-1]["content_excerpt"] == "scan target host"
    assert updated["constraints"]["scope"] == ["lab-only"]


def test_reduce_turn_start_does_not_infer_target_from_recent_conversation_tail() -> None:
    previous = create_working_memory(ids={"task_id": 44})
    history_tail = [
        {
            "role": "assistant",
            "turn_sequence": 9,
            "content": "From what we saw, scan 172.17.0.1 with nmap --top-ports 1000.",
        },
        {"role": "user", "turn_sequence": 10, "content": "scan it then"},
    ]
    updated = MemoryManager.reduce_turn_start(
        previous=previous,
        user_message="scan it then",
        conversation_history_tail=history_tail,
        runtime_ids={"turn_sequence": 11, "turn_id": "t-11"},
        route="simple_tool_execution",
        constraints={},
        intent_hints={},
    )

    assert updated["stage"] == "tool_selection"
    assert updated["active"]["target_id"] is None


def test_reduce_turn_start_reuses_prior_active_target_when_continuity_allow() -> None:
    previous = create_working_memory(ids={"task_id": 45})
    previous["active"]["target_id"] = "target:intent:target"
    previous["referents"]["intent:target"] = {
        "value": "172.17.0.1",
        "kind": "ip",
        "confidence": 0.9,
        "source": "intent_hints",
    }

    updated = MemoryManager.reduce_turn_start(
        previous=previous,
        user_message="scan it then",
        conversation_history_tail=[],
        runtime_ids={"turn_sequence": 12, "turn_id": "t-12"},
        route="simple_tool_execution",
        constraints={},
        intent_hints={},
        intent_target_continuity={"status": "allow", "source": "classifier"},
    )

    assert updated["active"]["target_id"] == "target:intent:target"
    assert updated["referents"]["intent:target"]["value"] == "172.17.0.1"
    assert updated["referents"]["intent:target"]["source"] == "working_memory_active_binding"


def test_reduce_turn_start_clears_stale_target_questions_in_tool_selection() -> None:
    previous = create_working_memory(ids={"task_id": 55})
    previous["open_questions"] = [
        {"code": "missing_target_handle", "message": "Please specify target", "stage": "tool_selection"},
        {"code": "clarify_scope", "message": "Clarify scope", "stage": "chat"},
    ]

    updated = MemoryManager.reduce_turn_start(
        previous=previous,
        user_message="scan it then",
        conversation_history_tail=[],
        runtime_ids={"turn_sequence": 3, "turn_id": "t-3"},
        route="simple_tool_execution",
        constraints={},
        intent_hints={},
    )

    open_question_codes = {item["code"] for item in updated["open_questions"]}
    assert "missing_target_handle" not in open_question_codes
    assert "clarify_scope" in open_question_codes


def test_reduce_turn_start_does_not_promote_verb_like_message_to_target() -> None:
    updated = MemoryManager.reduce_turn_start(
        previous=create_working_memory(ids={"task_id": 77}),
        user_message="scan",
        conversation_history_tail=[],
        runtime_ids={"turn_sequence": 1, "turn_id": "t-1"},
        route="simple_tool_execution",
        constraints={},
        intent_hints={},
    )

    assert updated["active"]["target_id"] is None
    assert "intent:target" not in updated["referents"]


def test_reduce_turn_start_projects_next_operational_goal_into_objective() -> None:
    updated = MemoryManager.reduce_turn_start(
        previous=create_working_memory(ids={"task_id": 88}),
        user_message="continue with enumeration",
        conversation_history_tail=[],
        runtime_ids={"turn_sequence": 2, "turn_id": "t-2"},
        route="simple_tool_execution",
        constraints={},
        intent_hints={},
        intent_turn_interpretation={
            "next_operational_goal": "Enumerate exposed services on 10.0.0.5",
            "execution_readiness": "ready",
        },
    )

    assert updated["objective"]["text"] == "Enumerate exposed services on 10.0.0.5"
    assert updated["objective"]["status"] == "active"
    assert updated["objective"]["source"] == "intent_turn_interpretation"
    assert updated["objective"]["provenance"]["authority"] == "derived"


def test_reduce_turn_start_projects_blocking_reason_into_open_questions() -> None:
    updated = MemoryManager.reduce_turn_start(
        previous=create_working_memory(ids={"task_id": 89}),
        user_message="scan it",
        conversation_history_tail=[],
        runtime_ids={"turn_sequence": 4, "turn_id": "t-4"},
        route="simple_tool_execution",
        constraints={},
        intent_hints={},
        intent_turn_interpretation={
            "next_operational_goal": "Run TCP port scan on selected host",
            "execution_readiness": "blocked",
            "blocking_reason": "The target host has not been specified yet.",
        },
    )

    assert updated["objective"]["status"] == "blocked"
    blockers = [item for item in updated["open_questions"] if item["code"] == "intent_execution_blocked"]
    assert len(blockers) == 1
    assert blockers[0]["message"] == "The target host has not been specified yet."


def test_reduce_tool_result_is_pointer_first_and_no_raw_output() -> None:
    previous = create_working_memory()
    updated = MemoryManager.reduce_tool_result(
        previous=previous,
        tool_id="nmap_scan",
        tool_params={"target": "127.0.0.1", "ports": "22,80"},
        compact_envelope={
            "summary": "Port 22 and 80 open",
            "key_findings": ["22/tcp open", "80/tcp open"],
            "stdout": "raw output should not be persisted",
            "stderr_excerpt": "raw error should not be persisted",
        },
        artifact_refs=[{"uri": "artifact://scan.json", "count": 2}],
        execution_id="run-1",
        observed_findings=[
            {
                "kind": "port_open",
                "target": "127.0.0.1",
                "subject": "127.0.0.1:80/tcp",
                "details": {"service": "http"},
                "assertion_level": "observed",
                "confidence": 1.0,
                "seen_at": 100,
                "ttl_seconds": 600,
            }
        ],
    )

    assert updated["tool_state"]["selected_tool"] == "nmap_scan"
    assert updated["tool_runs"][-1]["id"] == "tool_run:run-1"
    assert updated["tool_runs"][-1]["summary"] == "Port 22 and 80 open"
    assert "stdout" not in updated["tool_runs"][-1]
    assert "stderr_excerpt" not in updated["tool_runs"][-1]
    assert updated["collections"][-1]["id"] == "collection:run-1:0"
    assert updated["active"]["subject_id"] == "tool_run:run-1"
    assert updated["active"]["collection_id"] == "collection:run-1:0"
    assert updated["active"]["target_id"] is not None
    assert updated["coverage"][-1]["tool_id"] == "nmap_scan"
    assert updated["coverage"][-1]["status"] == "covered"
    assert updated["coverage"][-1]["provenance"]["authority"] == "tool"
    assert updated["available_findings"][-1]["subject"] == "127.0.0.1:80/tcp"
    assert updated["available_findings"][-1]["kind"] == "port_open"


def test_reduce_tool_result_extracts_target_before_param_redaction() -> None:
    updated = MemoryManager.reduce_tool_result(
        previous=create_working_memory(),
        tool_id="shell.exec",
        tool_params={"target": "token.example.com", "api_key": "top-secret"},
        compact_envelope={"summary": "ok"},
        artifact_refs=[],
        execution_id="run-raw-target",
    )

    assert updated["tool_state"]["tool_params"]["target"] == "<REDACTED>"
    assert updated["tool_state"]["tool_params"]["api_key"] == "<REDACTED>"
    assert updated["referents"]["tool:run-raw-target:target"]["value"] == "token.example.com"
    assert updated["active"]["target_id"] == "target:tool:run-raw-target:target"


def test_reduce_tool_result_without_target_creates_open_question() -> None:
    previous = create_working_memory()
    updated = MemoryManager.reduce_tool_result(
        previous=previous,
        tool_id="nmap_scan",
        tool_params={"ports": "80"},
        compact_envelope={"summary": "partial"},
        artifact_refs=[],
        execution_id="run-2",
    )

    codes = {item["code"] for item in updated["open_questions"]}
    assert "unresolved_target_for_tool_run" in codes


def test_reduce_post_tool_findings_projects_candidate_rows_into_available_findings() -> None:
    updated = MemoryManager.reduce_post_tool_findings(
        previous=create_working_memory(),
        candidate_observations=[
            {
                "observation_type": "finding.vulnerability",
                "subject_key_hint": "http/nginx",
                "confidence": 0.6,
                "attributes": [{"key": "service", "value": "http"}],
                "rationale": "Version banner suggests nginx.",
                "evidence_refs": [{"source_artifact_id": "artifact-1", "excerpt": "Server: nginx"}],
                "vulnerability": {"id": "CVE-2024-0001", "title": "Example", "severity": "medium"},
                "vulnerability_confidence": 0.6,
            }
        ],
        active_target="10.10.10.5",
    )

    finding = updated["available_findings"][-1]
    assert finding["kind"] == "finding.vulnerability"
    assert finding["target"] == "10.10.10.5"
    assert finding["subject"] == "http/nginx"
    assert finding["assertion_level"] == "candidate"


def test_reduce_turn_end_is_bounded_and_deterministic() -> None:
    previous = create_working_memory()
    commitments = [{"text": f"c-{i}"} for i in range(30)]
    open_questions = [{"code": "clarify_scope", "message": "Scope?", "stage": "chat"}]
    a = MemoryManager.reduce_turn_end(previous, commitments, open_questions)
    b = MemoryManager.reduce_turn_end(previous, commitments, open_questions)

    assert a == b
    assert a["stage"] == "chat"
    assert len(a["commitments"]) == 20
    assert any(item["code"] == "clarify_scope" for item in a["open_questions"])


def test_reduce_tool_result_keeps_memory_within_caps_over_many_updates() -> None:
    memory = create_working_memory()
    for idx in range(120):
        memory = MemoryManager.reduce_tool_result(
            previous=memory,
            tool_id="shell.exec",
            tool_params={"target": f"host-{idx}"},
            compact_envelope={"summary": f"run {idx}", "key_findings": [f"finding-{idx}"]},
            artifact_refs=[{"uri": f"artifact://{idx}.json", "count": 1}],
            execution_id=f"run-{idx}",
        )

    assert len(memory["tool_runs"]) <= 10
    assert len(memory["collections"]) <= 20
    assert len(memory["open_questions"]) <= 10
    assert len(memory["facts"]) <= 50
    assert len(memory["coverage"]) == CAP_COVERAGE
    assert len(memory["referents"]) == CAP_REFERENTS


def test_reduce_post_tool_decision_sets_active_decision_contract() -> None:
    previous = create_working_memory(ids={"turn_sequence": 3, "turn_id": "turn-3"})
    updated = MemoryManager.reduce_post_tool_decision(
        previous=previous,
        active_decision={
            "source": "post_tool_reasoning",
            "authority": "llm_proposal",
            "status": "active",
            "next_action": "call_tool",
            "tool_intent": {
                "description": "Scan TCP 5432 on selected host",
                "target": "172.17.0.1",
                "focus": "postgres",
            },
            "effective_next_goal": "Verify TCP/5432 state",
            "action_reasoning": "Only feasible host was selected in previous step.",
            "todo_delta": [{"index": 1, "status": "in_progress"}],
        },
    )

    active = updated["active_decision"]
    assert active is not None
    assert active["status"] == "active"
    assert active["authority"] == "llm_proposal"
    assert active["tool_intent"]["target"] == "172.17.0.1"


def test_reduce_turn_start_clears_stale_active_decision_on_new_turn() -> None:
    previous = create_working_memory(ids={"turn_sequence": 3, "turn_id": "turn-3"})
    previous = MemoryManager.reduce_post_tool_decision(
        previous=previous,
        active_decision={
            "source": "post_tool_reasoning",
            "authority": "llm_proposal",
            "status": "active",
            "next_action": "call_tool",
            "tool_intent": {"description": "Scan host", "target": "10.0.0.1", "focus": "5432"},
            "action_reasoning": "Pending follow-up",
            "todo_delta": [],
        },
    )

    updated = MemoryManager.reduce_turn_start(
        previous=previous,
        user_message="new request",
        conversation_history_tail=[],
        runtime_ids={"turn_sequence": 4, "turn_id": "turn-4"},
        route="deep_reasoning",
        constraints={},
        intent_hints={},
    )

    assert updated["active_decision"] is None
