"""Unit tests for turn-level compression orchestration service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import Mock

import pytest

from backend.services.chat.conversation_history_reader import (
    SYSTEM_SUMMARY_MESSAGE_TYPE,
    ConversationHistoryReader,
)
from backend.services.langgraph_chat.compression.context_models import (
    CompressionPassResult,
    ContextCompressionOutcome,
    CompressionRequiredError,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.window_manager import (
    ContextWindowDecision,
    ContextWindowManager,
    ContextWindowSnapshot,
)
from backend.services.langgraph_chat.compression.turn_service import (
    TurnCompressionService,
    _build_compression_epoch_id,
    build_compaction_turn_candidates,
    split_aligned_transcript_into_turn_groups,
)
from backend.services.langgraph_chat.runtime.run_lifecycle import RunLifecycleService


def _decision_for_test(
    *,
    ceiling_reached: bool,
    task_id: int = 1,
    conversation_id: str = "conv-1",
    max_tokens: int = 128_000,
    used_tokens: int = 640,
    remaining_tokens: int = 127_360,
    ratio: float = 0.005,
) -> ContextWindowDecision:
    return ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=task_id,
            conversation_id=conversation_id,
            max_tokens=max_tokens,
            used_tokens=used_tokens,
            remaining_tokens=remaining_tokens,
            ratio=ratio,
            ceiling_reached=ceiling_reached,
        ),
        ceiling_reached=ceiling_reached,
        recommended_next_action="compress" if ceiling_reached else "none",
        compression_candidate=ceiling_reached,
    )


def _pass_result(*, output_text: str, output_tokens: int) -> CompressionPassResult:
    return CompressionPassResult(
        pass_name="pass1",
        system_template_id="context_compression_system_pass1",
        user_template_id="context_compression_user_pass1",
        output_text=output_text,
        output_tokens=output_tokens,
        target_max_tokens=max(output_tokens, 1),
        within_target=True,
    )


def test_aligned_turn_groups_use_position_not_duplicate_content() -> None:
    """Canonical grouping preserves source IDs without content matching."""
    history = [
        {"role": "system", "content": "same"},
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "same"},
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "same"},
    ]

    leading, turns = split_aligned_transcript_into_turn_groups(
        history,
        [10, 11, 12, 13, 14],
    )

    assert leading.messages == (history[0],)
    assert leading.source_message_ids == (10,)
    assert [turn.messages for turn in turns] == [
        (history[1], history[2]),
        (history[3], history[4]),
    ]
    assert [turn.source_message_ids for turn in turns] == [(11, 12), (13, 14)]
    assert all("source_message_id" not in message for message in history)


def test_aligned_turn_groups_reject_misaligned_source_ids() -> None:
    """A partial sidecar fails instead of guessing IDs from message content."""
    history = [
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "same"},
    ]

    with pytest.raises(ValueError, match="equal lengths"):
        split_aligned_transcript_into_turn_groups(history, [10])


def test_compaction_candidates_reduce_only_by_complete_turns() -> None:
    """Fallback candidates retain five, four, then three intact turns."""
    history = [{"role": "system", "content": "prior summary"}]
    source_ids = [100]
    expected_turn_ids: list[tuple[int, ...]] = []
    for turn_number in range(1, 9):
        turn_ids = (
            turn_number * 10,
            turn_number * 10 + 1,
            turn_number * 10 + 2,
        )
        expected_turn_ids.append(turn_ids)
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"PTR {turn_number}"},
                {"role": "tool", "content": f"tool {turn_number}"},
            ]
        )
        source_ids.extend(turn_ids)

    candidates = build_compaction_turn_candidates(history, source_ids)

    assert [candidate.retained_turn_count for candidate in candidates] == [5, 4, 3]
    assert all(candidate.leading_group.source_message_ids == (100,) for candidate in candidates)
    assert [
        [group.source_message_ids for group in candidate.expired_turn_groups]
        for candidate in candidates
    ] == [
        expected_turn_ids[:3],
        expected_turn_ids[:4],
        expected_turn_ids[:5],
    ]
    assert [
        [group.source_message_ids for group in candidate.retained_turn_groups]
        for candidate in candidates
    ] == [
        expected_turn_ids[3:],
        expected_turn_ids[4:],
        expected_turn_ids[5:],
    ]


def test_compaction_candidates_never_retain_below_hard_minimum() -> None:
    """History with fewer than three complete turns has no valid compaction tail."""
    history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "answer two"},
    ]

    assert build_compaction_turn_candidates(history, [1, 2, 3, 4]) == ()


@pytest.mark.parametrize(
    ("turn_count", "expected_shape"),
    [
        (3, []),
        (4, [(3, 1)]),
        (5, [(4, 1), (3, 2)]),
    ],
)
def test_compaction_candidates_require_a_real_expired_turn(
    turn_count: int,
    expected_shape: List[tuple[int, int]],
) -> None:
    """Three-to-five-turn boundaries never expose an empty summary prefix."""
    history, source_ids = _complete_turn_history(turn_count)

    candidates = build_compaction_turn_candidates(history, source_ids)

    assert [
        (candidate.retained_turn_count, len(candidate.expired_turn_groups))
        for candidate in candidates
    ] == expected_shape
    assert all(candidate.summarized_through_message_id is not None for candidate in candidates)


def _outcome_from_request(
    request: ContextCompressionRequest,
    *,
    final_text: str,
    original_tokens: int,
    final_tokens: int,
    pass_count: int = 1,
    degraded: bool = False,
    fallback_reason: Optional[str] = None,
) -> ContextCompressionOutcome:
    return ContextCompressionOutcome(
        request=request,
        original_tokens=original_tokens,
        final_tokens=final_tokens,
        final_text=final_text,
        pass_results=tuple(_pass_result(output_text=final_text, output_tokens=final_tokens) for _ in range(pass_count)),
        pass_count=pass_count,
        degraded=degraded,
        fallback_reason=fallback_reason,
    )


class _FakeSession:
    def __init__(self, state: Dict[str, Any]) -> None:
        self.state = state

    def close(self) -> None:
        self.state["sessions_closed"] = self.state.get("sessions_closed", 0) + 1


class _FakeContextWindowManager:
    def __init__(
        self,
        *,
        decision: ContextWindowDecision,
        max_tokens: int,
        estimate_tokens: int,
        state: Dict[str, Any],
    ) -> None:
        self._decision = decision
        self.max_tokens = max_tokens
        self._estimate_tokens = estimate_tokens
        self._state = state

    def evaluate_classifier_prompt(
        self,
        *,
        task_id: int,
        conversation_id: str,
        prompt_tokens: int,
        reserved_output_tokens: int,
    ) -> tuple[ContextWindowDecision, SimpleNamespace]:
        _kwargs = {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "prompt_tokens": prompt_tokens,
            "reserved_output_tokens": reserved_output_tokens,
        }
        self._state["evaluate_called"] = self._state.get("evaluate_called", 0) + 1
        self._state["last_evaluate_kwargs"] = dict(_kwargs)
        usable_prompt_tokens = self.max_tokens - reserved_output_tokens
        return self._decision, SimpleNamespace(
            usable_prompt_tokens=usable_prompt_tokens,
            trigger_tokens=int(usable_prompt_tokens * 0.8),
            reserved_output_tokens=reserved_output_tokens,
            override_active=False,
        )

    def estimate_tokens_from_history(self, **_kwargs: Any) -> int:
        self._state["estimate_calls"] = self._state.get("estimate_calls", 0) + 1
        self._state["last_estimate_kwargs"] = dict(_kwargs)
        return self._estimate_tokens

    def estimate_tokens_from_openai_history(self, **kwargs: Any) -> int:
        return self.estimate_tokens_from_history(provider="openai", **kwargs)


class _FakeCompressionService:
    def __init__(
        self,
        *,
        enabled: bool = True,
        error: Optional[BaseException] = None,
        outcome: Optional[ContextCompressionOutcome] = None,
        outcome_builder: Optional[Callable[[ContextCompressionRequest], ContextCompressionOutcome]] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.enabled = enabled
        self.error = error
        self.outcome = outcome
        self.outcome_builder = outcome_builder
        self._state = state or {}
        self.calls: List[Dict[str, Any]] = []

    def is_enabled(self) -> bool:
        self._state["is_enabled_calls"] = self._state.get("is_enabled_calls", 0) + 1
        return self.enabled

    async def compress(
        self,
        request: ContextCompressionRequest,
        **kwargs: Any,
    ) -> ContextCompressionOutcome:
        self.calls.append({"request": request, **kwargs})
        if self.error:
            raise self.error
        if self.outcome_builder:
            return self.outcome_builder(request)
        assert self.outcome is not None
        return self.outcome


class _FakeCompressionSnapshotRepository:
    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    def persist_snapshot(self, **kwargs: Any) -> Any:
        self._state.setdefault("order", []).append("persist_snapshot")
        self._state["persist_calls"] = self._state.get("persist_calls", [])
        self._state["persist_calls"].append(kwargs)
        persisted_snapshot = self._state.get("persisted_snapshot")
        if persisted_snapshot is not None:
            return persisted_snapshot
        return SimpleNamespace(
            message=kwargs["summary_text"],
            token_count=kwargs["token_count"],
        )


def _context_window_manager_factory(
    decision: ContextWindowDecision,
    estimate_tokens: int,
    state: Optional[Dict[str, Any]] = None,
) -> Callable[[Optional[int]], _FakeContextWindowManager]:
    manager_state: Dict[str, Any] = state if state is not None else {}

    def factory(max_tokens: Optional[int]) -> _FakeContextWindowManager:
        max_token_value = max_tokens if max_tokens is not None else 128_000
        manager_state["constructed_max_tokens"] = max_token_value
        return _FakeContextWindowManager(
            decision=decision,
            max_tokens=max_token_value,
            estimate_tokens=estimate_tokens,
            state=manager_state,
        )

    return factory



def _compression_snapshot_repository_factory(
    state: Dict[str, Any],
) -> Callable[..., _FakeCompressionSnapshotRepository]:
    return lambda _db: _FakeCompressionSnapshotRepository(state)


def _session_factory(state: Dict[str, Any]) -> Callable[[], _FakeSession]:
    return lambda: _FakeSession(state)


def _complete_turn_history(turn_count: int = 6) -> tuple[List[Dict[str, str]], List[int]]:
    """Build a source-aligned transcript with enough complete turns to compact."""
    history: List[Dict[str, str]] = []
    source_ids: List[int] = []
    for turn_number in range(1, turn_count + 1):
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"answer {turn_number}"},
            ]
        )
        source_ids.extend((turn_number * 10, turn_number * 10 + 1))
    return history, source_ids


@pytest.mark.asyncio
async def test_three_turn_overflow_fails_before_compressor_work() -> None:
    """Exactly three turns cannot produce a cutoff and never call the provider."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="must not be used",
            original_tokens=901,
            final_tokens=100,
        ),
        state={},
    )
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        session_factory=lambda: _FakeSession({}),
    )
    history, source_ids = _complete_turn_history(3)

    with pytest.raises(CompressionRequiredError) as exc_info:
        await service.prepare_preturn_history(
            task_id=303,
            conversation_id="conv-303",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=1_000,
            request_prompt_tokens=901,
            reserved_output_tokens=100,
            candidate_classifier_prompt_counter=lambda _history: 800,
        )

    assert exc_info.value.reason == "context_uncompactable"
    assert compression_service.calls == []


@pytest.mark.asyncio
async def test_three_turn_soft_overflow_fails_open_before_compressor_work() -> None:
    """A hard-fitting three-turn request continues unchanged without provider work."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="must not be used",
            original_tokens=850,
            final_tokens=100,
        ),
        state={},
    )
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        session_factory=lambda: _FakeSession({}),
    )
    history, source_ids = _complete_turn_history(3)

    result_history, _, compression, _ = await service.prepare_preturn_history(
        task_id=3031,
        conversation_id="conv-3031",
        history=history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=850,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=lambda _history: 800,
    )

    assert result_history == history
    assert compression == {
        "applied": False,
        "reason": "context_uncompactable",
        "warning": True,
        "original_request_fits": True,
    }
    assert compression_service.calls == []


@pytest.mark.asyncio
async def test_four_turn_overflow_starts_with_prior_summary_and_first_expired_turn() -> None:
    """The first valid four-turn candidate persists the exact first-turn cutoff."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="summary through turn one",
            original_tokens=900,
            final_tokens=120,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    turns, turn_source_ids = _complete_turn_history(4)
    history = [{"role": "system", "content": "prior durable summary"}, *turns]
    source_ids = [1, *turn_source_ids]
    candidate_histories: List[List[Dict[str, Any]]] = []

    await service.prepare_preturn_history(
        task_id=304,
        conversation_id="conv-304",
        history=history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=900,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=lambda candidate: (
            candidate_histories.append(candidate) or 400
        ),
    )

    assert compression_service.calls[0]["request"].conversation_history == [
        history[0],
        *turns[:2],
    ]
    assert candidate_histories == [
        [{"role": "system", "content": "summary through turn one"}, *turns[2:]]
    ]
    assert persistence_state["persist_calls"][0]["through_message_id"] == 11


@pytest.mark.asyncio
async def test_five_turn_fallback_carries_only_real_summary_and_newly_expired_turn() -> None:
    """Five-to-three fallback never carries a summary produced from empty input."""
    compression_call_count = 0

    def _build_outcome(request: ContextCompressionRequest) -> ContextCompressionOutcome:
        nonlocal compression_call_count
        compression_call_count += 1
        return _outcome_from_request(
            request,
            final_text=f"candidate summary {compression_call_count}",
            original_tokens=901,
            final_tokens=120,
        )

    compression_service = _FakeCompressionService(
        outcome_builder=_build_outcome,
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    turns, turn_source_ids = _complete_turn_history(5)
    history = [{"role": "system", "content": "prior durable summary"}, *turns]
    source_ids = [1, *turn_source_ids]
    candidate_counts = iter((901, 800))

    await service.prepare_preturn_history(
        task_id=305,
        conversation_id="conv-305",
        history=history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=901,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=lambda _candidate: next(candidate_counts),
    )

    assert [
        call["request"].conversation_history for call in compression_service.calls
    ] == [
        [history[0], *turns[:2]],
        [
            {"role": "system", "content": "candidate summary 1"},
            *turns[2:4],
        ],
    ]
    assert persistence_state["persist_calls"][0]["through_message_id"] == 21


@pytest.mark.asyncio
async def test_prepare_preturn_history_non_ceiling_emits_once_and_keeps_history() -> None:
    emitted: List[Dict[str, Any]] = []
    decision = _decision_for_test(
        ceiling_reached=False,
        max_tokens=1_000_000,
        used_tokens=640,
        remaining_tokens=999_360,
        ratio=0.00064,
    )
    manager_state: Dict[str, Any] = {}
    compression_service = _FakeCompressionService(state={})

    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(
            decision=decision,
            estimate_tokens=640,
            state=manager_state,
        ),
        context_compression_service_factory=lambda: compression_service,
        session_factory=lambda: _FakeSession(manager_state),
    )

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
            lambda **kwargs: emitted.append(kwargs),
        )

        original_history = [{"role": "assistant", "content": "hello"}]
        history_for_facade, context_window_metadata, compression_metadata, emitted_flag = (
            await service.prepare_preturn_history(
                task_id=21,
                conversation_id="conv-21",
                history=original_history,
                history_source_message_ids=[1],
                provider="anthropic",
                model="claude-sonnet-4-6",
                context_limit_tokens=1_000_000,
                request_prompt_tokens=640,
                reserved_output_tokens=1,
                candidate_classifier_prompt_counter=lambda _history: 640,
            )
        )
    finally:
        monkeypatch.undo()

    assert history_for_facade == original_history
    assert emitted_flag is True
    assert compression_metadata == {"applied": False, "reason": "below_trigger"}
    assert context_window_metadata is not None
    assert context_window_metadata["ceiling_reached"] is False
    assert manager_state["constructed_max_tokens"] == 1_000_000
    assert manager_state["last_evaluate_kwargs"]["prompt_tokens"] == 640
    assert manager_state["last_evaluate_kwargs"]["reserved_output_tokens"] == 1
    assert len(emitted) == 1
    assert emitted[0]["task_id"] == 21
    assert emitted[0]["conversation_id"] == context_window_metadata["conversation_id"]
    assert emitted[0]["max_tokens"] == 1_000_000
    assert emitted[0]["used_tokens"] == 640
    assert emitted[0]["remaining_tokens"] == 999_360
    assert emitted[0]["ratio"] == 0.00064
    assert emitted[0]["ceiling_reached"] is False
    assert emitted[0]["recommended_next_action"] == "none"
    assert emitted[0]["compression_candidate"] is False


@pytest.mark.asyncio
async def test_prepare_preturn_history_uses_exact_request_prompt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared request tokens drive the soft trigger without history recounting."""
    emitted: List[Dict[str, Any]] = []
    captured: List[Dict[str, Any]] = []
    monkeypatch.delenv(
        "LANGGRAPH_CONTEXT_COMPACTION_TRIGGER_TOKENS_OVERRIDE",
        raising=False,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
        lambda **kwargs: emitted.append(kwargs),
    )
    service = TurnCompressionService(
        context_window_manager_factory=lambda max_tokens: ContextWindowManager(
            max_tokens=max_tokens
        )
    )

    _, metadata, compression, emitted_flag = await service.prepare_preturn_history(
        task_id=21,
        conversation_id="conv-21",
        turn_sequence=7,
        history=[{"role": "assistant", "content": "history is not recounted"}],
        history_source_message_ids=[1],
        provider="openai",
        model="gpt-5.2",
        context_limit_tokens=128_000,
        request_prompt_tokens=7_321,
        reserved_output_tokens=32_000,
        candidate_classifier_prompt_counter=lambda _history: 7_321,
        on_context_window_snapshot=lambda snapshot: captured.append(dict(snapshot)),
    )

    assert emitted_flag is True
    assert compression == {"applied": False, "reason": "below_trigger"}
    assert metadata is not None
    assert metadata["used_tokens"] == 7_321
    assert metadata["max_tokens"] == 128_000
    assert metadata["reserved_output_tokens"] == 32_000
    assert metadata["usable_prompt_tokens"] == 96_000
    assert metadata["trigger_tokens"] == 76_800
    assert metadata["trigger_override_active"] is False
    assert metadata["turn_sequence"] == 7
    assert metadata["revision"] == 7
    assert metadata["snapshot_kind"] == "measured"
    assert captured == [metadata]
    assert emitted[0]["used_tokens"] == 7_321
    assert emitted[0]["revision"] == 7
    assert emitted[0]["snapshot_kind"] == "measured"


@pytest.mark.asyncio
async def test_prepare_preturn_history_disabled_path_raises_with_reason_and_detail() -> None:
    emitted: List[Dict[str, Any]] = []
    decision = _decision_for_test(ceiling_reached=True, used_tokens=128_000, remaining_tokens=0, ratio=1.0)

    compression_service = _FakeCompressionService(enabled=False, state={})
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(decision=decision, estimate_tokens=128_000),
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            "backend.services.langgraph_chat.compression.turn_service._default_emit_context_window_event",
            lambda **kwargs: emitted.append(kwargs),
        )
        with pytest.raises(CompressionRequiredError) as exc_info:
            await service.prepare_preturn_history(
                task_id=22,
                conversation_id="conv-22",
                history=[],
                history_source_message_ids=[],
                model="gpt-5.2",
                context_limit_tokens=128_000,
                request_prompt_tokens=128_000,
                reserved_output_tokens=1,
                candidate_classifier_prompt_counter=lambda _history: 128_000,
            )
    finally:
        monkeypatch.undo()

    assert "compression_required_failed" in str(exc_info.value)
    assert "compression service disabled at context ceiling" in str(exc_info.value)
    assert compression_service.calls == []
    assert len(emitted) == 1
    assert emitted[0]["task_id"] == 22


@pytest.mark.asyncio
async def test_prepare_preturn_history_compression_call_failure_preserves_failure_detail() -> None:
    decision = _decision_for_test(ceiling_reached=True, used_tokens=128_000, remaining_tokens=0, ratio=1.0)
    compression_service = _FakeCompressionService(enabled=True, error=RuntimeError("llm-fail"), state={})
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(decision=decision, estimate_tokens=128_001),
        context_compression_service_factory=lambda: compression_service,
        session_factory=lambda: _FakeSession({}),
    )

    history, source_ids = _complete_turn_history()
    with pytest.raises(CompressionRequiredError) as exc_info:
        await service.prepare_preturn_history(
            task_id=23,
            conversation_id="conv-23",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 128_000,
        )

    assert "compression_required_failed" in str(exc_info.value)
    assert "compression call failed at context ceiling" in str(exc_info.value)


@pytest.mark.asyncio
async def test_soft_trigger_failure_continues_with_original_request_when_it_fits() -> None:
    """A compaction failure above the soft trigger is a warning below hard fit."""
    lifecycle_events: List[Dict[str, Any]] = []

    async def _publish_lifecycle(**kwargs: Any) -> bool:
        lifecycle_events.append(dict(kwargs))
        return True

    service = TurnCompressionService(
        context_compression_service_factory=lambda: _FakeCompressionService(
            error=RuntimeError("provider failed"),
            state={},
        ),
        session_factory=lambda: _FakeSession({}),
        publish_context_window_lifecycle_event=_publish_lifecycle,
    )
    original_history, source_ids = _complete_turn_history()

    history, _, compression, _ = await service.prepare_preturn_history(
        task_id=232,
        conversation_id="conv-232",
        turn_id="turn-232",
        history=original_history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=850,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=lambda _history: 800,
    )

    assert history == original_history
    assert compression == {
        "applied": False,
        "reason": "compression_call_failed",
        "warning": True,
        "original_request_fits": True,
    }
    assert [event["state"] for event in lifecycle_events] == [
        "compacting",
        "failed",
    ]


@pytest.mark.asyncio
async def test_soft_trigger_failure_stops_when_original_request_exceeds_hard_limit() -> None:
    """The same exhausted compaction failure remains terminal above hard fit."""
    service = TurnCompressionService(
        context_compression_service_factory=lambda: _FakeCompressionService(
            error=RuntimeError("provider failed"),
            state={},
        ),
        session_factory=lambda: _FakeSession({}),
    )

    history, source_ids = _complete_turn_history()
    with pytest.raises(CompressionRequiredError, match="compression_required_failed"):
        await service.prepare_preturn_history(
            task_id=233,
            conversation_id="conv-233",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=1_000,
            request_prompt_tokens=901,
            reserved_output_tokens=100,
            candidate_classifier_prompt_counter=lambda _history: 800,
        )


@pytest.mark.asyncio
async def test_prepare_preturn_history_does_not_resend_signature_shaped_type_error() -> None:
    """A compressor TypeError is one failed provider attempt, never a compatibility retry."""
    decision = _decision_for_test(
        ceiling_reached=True,
        used_tokens=128_000,
        remaining_tokens=0,
        ratio=1.0,
    )
    compression_service = _FakeCompressionService(
        enabled=True,
        error=TypeError("got an unexpected keyword during provider execution"),
        state={},
    )
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(
            decision=decision,
            estimate_tokens=128_001,
        ),
        context_compression_service_factory=lambda: compression_service,
        session_factory=lambda: _FakeSession({}),
    )

    history, source_ids = _complete_turn_history()
    with pytest.raises(CompressionRequiredError):
        await service.prepare_preturn_history(
            task_id=231,
            conversation_id="conv-231",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            runtime_services=object(),
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 128_000,
        )

    assert len(compression_service.calls) == 1


@pytest.mark.asyncio
async def test_prepare_preturn_history_empty_compressed_text_raises() -> None:
    decision = _decision_for_test(ceiling_reached=True, used_tokens=128_000, remaining_tokens=0, ratio=1.0)

    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="   ",
            original_tokens=128_000,
            final_tokens=0,
        ),
        state={},
    )
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(decision=decision, estimate_tokens=128_001),
        context_compression_service_factory=lambda: compression_service,
        session_factory=lambda: _FakeSession({}),
    )

    history, source_ids = _complete_turn_history()
    with pytest.raises(CompressionRequiredError) as exc_info:
        await service.prepare_preturn_history(
            task_id=24,
            conversation_id="conv-24",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 128_000,
        )

    assert "compression_required_failed" in str(exc_info.value)
    assert "compression returned empty context at ceiling" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prepare_preturn_history_success_populates_system_history_and_compression_metadata() -> None:
    decision = _decision_for_test(ceiling_reached=True, used_tokens=128_000, remaining_tokens=0, ratio=1.0)

    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="compressed text",
            original_tokens=128_000,
            final_tokens=920,
            pass_count=2,
            degraded=False,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(decision=decision, estimate_tokens=128_001),
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )

    original_history, source_ids = _complete_turn_history()
    history_for_facade, context_window_metadata, compression_metadata, emitted_flag = (
        await service.prepare_preturn_history(
            task_id=25,
            conversation_id="conv-25",
            history=list(original_history),
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 1_000,
        )
    )

    assert emitted_flag is True
    # Compression runs before classification and leaves the canonical
    # transcript unchanged for non-classifier prompt roles.
    assert history_for_facade == original_history
    assert context_window_metadata is not None
    assert context_window_metadata["compression"]["applied"] is True
    assert context_window_metadata["compression"]["pass_count"] == 2
    assert context_window_metadata["compression"]["fallback_reason"] is None
    assert context_window_metadata["compression"]["epoch_id"] == (
        _build_compression_epoch_id(
            task_id=25,
            conversation_id="conv-25",
            source_tokens=128_000,
            source_message_ids=source_ids,
        )
    )
    assert compression_service.calls and compression_service.calls[0]["request"].model == "gpt-5.2"
    assert compression_service.calls[0]["request"].provider == "openai"
    assert compression_service.calls[0]["request"].conversation_history == original_history[:2]
    assert "source_message_id" not in compression_service.calls[0]["request"].conversation_history[0]


@pytest.mark.asyncio
async def test_compaction_lifecycle_is_ordered_around_provider_work() -> None:
    """Compacting is sequenced before provider work and completed afterward."""
    order: List[str] = []
    lifecycle_events: List[Dict[str, Any]] = []

    async def _publish_lifecycle(**kwargs: Any) -> bool:
        lifecycle_events.append(dict(kwargs))
        order.append(kwargs["state"])
        return True

    def _build_outcome(request: ContextCompressionRequest) -> ContextCompressionOutcome:
        order.append("provider_call")
        assert order == ["compacting", "provider_call"]
        return _outcome_from_request(
            request,
            final_text="compressed text",
            original_tokens=128_000,
            final_tokens=920,
        )

    decision = _decision_for_test(
        ceiling_reached=True,
        used_tokens=128_000,
        remaining_tokens=0,
        ratio=1.0,
    )
    compression_service = _FakeCompressionService(
        outcome_builder=_build_outcome,
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(
            decision=decision,
            estimate_tokens=128_001,
        ),
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
        publish_context_window_lifecycle_event=_publish_lifecycle,
    )

    history, source_ids = _complete_turn_history()
    await service.prepare_preturn_history(
        task_id=251,
        conversation_id="conv-251",
        turn_id="turn-251",
        history=history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=128_000,
        request_prompt_tokens=128_000,
        reserved_output_tokens=1,
        candidate_classifier_prompt_counter=lambda _history: 1_000,
    )

    assert order == ["compacting", "provider_call", "completed"]
    assert [event["state"] for event in lifecycle_events] == [
        "compacting",
        "completed",
    ]
    assert all(event["task_id"] == 251 for event in lifecycle_events)
    assert all(event["conversation_id"] == "conv-251" for event in lifecycle_events)
    assert all(event["turn_id"] == "turn-251" for event in lifecycle_events)
    assert {event["epoch_id"] for event in lifecycle_events} == {
        _build_compression_epoch_id(
            task_id=251,
            conversation_id="conv-251",
            source_tokens=128_000,
            source_message_ids=source_ids,
        )
    }


@pytest.mark.asyncio
async def test_compaction_failure_awaits_matching_failed_lifecycle() -> None:
    """Provider failure closes the exact lifecycle before propagating."""
    lifecycle_events: List[Dict[str, Any]] = []

    async def _publish_lifecycle(**kwargs: Any) -> bool:
        lifecycle_events.append(dict(kwargs))
        return True

    decision = _decision_for_test(
        ceiling_reached=True,
        used_tokens=128_000,
        remaining_tokens=0,
        ratio=1.0,
    )
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(
            decision=decision,
            estimate_tokens=128_001,
        ),
        context_compression_service_factory=lambda: _FakeCompressionService(
            error=RuntimeError("provider failed"),
            state={},
        ),
        session_factory=lambda: _FakeSession({}),
        publish_context_window_lifecycle_event=_publish_lifecycle,
    )

    history, source_ids = _complete_turn_history()
    with pytest.raises(CompressionRequiredError):
        await service.prepare_preturn_history(
            task_id=252,
            conversation_id="conv-252",
            turn_id="turn-252",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 1_000,
        )

    assert [event["state"] for event in lifecycle_events] == [
        "compacting",
        "failed",
    ]
    assert lifecycle_events[0]["epoch_id"] == lifecycle_events[1]["epoch_id"]
    assert lifecycle_events[0]["turn_id"] == lifecycle_events[1]["turn_id"]


@pytest.mark.asyncio
async def test_compaction_cancellation_awaits_matching_cancelled_lifecycle() -> None:
    """Task cancellation closes the exact lifecycle before cancellation escapes."""
    lifecycle_events: List[Dict[str, Any]] = []

    async def _publish_lifecycle(**kwargs: Any) -> bool:
        lifecycle_events.append(dict(kwargs))
        return True

    decision = _decision_for_test(
        ceiling_reached=True,
        used_tokens=128_000,
        remaining_tokens=0,
        ratio=1.0,
    )
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(
            decision=decision,
            estimate_tokens=128_001,
        ),
        context_compression_service_factory=lambda: _FakeCompressionService(
            error=asyncio.CancelledError(),
            state={},
        ),
        session_factory=lambda: _FakeSession({}),
        publish_context_window_lifecycle_event=_publish_lifecycle,
    )

    history, source_ids = _complete_turn_history()
    with pytest.raises(asyncio.CancelledError):
        await service.prepare_preturn_history(
            task_id=253,
            conversation_id="conv-253",
            turn_id="turn-253",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 1_000,
        )

    assert [event["state"] for event in lifecycle_events] == [
        "compacting",
        "cancelled",
    ]
    assert lifecycle_events[0]["epoch_id"] == lifecycle_events[1]["epoch_id"]
    assert lifecycle_events[0]["turn_id"] == lifecycle_events[1]["turn_id"]


@pytest.mark.asyncio
async def test_run_lifecycle_request_cancel_stops_inflight_compaction() -> None:
    """The mounted Stop lifecycle cancels provider work before persistence."""
    lifecycle = RunLifecycleService()
    lifecycle_events: List[Dict[str, Any]] = []
    provider_started = asyncio.Event()
    persistence_state: Dict[str, Any] = {}

    class _BlockingCompressionService(_FakeCompressionService):
        async def compress(
            self,
            request: ContextCompressionRequest,
            **kwargs: Any,
        ) -> ContextCompressionOutcome:
            self.calls.append({"request": request, **kwargs})
            provider_started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled provider call must not complete")

    async def _publish_lifecycle(**kwargs: Any) -> bool:
        lifecycle_events.append(dict(kwargs))
        return True

    db_session = Mock()
    db_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    lifecycle.start_run(
        task_id=2531,
        turn_id="turn-2531",
        conversation_id="conv-2531",
        db_session=db_session,
    )
    service = TurnCompressionService(
        context_compression_service_factory=lambda: _BlockingCompressionService(
            state={}
        ),
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
        publish_context_window_lifecycle_event=_publish_lifecycle,
        run_lifecycle_service=lifecycle,
    )

    history, source_ids = _complete_turn_history()
    compaction_task = asyncio.create_task(
        service.prepare_preturn_history(
            task_id=2531,
            conversation_id="conv-2531",
            turn_id="turn-2531",
            history=history,
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=1_000,
            request_prompt_tokens=850,
            reserved_output_tokens=100,
            candidate_classifier_prompt_counter=lambda _history: 800,
        )
    )
    await provider_started.wait()

    cancel_result = lifecycle.request_cancel(
        task_id=2531,
        turn_id="turn-2531",
        reason="test_stop",
        db_session=db_session,
    )
    with pytest.raises(asyncio.CancelledError):
        await compaction_task

    assert cancel_result["cancelled"] is True
    assert [event["state"] for event in lifecycle_events] == [
        "compacting",
        "cancelled",
    ]
    assert persistence_state.get("persist_calls", []) == []
    lifecycle.end_run(
        task_id=2531,
        turn_id="turn-2531",
        status="cancelled",
        db_session=db_session,
    )


@pytest.mark.asyncio
async def test_prepare_preturn_history_summarizes_only_prior_summary_and_expired_turns() -> None:
    """The retained tail and active message never enter compressor input."""
    decision = _decision_for_test(
        ceiling_reached=True,
        used_tokens=128_000,
        remaining_tokens=0,
        ratio=1.0,
    )
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="compressed text",
            original_tokens=128_000,
            final_tokens=920,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(
            decision=decision,
            estimate_tokens=128_001,
        ),
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    history = [{"role": "system", "content": "prior summary"}]
    source_ids = [100]
    for turn_number in range(1, 9):
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"PTR {turn_number}"},
                {"role": "tool", "content": f"tool {turn_number}"},
            ]
        )
        source_ids.extend(
            [
                turn_number * 10,
                turn_number * 10 + 1,
                turn_number * 10 + 2,
            ]
        )

    history_for_facade, _, _, _ = await service.prepare_preturn_history(
        task_id=26,
        conversation_id="conv-26",
        history=history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=128_000,
        request_prompt_tokens=128_000,
        reserved_output_tokens=1,
        candidate_classifier_prompt_counter=lambda _history: 1_000,
    )

    request = compression_service.calls[0]["request"]
    assert request.conversation_history == history[:10]
    assert request.projected_user_message is None
    assert history_for_facade == history
    assert all(
        "source_message_id" not in item for item in request.conversation_history
    )
    assert all(
        "active user message" not in str(item)
        for item in request.conversation_history
    )


@pytest.mark.asyncio
async def test_prepare_preturn_history_recounts_isolated_classifier_candidate() -> None:
    """Candidate accounting sees only the new summary and five intact turns."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="candidate summary",
            original_tokens=900,
            final_tokens=120,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    history = [{"role": "system", "content": "prior summary"}]
    source_ids = [100]
    for turn_number in range(1, 9):
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"answer {turn_number}"},
                {"role": "tool", "content": f"tool {turn_number}"},
            ]
        )
        source_ids.extend(
            [
                turn_number * 10,
                turn_number * 10 + 1,
                turn_number * 10 + 2,
            ]
        )
    original_history = list(history)
    candidate_histories: List[List[Dict[str, Any]]] = []

    def _count_candidate(candidate_history: List[Dict[str, Any]]) -> int:
        persistence_state.setdefault("order", []).append("validate_candidate")
        candidate_histories.append(candidate_history)
        return 400

    history_for_facade, _, compression_metadata, _ = (
        await service.prepare_preturn_history(
            task_id=27,
            conversation_id="conv-27",
            history=history,
            history_source_message_ids=source_ids,
            provider="openai",
            model="gpt-5.2",
            context_limit_tokens=1_000,
            request_prompt_tokens=900,
            reserved_output_tokens=100,
            candidate_classifier_prompt_counter=_count_candidate,
        )
    )

    expected_candidate = [
        {"role": "system", "content": "candidate summary"},
        *history[10:],
    ]
    assert candidate_histories == [expected_candidate]
    assert all("active user message" not in str(item) for item in expected_candidate)
    assert history_for_facade == original_history
    assert history == original_history
    assert compression_metadata["candidate_prompt_tokens"] == 400
    assert compression_metadata["candidate_retained_turns"] == 5
    assert compression_metadata["candidate_request_fits"] is True
    assert compression_metadata["snapshot_persisted"] is True
    assert persistence_state["order"] == ["validate_candidate", "persist_snapshot"]
    assert persistence_state["persist_calls"] == [
        {
            "task_id": 27,
            "conversation_id": "conv-27",
            "summary_text": "candidate summary",
            "token_count": 120,
            "compression_epoch_id": _build_compression_epoch_id(
                task_id=27,
                conversation_id="conv-27",
                source_tokens=900,
                source_message_ids=source_ids,
            ),
            "source_tokens": 900,
            "through_message_id": 32,
        }
    ]
    assert persistence_state["sessions_closed"] == 1


@pytest.mark.asyncio
async def test_refresh_reconstruction_matches_validated_classifier_candidate() -> None:
    """Fresh readers rebuild the exact persisted summary and retained tail."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="restart-stable summary",
            original_tokens=900,
            final_tokens=120,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    history: List[Dict[str, Any]] = []
    source_ids: List[int] = []
    for turn_number in range(1, 9):
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"answer {turn_number}"},
            ]
        )
        source_ids.extend([turn_number * 10, turn_number * 10 + 1])
    candidate_histories: List[List[Dict[str, Any]]] = []

    await service.prepare_preturn_history(
        task_id=30,
        conversation_id="conv-30",
        history=history,
        history_source_message_ids=source_ids,
        provider="openai",
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=900,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=lambda candidate: (
            candidate_histories.append(candidate) or 400
        ),
    )

    persist_call = persistence_state["persist_calls"][0]
    raw_rows: List[SimpleNamespace] = []
    parent_message_id: Optional[int] = None
    for index, (message, source_id) in enumerate(zip(history, source_ids, strict=True)):
        raw_rows.append(
            SimpleNamespace(
                id=source_id,
                task_id=30,
                conversation_id="conv-30",
                parent_message_id=parent_message_id,
                message_type=message["role"],
                message=message["content"],
                created_at=datetime(2024, 1, 1, index, tzinfo=timezone.utc),
                tool_calls=[],
            )
        )
        parent_message_id = source_id
    summary_row = SimpleNamespace(
        id=999,
        task_id=30,
        conversation_id="conv-30",
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message=persist_call["summary_text"],
        citations={
            "context_compression": {
                "epoch_id": persist_call["compression_epoch_id"],
                "source_tokens": persist_call["source_tokens"],
                "through_message_id": persist_call["through_message_id"],
            }
        },
        created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        tool_calls=[],
    )

    reconstructed = []
    for _ in range(2):
        db = Mock()
        result = Mock()
        result.scalars.return_value.unique.return_value.all.return_value = [
            summary_row,
            *reversed(raw_rows),
        ]
        db.execute.return_value = result
        reconstructed.append(
            ConversationHistoryReader(db).build_aligned_openai_conversation_history(
                task_id=30,
                conversation_id="conv-30",
            )
        )
        db.execute.assert_called_once()

    assert candidate_histories == [
        [
            {"role": "system", "content": "restart-stable summary"},
            *history[6:],
        ]
    ]
    for aligned_history in reconstructed:
        assert list(aligned_history.messages) == candidate_histories[0]
        assert aligned_history.source_message_ids == (999, *source_ids[6:])
        assert "active user message" not in str(aligned_history.messages)


@pytest.mark.asyncio
async def test_idempotent_snapshot_reuse_recounts_live_candidate_from_durable_summary() -> None:
    """A retry projects the existing durable snapshot exactly as a fresh reader does."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="different retry summary",
            original_tokens=900,
            final_tokens=120,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {
        "persisted_snapshot": SimpleNamespace(
            message="  authoritative durable summary  ",
            token_count=77,
        )
    }
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    history, source_ids = _complete_turn_history(8)
    candidate_histories: List[List[Dict[str, Any]]] = []

    _, _, compression_metadata, _ = await service.prepare_preturn_history(
        task_id=302,
        conversation_id="conv-302",
        history=history,
        history_source_message_ids=source_ids,
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=900,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=lambda candidate: (
            candidate_histories.append(candidate) or 400
        ),
    )

    persist_call = persistence_state["persist_calls"][0]
    raw_rows: List[SimpleNamespace] = []
    parent_message_id: Optional[int] = None
    for index, (message, source_id) in enumerate(
        zip(history, source_ids, strict=True)
    ):
        raw_rows.append(
            SimpleNamespace(
                id=source_id,
                task_id=302,
                conversation_id="conv-302",
                parent_message_id=parent_message_id,
                message_type=message["role"],
                message=message["content"],
                created_at=datetime(2024, 1, 1, index, tzinfo=timezone.utc),
                tool_calls=[],
            )
        )
        parent_message_id = source_id
    summary_row = SimpleNamespace(
        id=999,
        task_id=302,
        conversation_id="conv-302",
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="  authoritative durable summary  ",
        citations={
            "context_compression": {
                "epoch_id": persist_call["compression_epoch_id"],
                "source_tokens": persist_call["source_tokens"],
                "through_message_id": persist_call["through_message_id"],
            }
        },
        created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        tool_calls=[],
    )
    db = Mock()
    result = Mock()
    result.scalars.return_value.unique.return_value.all.return_value = [
        summary_row,
        *reversed(raw_rows),
    ]
    db.execute.return_value = result

    reconstructed = ConversationHistoryReader(
        db
    ).build_aligned_openai_conversation_history(
        task_id=302,
        conversation_id="conv-302",
    )

    assert candidate_histories[0][0]["content"] == "different retry summary"
    assert candidate_histories[-1][0]["content"] == "authoritative durable summary"
    assert list(reconstructed.messages) == candidate_histories[-1]
    assert compression_metadata["final_tokens"] == 77


@pytest.mark.asyncio
async def test_equal_token_histories_persist_distinct_cutoffs_and_reconstruct_latest() -> None:
    """Exact source identity prevents equal-token histories from sharing an epoch."""
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text=f"summary through {request.conversation_history[-1]['content']}",
            original_tokens=900,
            final_tokens=120,
        ),
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )

    async def _compact(turn_count: int) -> tuple[List[Dict[str, Any]], List[int]]:
        history: List[Dict[str, Any]] = []
        source_ids: List[int] = []
        for turn_number in range(1, turn_count + 1):
            history.extend(
                [
                    {"role": "user", "content": f"question {turn_number}"},
                    {"role": "assistant", "content": f"answer {turn_number}"},
                ]
            )
            source_ids.extend([turn_number * 10, turn_number * 10 + 1])
        await service.prepare_preturn_history(
            task_id=301,
            conversation_id="conv-301",
            history=history,
            history_source_message_ids=source_ids,
            provider="openai",
            model="gpt-5.2",
            context_limit_tokens=1_000,
            request_prompt_tokens=900,
            reserved_output_tokens=100,
            candidate_classifier_prompt_counter=lambda _candidate: 400,
        )
        return history, source_ids

    await _compact(8)
    latest_history, latest_source_ids = await _compact(9)

    first_persist, latest_persist = persistence_state["persist_calls"]
    assert first_persist["source_tokens"] == latest_persist["source_tokens"] == 900
    assert first_persist["through_message_id"] == 31
    assert latest_persist["through_message_id"] == 41
    assert first_persist["compression_epoch_id"] != latest_persist["compression_epoch_id"]

    raw_rows: List[SimpleNamespace] = []
    parent_message_id: Optional[int] = None
    for index, (message, source_id) in enumerate(
        zip(latest_history, latest_source_ids, strict=True)
    ):
        raw_rows.append(
            SimpleNamespace(
                id=source_id,
                task_id=301,
                conversation_id="conv-301",
                parent_message_id=parent_message_id,
                message_type=message["role"],
                message=message["content"],
                created_at=datetime(2024, 1, 1, index, tzinfo=timezone.utc),
                tool_calls=[],
            )
        )
        parent_message_id = source_id
    summary_row = SimpleNamespace(
        id=999,
        task_id=301,
        conversation_id="conv-301",
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message=latest_persist["summary_text"],
        citations={
            "context_compression": {
                "epoch_id": latest_persist["compression_epoch_id"],
                "source_tokens": latest_persist["source_tokens"],
                "through_message_id": latest_persist["through_message_id"],
            }
        },
        created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        tool_calls=[],
    )
    db = Mock()
    result = Mock()
    result.scalars.return_value.unique.return_value.all.return_value = [
        summary_row,
        *reversed(raw_rows),
    ]
    db.execute.return_value = result

    reconstructed = ConversationHistoryReader(
        db
    ).build_aligned_openai_conversation_history(
        task_id=301,
        conversation_id="conv-301",
    )

    assert reconstructed.messages[0] == {
        "role": "system",
        "content": latest_persist["summary_text"],
    }
    assert reconstructed.source_message_ids == (999, *latest_source_ids[8:])


@pytest.mark.asyncio
async def test_prepare_preturn_history_falls_back_by_one_complete_turn() -> None:
    """A five-turn miss retries with four turns and persists one snapshot."""
    compression_call_count = 0

    def _build_outcome(request: ContextCompressionRequest) -> ContextCompressionOutcome:
        nonlocal compression_call_count
        compression_call_count += 1
        return _outcome_from_request(
            request,
            final_text=f"candidate summary {compression_call_count}",
            original_tokens=900,
            final_tokens=120,
        )

    compression_service = _FakeCompressionService(
        outcome_builder=_build_outcome,
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    history: List[Dict[str, Any]] = []
    source_ids: List[int] = []
    for turn_number in range(1, 9):
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"answer {turn_number}"},
            ]
        )
        source_ids.extend([turn_number * 10, turn_number * 10 + 1])

    candidate_histories: List[List[Dict[str, Any]]] = []
    candidate_counts = iter((901, 800))

    def _count_candidate(candidate_history: List[Dict[str, Any]]) -> int:
        candidate_histories.append(candidate_history)
        return next(candidate_counts)

    _, _, compression_metadata, _ = await service.prepare_preturn_history(
        task_id=28,
        conversation_id="conv-28",
        history=history,
        history_source_message_ids=source_ids,
        provider="openai",
        model="gpt-5.2",
        context_limit_tokens=1_000,
        request_prompt_tokens=900,
        reserved_output_tokens=100,
        candidate_classifier_prompt_counter=_count_candidate,
    )

    assert [
        call["request"].conversation_history for call in compression_service.calls
    ] == [
        history[:6],
        [
            {"role": "system", "content": "candidate summary 1"},
            *history[6:8],
        ],
    ]
    assert candidate_histories == [
        [
            {"role": "system", "content": "candidate summary 1"},
            *history[6:],
        ],
        [
            {"role": "system", "content": "candidate summary 2"},
            *history[8:],
        ],
    ]
    assert compression_metadata["candidate_request_fits"] is True
    assert compression_metadata["candidate_retained_turns"] == 4
    assert compression_metadata["snapshot_persisted"] is True
    assert len(persistence_state["persist_calls"]) == 1
    assert persistence_state["persist_calls"][0]["through_message_id"] == 41
    assert persistence_state["persist_calls"][0]["summary_text"] == (
        "candidate summary 2"
    )


@pytest.mark.asyncio
async def test_prepare_preturn_history_raises_context_uncompactable_at_minimum() -> None:
    """Three-turn hard-fit failure stops before persistence or classifier send."""
    compression_call_count = 0

    def _build_outcome(request: ContextCompressionRequest) -> ContextCompressionOutcome:
        nonlocal compression_call_count
        compression_call_count += 1
        return _outcome_from_request(
            request,
            final_text=f"candidate summary {compression_call_count}",
            original_tokens=900,
            final_tokens=120,
        )

    compression_service = _FakeCompressionService(
        outcome_builder=_build_outcome,
        state={},
    )
    persistence_state: Dict[str, Any] = {}
    service = TurnCompressionService(
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _compression_snapshot_repository_factory(persistence_state)
        ),
        session_factory=_session_factory(persistence_state),
    )
    history: List[Dict[str, Any]] = []
    source_ids: List[int] = []
    for turn_number in range(1, 9):
        history.extend(
            [
                {"role": "user", "content": f"question {turn_number}"},
                {"role": "assistant", "content": f"answer {turn_number}"},
            ]
        )
        source_ids.extend([turn_number * 10, turn_number * 10 + 1])
    candidate_histories: List[List[Dict[str, Any]]] = []

    with pytest.raises(CompressionRequiredError) as exc_info:
        await service.prepare_preturn_history(
            task_id=29,
            conversation_id="conv-29",
            history=history,
            history_source_message_ids=source_ids,
            provider="openai",
            model="gpt-5.2",
            context_limit_tokens=1_000,
            request_prompt_tokens=901,
            reserved_output_tokens=100,
            candidate_classifier_prompt_counter=lambda candidate: (
                candidate_histories.append(candidate) or 901
            ),
        )

    assert exc_info.value.reason == "context_uncompactable"
    assert [
        len(candidate) for candidate in candidate_histories
    ] == [11, 9, 7]
    assert len(compression_service.calls) == 3
    assert compression_service.calls[1]["request"].conversation_history == [
        {"role": "system", "content": "candidate summary 1"},
        *history[6:8],
    ]
    assert compression_service.calls[2]["request"].conversation_history == [
        {"role": "system", "content": "candidate summary 2"},
        *history[8:10],
    ]
    assert persistence_state.get("persist_calls", []) == []
    assert persistence_state.get("sessions_closed", 0) == 0
