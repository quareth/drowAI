"""Direct equivalence tests for process-local handoff claim policy."""

from __future__ import annotations

import ast
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from backend.services.agent_runs import (
    registry_handoffs,
    registry_lifecycle,
)
from backend.services.agent_runs.registry_contracts import AgentRunKey, LocalAgentRun
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
    build_runtime_identity,
)


def _entry(
    agent_run_id: str,
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    conversation_id: str = "conversation-1",
    created_at: datetime | None = None,
) -> LocalAgentRun:
    assignment = build_agent_assignment(
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        conversation_id=conversation_id,
        runtime_identity=build_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )
    return registry_lifecycle.build_queued_entry(
        assignment=assignment,
        graph_thread_id=f"thread-{agent_run_id}",
        created_at=created_at or datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )


def _key(entry: LocalAgentRun) -> AgentRunKey:
    return (entry.tenant_id, entry.task_id, entry.agent_run_id)


def _running(
    entry: LocalAgentRun,
    *,
    started_at: datetime | None = None,
) -> LocalAgentRun:
    return registry_lifecycle.build_running_entry(
        entry,
        started_at=started_at or datetime(2026, 8, 2, 12, 1, tzinfo=UTC),
    )


def _completed(
    entry: LocalAgentRun,
    *,
    completed_at: datetime | None = None,
) -> LocalAgentRun:
    return registry_lifecycle.build_completed_entry(
        entry,
        result=build_agent_result(entry.assignment),
        completed_at=completed_at or datetime(2026, 8, 2, 12, 2, tzinfo=UTC),
    )


def test_selection_covers_empty_claimed_consumed_scoped_and_active_runs() -> None:
    active = _running(_entry("active"))
    claimed = replace(_completed(_entry("claimed")), result_claim_id="claim-1")
    consumed = replace(_completed(_entry("consumed")), result_consumed=True)
    foreign_task = _completed(_entry("foreign-task", task_id=43))
    foreign_conversation = _completed(
        _entry("foreign-conversation", conversation_id="conversation-2")
    )
    entries = (
        active,
        claimed,
        consumed,
        foreign_task,
        foreign_conversation,
    )

    projection = registry_handoffs.select_ready_handoffs(
        entries,
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )

    assert projection.candidates == ()
    assert projection.claimed_ready_count == 1
    assert projection.active_runs == (active,)

    empty_foreign_conversation = registry_handoffs.select_ready_handoffs(
        entries,
        tenant_id=7,
        task_id=42,
        conversation_id="missing-conversation",
    )
    assert empty_foreign_conversation.candidates == ()
    assert empty_foreign_conversation.claimed_ready_count == 0
    assert empty_foreign_conversation.active_runs == ()


def test_selection_is_deterministic_and_respects_max_results() -> None:
    late = _completed(
        _entry("late", created_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC)),
        completed_at=datetime(2026, 8, 2, 12, 3, tzinfo=UTC),
    )
    early = _completed(
        _entry("early", created_at=datetime(2026, 8, 2, 12, 1, tzinfo=UTC)),
        completed_at=datetime(2026, 8, 2, 12, 2, tzinfo=UTC),
    )
    tie_b = _completed(
        _entry("tie-b", created_at=datetime(2026, 8, 2, 12, 1, tzinfo=UTC)),
        completed_at=datetime(2026, 8, 2, 12, 4, tzinfo=UTC),
    )
    tie_a = _completed(
        _entry("tie-a", created_at=datetime(2026, 8, 2, 12, 1, tzinfo=UTC)),
        completed_at=datetime(2026, 8, 2, 12, 4, tzinfo=UTC),
    )
    active_late = _running(
        _entry("active-late"),
        started_at=datetime(2026, 8, 2, 12, 6, tzinfo=UTC),
    )
    active_early = _running(
        _entry("active-early"),
        started_at=datetime(2026, 8, 2, 12, 5, tzinfo=UTC),
    )

    projection = registry_handoffs.select_ready_handoffs(
        (late, tie_b, active_late, early, tie_a, active_early),
        tenant_id=7,
        task_id=42,
        max_results=3,
    )

    assert tuple(entry.agent_run_id for entry in projection.candidates) == (
        "early",
        "late",
        "tie-a",
    )
    assert tuple(entry.agent_run_id for entry in projection.active_runs) == (
        "active-early",
        "active-late",
    )


def test_claim_decision_builds_claim_replacements_and_public_batch() -> None:
    first = _completed(_entry("run-1"))
    second = _completed(_entry("run-2"))
    active = _running(_entry("active"))
    projection = registry_handoffs.select_ready_handoffs(
        (first, active, second),
        tenant_id=7,
        task_id=42,
    )

    decision = registry_handoffs.build_claim_decision(
        projection,
        claim_id="handoff-claim:7:42:1",
        tenant_id=7,
        task_id=42,
    )

    assert decision.claim_keys == (_key(first), _key(second))
    assert tuple(replacement.key for replacement in decision.replacements) == (
        _key(first),
        _key(second),
    )
    assert all(
        replacement.entry.result_claim_id == "handoff-claim:7:42:1"
        for replacement in decision.replacements
    )
    assert decision.batch.claim_id == "handoff-claim:7:42:1"
    assert decision.batch.agent_run_ids == ("run-1", "run-2")
    assert decision.batch.results == (first.result, second.result)
    assert decision.batch.active_runs == (active,)
    assert first.result_claim_id is None
    assert second.result_claim_id is None


def test_acknowledge_and_release_replacements_preserve_claim_rules() -> None:
    claim_id = "handoff-claim:7:42:1"
    acknowledged = replace(_completed(_entry("ack")), result_claim_id=claim_id)
    wrong_claim = replace(_completed(_entry("wrong")), result_claim_id="other-claim")
    released = replace(_completed(_entry("release")), result_claim_id=claim_id)
    consumed = replace(
        replace(_completed(_entry("consumed")), result_claim_id=claim_id),
        result_consumed=True,
    )
    runs = OrderedDict(
        (
            (_key(acknowledged), acknowledged),
            (_key(wrong_claim), wrong_claim),
            (_key(released), released),
            (_key(consumed), consumed),
        )
    )
    claim_keys = (
        _key(acknowledged),
        _key(wrong_claim),
        _key(released),
        _key(consumed),
        (7, 42, "missing"),
    )

    ack_decision = registry_handoffs.build_acknowledge_replacements(
        runs,
        claim_keys=claim_keys,
        claim_id=claim_id,
    )
    release_decision = registry_handoffs.build_release_replacements(
        runs,
        claim_keys=claim_keys,
        claim_id=claim_id,
    )

    assert ack_decision.affected_count == 3
    assert tuple(replacement.key for replacement in ack_decision.replacements) == (
        _key(acknowledged),
        _key(released),
        _key(consumed),
    )
    assert all(
        replacement.entry.result_consumed is True
        and replacement.entry.result_claim_id is None
        for replacement in ack_decision.replacements
    )
    assert release_decision.affected_count == 2
    assert tuple(replacement.key for replacement in release_decision.replacements) == (
        _key(acknowledged),
        _key(released),
    )
    assert all(
        replacement.entry.result_consumed is False
        and replacement.entry.result_claim_id is None
        for replacement in release_decision.replacements
    )
    assert wrong_claim.result_claim_id == "other-claim"


def test_one_shot_consumption_eligibility_matches_registry_rules() -> None:
    ready = _completed(_entry("ready"))
    claimed = replace(_completed(_entry("claimed")), result_claim_id="claim-1")
    consumed = replace(_completed(_entry("consumed")), result_consumed=True)
    active = _running(_entry("active"))

    decision = registry_handoffs.build_consumption_decision(ready)

    assert decision.result == ready.result
    assert decision.replacement is not None
    assert decision.replacement.key == _key(ready)
    assert decision.replacement.entry.result_consumed is True
    assert ready.result_consumed is False
    assert registry_handoffs.build_consumption_decision(None).result is None
    assert registry_handoffs.build_consumption_decision(claimed).replacement is None
    assert registry_handoffs.build_consumption_decision(consumed).replacement is None
    assert registry_handoffs.build_consumption_decision(active).replacement is None


def test_registry_handoffs_are_pure_and_facade_delegates_to_policy() -> None:
    handoffs_path = (
        Path(__file__).resolve().parents[3]
        / "services/agent_runs/registry_handoffs.py"
    )
    registry_path = (
        Path(__file__).resolve().parents[3] / "services/agent_runs/registry.py"
    )
    handoffs_source = handoffs_path.read_text(encoding="utf-8")
    handoffs_tree = ast.parse(handoffs_source)
    imports: set[tuple[int, str]] = set()
    for node in ast.walk(handoffs_tree):
        if isinstance(node, ast.Import):
            imports.update((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add((node.level, node.module))

    assert imports == {
        (0, "__future__"),
        (0, "dataclasses"),
        (0, "typing"),
        (1, "contracts"),
        (1, "registry_contracts"),
        (1, "registry_queries"),
    }
    assert "asyncio.Lock" not in handoffs_source
    assert "safe_inc" not in handoffs_source
    assert "safe_gauge" not in handoffs_source
    assert "logger" not in handoffs_source
    assert "_claims" not in handoffs_source
    assert "self._runs" not in handoffs_source
    assert "_store" not in handoffs_source
    assert "notify" not in handoffs_source
    assert "claim_sequence" not in handoffs_source
    assert registry_handoffs.__all__ == [
        "ConsumptionDecision",
        "HandoffClaimDecision",
        "ReadyHandoffProjection",
        "RunReplacement",
        "SettlementDecision",
        "agent_run_key",
        "build_acknowledge_replacements",
        "build_claim_decision",
        "build_consumption_decision",
        "build_release_replacements",
        "select_ready_handoffs",
    ]

    registry_source = registry_path.read_text(encoding="utf-8")
    registry_tree = ast.parse(registry_source)
    registry_imports_handoffs = any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            in {"registry_handoffs", "backend.services.agent_runs.registry_handoffs"}
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module is None
            and any(alias.name == "registry_handoffs" for alias in node.names)
        )
        for node in ast.walk(registry_tree)
    )
    assert registry_imports_handoffs is True
    assert "candidates: list[_registry_contracts.LocalAgentRun]" not in registry_source
    assert "claimed_ready_count = 0" not in registry_source
    assert "active_entries: list[_registry_contracts.LocalAgentRun]" not in registry_source
    assert "result_consumed=True, result_claim_id=None" not in registry_source
    assert "replace(entry, result_claim_id=None)" not in registry_source
