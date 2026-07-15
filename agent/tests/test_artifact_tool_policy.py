"""Tests for artifact tool exposure policy used by planner/catalog gating."""

from __future__ import annotations

import sys
import types

from agent.tool_runtime.artifact_tool_policy import (
    ARTIFACT_READ_TOOL_ID,
    ARTIFACT_SEARCH_TOOL_ID,
    ArtifactToolExposure,
    apply_artifact_tool_exposure,
    resolve_and_apply_exposure,
    resolve_artifact_tool_exposure,
    task_has_persisted_artifacts,
)


def test_apply_artifact_tool_exposure_hides_disallowed_tools() -> None:
    exposure = ArtifactToolExposure(
        allow_search=False,
        allow_read=False,
        has_persisted_artifacts=False,
        known_artifact_ids=(),
        evidence_gap_signal=False,
    )
    result = apply_artifact_tool_exposure(
        ["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        exposure=exposure,
    )
    assert result == ["shell.exec"]


def test_resolve_exposure_collects_known_artifact_id_without_exposing_read(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    artifact_id = "11111111-1111-4111-8111-111111111111"
    metadata = {
        "last_tool_result_compact": {
            "artifact_refs": [{"artifact_id": artifact_id}],
        }
    }
    exposure = resolve_artifact_tool_exposure(
        task_id=7,
        metadata=metadata,
        user_message="summarize evidence",
        next_tool_hint="",
    )
    assert exposure.allow_read is False
    assert artifact_id in exposure.known_artifact_ids


def test_resolve_exposure_records_evidence_gap_without_exposing_search(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    # Fix 1 (runner_control follow-up): artifact-tool policy no longer reads
    # transcript history. The evidence-gap signal surfaces from
    # structured metadata hints and the classifier-derived
    # ``intent_brief`` instead.
    exposure = resolve_artifact_tool_exposure(
        task_id=9,
        metadata={},
        user_message="continue",
        next_tool_hint="fill the evidence gap from saved output",
    )
    assert exposure.evidence_gap_signal is True
    assert exposure.allow_search is False


def test_resolve_and_apply_exposure_uses_payload_from_context() -> None:
    artifact_id = "22222222-2222-4222-8222-222222222222"
    context = {
        "artifact_tool_exposure": {
            "allow_search": False,
            "allow_read": True,
            "has_persisted_artifacts": False,
            "known_artifact_ids": [artifact_id, ""],
            "evidence_gap_signal": False,
        }
    }
    filtered_tools, exposure_metadata = resolve_and_apply_exposure(
        context=context,
        resolved_tools=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        available_tool_ids=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        user_message="",
    )
    assert filtered_tools == ["shell.exec"]
    assert exposure_metadata["allow_search"] is False
    assert exposure_metadata["allow_read"] is True
    assert exposure_metadata["known_artifact_ids"] == [artifact_id]


def test_resolve_and_apply_exposure_resolves_and_stores_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    # Fix 1 (runner_control follow-up): no ``history`` channel on the context.
    # The context's poisoned transcript (if present) must be ignored.
    context = {
        "task_id": "17",
        "next_tool_hint": "fill the evidence gap from saved output",
    }
    filtered_tools, exposure_metadata = resolve_and_apply_exposure(
        context=context,
        resolved_tools=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        available_tool_ids=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        user_message="continue",
    )
    assert filtered_tools == ["shell.exec"]
    assert exposure_metadata["allow_search"] is False
    assert exposure_metadata["allow_read"] is False
    assert context["artifact_tool_exposure"] == exposure_metadata


def test_resolve_exposure_extracts_artifact_id_from_intent_brief(monkeypatch) -> None:
    """Fix 1: brief's ``relevant_memory_fragments`` feed the artifact-id scan.

    The policy previously scanned the last few transcript entries for
    UUIDs. After Fix 1 the brief is the text-signal source: a UUID
    embedded in a memory fragment must still surface as a
    known_artifact_id.
    """
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    artifact_id = "33333333-3333-4333-8333-333333333333"
    brief = {
        "resolved_user_intent": "inspect the archived output",
        "next_operational_goal": None,
        "success_condition": "",
        "explicit_constraints": [],
        "relevant_memory_fragments": [
            f"prior saved artifact id: {artifact_id}",
        ],
        "retrieval_hints": [],
    }

    exposure = resolve_artifact_tool_exposure(
        task_id=11,
        metadata={},
        user_message="continue",
        next_tool_hint="",
        intent_brief=brief,
    )

    assert artifact_id in exposure.known_artifact_ids
    assert exposure.allow_read is False


def test_resolve_exposure_is_empty_when_brief_and_history_both_absent(monkeypatch) -> None:
    """Fix 1: no brief + no transcript + no metadata hints -> empty exposure."""
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    exposure = resolve_artifact_tool_exposure(
        task_id=12,
        metadata={},
        user_message="",
        next_tool_hint="",
        intent_brief=None,
    )
    assert exposure.allow_search is False
    assert exposure.allow_read is False
    assert exposure.known_artifact_ids == ()
    assert exposure.evidence_gap_signal is False


def test_resolve_and_apply_exposure_ignores_poisoned_history_key(monkeypatch) -> None:
    """Fix 1: a poisoned ``history`` key in context must not leak into exposure.

    Before the Fix, ``resolve_and_apply_exposure`` read
    ``context["history"]`` and fed it into the policy. After the Fix
    the context's history channel is gone entirely; a caller that
    still plants a poisoned list there must not be able to influence
    the exposure signal.
    """
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    poisoned_artifact_id = "44444444-4444-4444-8444-444444444444"
    context = {
        "task_id": "7",
        # A regression could try to revive the transcript channel; the
        # poisoned history must be ignored by the policy.
        "history": [
            {"role": "assistant", "content": f"artifact {poisoned_artifact_id}"}
        ],
        "next_tool_hint": "",
    }

    filtered_tools, exposure_metadata = resolve_and_apply_exposure(
        context=context,
        resolved_tools=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        available_tool_ids=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        user_message="continue",
    )

    # The poisoned transcript artifact id did not become a
    # known_artifact_id, so read access is not enabled by it.
    assert poisoned_artifact_id not in exposure_metadata["known_artifact_ids"]
    # And the artifact tools stay hidden when no other signal triggers them.
    assert ARTIFACT_SEARCH_TOOL_ID not in filtered_tools
    assert ARTIFACT_READ_TOOL_ID not in filtered_tools


def test_resolve_and_apply_exposure_reads_brief_from_context(monkeypatch) -> None:
    """Fix 1: ``resolve_and_apply_exposure`` reads the brief from context.

    When ``context["intent_brief"]`` carries an evidence-gap
    signal (e.g. "prior output" phrasing in a memory fragment), the
    policy records the evidence-gap signal without exposing hidden DB tools.
    """
    monkeypatch.setattr(
        "agent.tool_runtime.artifact_tool_policy.task_has_persisted_artifacts",
        lambda _task_id: False,
    )
    context = {
        "task_id": "19",
        "intent_brief": {
            "resolved_user_intent": "resume prior work",
            "next_operational_goal": None,
            "success_condition": None,
            "explicit_constraints": [],
            "relevant_memory_fragments": [
                "we saw a prior output artifact in this task",
            ],
            "retrieval_hints": [],
        },
        "next_tool_hint": "",
    }

    filtered_tools, exposure_metadata = resolve_and_apply_exposure(
        context=context,
        resolved_tools=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        available_tool_ids=["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        user_message="continue",
    )

    assert exposure_metadata["evidence_gap_signal"] is True
    assert exposure_metadata["allow_search"] is False
    assert ARTIFACT_SEARCH_TOOL_ID not in filtered_tools


def test_task_has_persisted_artifacts_uses_existence_method(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeSession:
        def close(self) -> None:
            calls["closed"] = True

    class FakeArtifactMemoryService:
        def __init__(self, db) -> None:
            calls["db"] = db

        def task_has_persisted_artifacts(self, *, task_id: int) -> bool:
            calls["task_id"] = task_id
            return True

        def search_task_artifacts(self, *args, **kwargs):
            raise AssertionError("search_task_artifacts should not be called for existence checks")

    fake_db_module = types.ModuleType("backend.database")
    fake_db_module.SessionLocal = lambda: FakeSession()  # type: ignore[attr-defined]
    fake_memory_module = types.ModuleType("backend.services.artifact.memory_service")
    fake_memory_module.ArtifactMemoryService = FakeArtifactMemoryService  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "backend.database", fake_db_module)
    monkeypatch.setitem(sys.modules, "backend.services.artifact.memory_service", fake_memory_module)

    assert task_has_persisted_artifacts(42) is True
    assert calls["task_id"] == 42
    assert calls["closed"] is True
