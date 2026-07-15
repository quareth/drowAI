"""Phase 5 regression tests — compression is not prompt authority.

Locks in the contract that after the Phase 5 cutover:

- Pre-turn compression (``TurnCompressionService.prepare_preturn_history``)
  still fires for observability and persists its validated snapshot before
  classifier invocation, but it no longer replaces the canonical transcript
  passed to the facade. The hot-path
  ``ConversationContextBundle`` is therefore built from the pristine
  ``ConversationHistoryReader`` transcript, not from a synthetic
  compression summary.
- When ``metadata`` carries compression-related payloads (e.g.
  ``last_tool_result_compact`` or ``context_compression``) alongside
  a properly-built bundle, none of those compression artifacts leak
  into the hot-path prompt surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# Force full graph package init to break the pre-existing circular import
# between planner_service and agent.graph.nodes / agent.graph.builders.
import agent.graph.builders  # noqa: F401  # side-effect: break import cycle
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.subgraphs.tool_execution_runtime import planner_service
from agent.tool_runtime import ToolExecutionRequest
from backend.services.langgraph_chat.compression.context_models import (
    CompressionPassResult,
    ContextCompressionOutcome,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.window_manager import (
    ContextWindowDecision,
    ContextWindowSnapshot,
)
from backend.services.langgraph_chat.compression.turn_service import (
    TurnCompressionService,
)


# ---------------------------------------------------------------------------
# Helpers (modelled on test_turn_compression_service.py fixtures)
# ---------------------------------------------------------------------------


def _make_history(turn_count: int) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for i in range(turn_count):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
    return history


def _install_bundle(
    metadata: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-comp",
        turn_id="turn-comp",
        turn_sequence=0,
        messages=list(messages),
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    return bundle


def _make_tool_execution_request(
    history: Optional[List[Dict[str, Any]]] = None,
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="follow up",
        history=list(history or []),
    )


def _decision_ceiling_reached(
    *,
    used_tokens: int = 128_000,
    max_tokens: int = 128_000,
    conversation_id: str = "conv-comp",
) -> ContextWindowDecision:
    return ContextWindowDecision(
        snapshot=ContextWindowSnapshot(
            task_id=1,
            conversation_id=conversation_id,
            max_tokens=max_tokens,
            used_tokens=used_tokens,
            remaining_tokens=max(0, max_tokens - used_tokens),
            ratio=used_tokens / max_tokens if max_tokens else 0.0,
            ceiling_reached=True,
        ),
        ceiling_reached=True,
        recommended_next_action="compress",
        compression_candidate=True,
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


def _outcome_from_request(
    request: ContextCompressionRequest,
    *,
    final_text: str,
    original_tokens: int,
    final_tokens: int,
) -> ContextCompressionOutcome:
    return ContextCompressionOutcome(
        request=request,
        original_tokens=original_tokens,
        final_tokens=final_tokens,
        final_text=final_text,
        pass_results=(_pass_result(output_text=final_text, output_tokens=final_tokens),),
        pass_count=1,
        degraded=False,
        fallback_reason=None,
    )


class _FakeSession:
    def close(self) -> None:
        return


class _FakeContextWindowManager:
    def __init__(self, decision: ContextWindowDecision, *, max_tokens: int) -> None:
        self._decision = decision
        self.max_tokens = max_tokens

    def evaluate_classifier_prompt(
        self,
        *,
        reserved_output_tokens: int,
        **_: Any,
    ) -> tuple[ContextWindowDecision, SimpleNamespace]:
        return self._decision, SimpleNamespace(
            usable_prompt_tokens=self.max_tokens - reserved_output_tokens,
            trigger_tokens=int((self.max_tokens - reserved_output_tokens) * 0.8),
            reserved_output_tokens=reserved_output_tokens,
            override_active=False,
        )

    def estimate_tokens_from_history(self, **_: Any) -> int:
        return self._decision.snapshot.used_tokens

    def estimate_tokens_from_openai_history(self, **kwargs: Any) -> int:
        return self.estimate_tokens_from_history(provider="openai", **kwargs)


def _context_window_manager_factory(
    decision: ContextWindowDecision,
) -> Callable[[Optional[int]], _FakeContextWindowManager]:
    def factory(max_tokens: Optional[int]) -> _FakeContextWindowManager:
        resolved = max_tokens if max_tokens is not None else decision.snapshot.max_tokens
        return _FakeContextWindowManager(decision, max_tokens=resolved)

    return factory


class _FakeCompressionService:
    def __init__(
        self,
        *,
        enabled: bool = True,
        outcome_builder: Optional[
            Callable[[ContextCompressionRequest], ContextCompressionOutcome]
        ] = None,
    ) -> None:
        self.enabled = enabled
        self._outcome_builder = outcome_builder
        self.calls: List[Dict[str, Any]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    async def compress(
        self,
        request: ContextCompressionRequest,
    ) -> ContextCompressionOutcome:
        self.calls.append({"request": request})
        assert self._outcome_builder is not None
        return self._outcome_builder(request)


class _FakeCompressionSnapshotRepository:
    def __init__(self, _db: Any) -> None:
        return

    def persist_snapshot(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            message=kwargs["summary_text"],
            token_count=kwargs["token_count"],
        )


# ---------------------------------------------------------------------------
# prepare_preturn_history preserves canonical transcript continuity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preturn_compression_does_not_replace_facade_history() -> None:
    """Compression fires for observability, but canonical transcript passes through.

    Before the Phase 5 cutover, ``prepare_preturn_history`` replaced
    the facade-bound history with ``[{role: system, content:
    compressed_text}]``. That replacement then fed the bundle builder,
    breaking the invariant that the bundle is built from the
    canonical ``ConversationHistoryReader`` transcript. After cutover the
    canonical transcript must pass through unchanged.
    """
    decision = _decision_ceiling_reached()
    compression_service = _FakeCompressionService(
        outcome_builder=lambda request: _outcome_from_request(
            request,
            final_text="COMPRESSED_SUMMARY_MUST_NOT_REPLACE_TRANSCRIPT",
            original_tokens=128_000,
            final_tokens=600,
        ),
    )

    service = TurnCompressionService(
        context_window_manager_factory=_context_window_manager_factory(decision),
        context_compression_service_factory=lambda: compression_service,
        compression_snapshot_repository_factory=(
            _FakeCompressionSnapshotRepository
        ),
        session_factory=_FakeSession,
    )

    canonical_history = _make_history(turn_count=6)
    source_ids = list(range(1, len(canonical_history) + 1))

    history_for_facade, context_window_metadata, compression_metadata, emitted = (
        await service.prepare_preturn_history(
            task_id=1,
            conversation_id="conv-comp",
            history=list(canonical_history),
            history_source_message_ids=source_ids,
            model="gpt-5.2",
            context_limit_tokens=128_000,
            request_prompt_tokens=128_000,
            reserved_output_tokens=1,
            candidate_classifier_prompt_counter=lambda _history: 1_000,
        )
    )

    # Canonical transcript passes through unchanged — it will feed the
    # bundle builder in the facade.
    assert history_for_facade == canonical_history
    # Compression still ran before classification.
    assert compression_metadata["applied"] is True
    assert compression_metadata["original_tokens"] == 128_000
    assert compression_metadata["final_tokens"] == 600
    assert emitted is True
    assert context_window_metadata is not None
    assert context_window_metadata["compression"]["applied"] is True
    assert compression_service.calls, "compression service must still be invoked"


# ---------------------------------------------------------------------------
# Bundle is built from canonical transcript even when compression metadata exists
# ---------------------------------------------------------------------------


def test_planner_context_does_not_emit_history_key_with_compression_metadata() -> None:
    """Runner Control follow-up Fix 1 regression: planner context emits no ``history`` key.

    Before Fix 1, the planner service projected a ``history`` list
    from the bundle and downstream callsites (artifact-tool policy,
    target resolver) consumed it. After Fix 1 the list is gone — the
    classifier-derived ``intent_brief`` is the current-turn
    authority. Even when heavy compression metadata is scattered on
    the runtime ``metadata``, the planner context dict must not carry
    a ``history`` key.
    """
    history = _make_history(turn_count=3)
    metadata: Dict[str, Any] = {
        "intent_capability": "simple_tool_execution",
        "tool_intent": {},
        "last_tool_result_compact": {
            "summary": "COMPRESSION_SUMMARY_MUST_NOT_APPEAR",
        },
        "context_compression": {"applied": True},
        "conversation_history": [
            {"role": "system", "content": "COMPRESSED_CONV_HISTORY_MUST_NOT_APPEAR"},
        ],
    }
    _install_bundle(metadata, history)

    interactive = MagicMock()
    interactive.facts.metadata = metadata
    interactive.facts.message = "follow up"
    interactive.facts.plan = []
    interactive.facts.current_goal = ""
    interactive.facts.next_tool_hint = None
    interactive.facts.selected_tool = None
    interactive.facts.tool_parameters = {}
    interactive.facts.todo_list = []
    interactive.facts.intent_hints = {"targets": []}
    interactive.trace.reasoning = []
    interactive.trace.observations = []

    request = _make_tool_execution_request(history=history)

    def _fake_catalog(*_args, **_kwargs):
        return ["tool.a"]

    planner_context = planner_service.build_planner_context(
        interactive,
        request,
        get_category_filtered_catalog=_fake_catalog,
        get_full_tool_catalog_for_planner=_fake_catalog,
        working_memory_summary_max_chars=900,
    )

    # Fix 1: no ``history`` list emitted.
    assert "history" not in planner_context

    # Transcript-shaped continuity is also not folded into the direct
    # executor's prompt path via conversation_history_text.
    assert "conversation_history_text" not in planner_context


__all__: List[str] = []
