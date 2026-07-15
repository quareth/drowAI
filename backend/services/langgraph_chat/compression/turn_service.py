"""Compression orchestration service for turn-level context management.

This module owns the pre-classifier compaction decision, ordered lifecycle
publication, candidate validation, and snapshot persistence coordination.

The pre-classifier path is the only compression authority for a user turn. It
validates and persists one durable candidate before classifier invocation;
successful-turn completion does not recompress or write another summary.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from agent.graph.context.transcript import split_transcript_into_turn_groups
from backend.database import SessionLocal
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.context_service import ContextCompressionService
from backend.services.langgraph_chat.compression.window_manager import (
    ContextWindowManager,
    MINIMUM_COMPACTION_RETAINED_TURNS,
    TARGET_COMPACTION_RETAINED_TURNS,
)
from backend.services.langgraph_chat.compression.window_models import (
    measured_snapshot_revision,
)
from backend.services.langgraph_chat.compression.snapshot_repository import (
    CompressionSnapshotRepository,
)
from backend.services.langgraph_chat.runtime.run_lifecycle import (
    RunLifecycleService,
    get_run_lifecycle_service,
)
from backend.services.langgraph_chat.streaming.status_events import (
    emit_context_window_event as _default_emit_context_window_event,
    publish_context_window_lifecycle_event as _default_publish_context_window_lifecycle_event,
)

logger = logging.getLogger(__name__)

_COMPRESSION_REQUIRED_FAILED = "compression_required_failed"
_COMPRESSION_PERSIST_FAILED = "compression_persist_failed"
_CONTEXT_UNCOMPACTABLE = "context_uncompactable"

ContextWindowManagerFactory = Callable[[Optional[int]], ContextWindowManager]
ContextCompressionServiceFactory = Callable[[], ContextCompressionService]
CompressionSnapshotRepositoryFactory = Callable[[Any], CompressionSnapshotRepository]
SessionFactory = Callable[[], Any]
EmitContextWindowEvent = Callable[..., None]
PublishContextWindowLifecycleEvent = Callable[..., Awaitable[bool]]
CompactionLifecycleStart = Callable[[str], Awaitable[None]]
CandidateClassifierPromptCounter = Callable[[List[Dict[str, Any]]], int]
ContextWindowSnapshotHandoff = Callable[[Dict[str, Any]], None]


def _build_compression_epoch_id(
    *,
    task_id: int,
    conversation_id: str,
    source_tokens: int,
    source_message_ids: Optional[List[int]],
) -> str:
    """Build a deterministic operation identity from the exact source sidecar."""
    normalized_ids = ",".join(
        str(message_id)
        for message_id in source_message_ids or []
        if not isinstance(message_id, bool)
        and isinstance(message_id, int)
        and message_id > 0
    )
    source_digest = hashlib.sha256(normalized_ids.encode("utf-8")).hexdigest()[:16]
    return f"{task_id}:{conversation_id}:{source_tokens}:{source_digest}"


@dataclass(frozen=True, slots=True)
class AlignedTranscriptGroup:
    """One transcript segment with positionally aligned backend source IDs."""

    messages: tuple[Dict[str, Any], ...]
    source_message_ids: tuple[Optional[int], ...]


@dataclass(frozen=True, slots=True)
class CompactionTurnCandidate:
    """One whole-turn retained-tail option for later hard-fit validation."""

    leading_group: AlignedTranscriptGroup
    expired_turn_groups: tuple[AlignedTranscriptGroup, ...]
    retained_turn_groups: tuple[AlignedTranscriptGroup, ...]

    @property
    def retained_turn_count(self) -> int:
        """Return the number of complete verbatim turns in this candidate."""
        return len(self.retained_turn_groups)

    @property
    def summary_input_messages(self) -> tuple[Dict[str, Any], ...]:
        """Return prior leading context plus only newly expired turn messages."""
        messages = list(self.leading_group.messages)
        for turn_group in self.expired_turn_groups:
            messages.extend(turn_group.messages)
        return tuple(messages)

    def classifier_history(self, summary: str) -> List[Dict[str, Any]]:
        """Project an isolated summary plus retained whole-turn transcript."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": summary},
        ]
        for turn_group in self.retained_turn_groups:
            messages.extend(turn_group.messages)
        return messages

    @property
    def summarized_through_message_id(self) -> Optional[int]:
        """Return the exact final raw source ID moved into the summary."""
        if not self.expired_turn_groups:
            return None
        final_group_ids = self.expired_turn_groups[-1].source_message_ids
        if not final_group_ids:
            return None
        through_message_id = final_group_ids[-1]
        if (
            isinstance(through_message_id, bool)
            or not isinstance(through_message_id, int)
            or through_message_id <= 0
        ):
            return None
        return through_message_id


def split_aligned_transcript_into_turn_groups(
    history: List[Dict[str, Any]],
    source_message_ids: Optional[List[int]],
) -> tuple[AlignedTranscriptGroup, tuple[AlignedTranscriptGroup, ...]]:
    """Reuse canonical turn grouping while retaining a backend-only ID sidecar."""
    if source_message_ids is not None and len(source_message_ids) != len(history):
        raise ValueError("history and source_message_ids must have equal lengths")
    aligned_ids: List[Optional[int]] = (
        list(source_message_ids)
        if source_message_ids is not None
        else [None] * len(history)
    )
    leading_messages, turn_messages = split_transcript_into_turn_groups(history)
    cursor = 0

    def _aligned_group(messages: List[Dict[str, Any]]) -> AlignedTranscriptGroup:
        nonlocal cursor
        end = cursor + len(messages)
        group = AlignedTranscriptGroup(
            messages=tuple(messages),
            source_message_ids=tuple(aligned_ids[cursor:end]),
        )
        cursor = end
        return group

    leading_group = _aligned_group(leading_messages)
    turn_groups = tuple(_aligned_group(messages) for messages in turn_messages)
    if cursor != len(history):
        raise RuntimeError("turn grouping did not consume the aligned history")
    return leading_group, turn_groups


def build_compaction_turn_candidates(
    history: List[Dict[str, Any]],
    source_message_ids: Optional[List[int]],
) -> tuple[CompactionTurnCandidate, ...]:
    """Build deterministic five-to-three whole-turn compaction candidates."""
    leading_group, turn_groups = split_aligned_transcript_into_turn_groups(
        history,
        source_message_ids,
    )
    if len(turn_groups) < MINIMUM_COMPACTION_RETAINED_TURNS:
        return ()

    target_count = min(
        len(turn_groups) - 1,
        TARGET_COMPACTION_RETAINED_TURNS,
    )
    if target_count < MINIMUM_COMPACTION_RETAINED_TURNS:
        return ()
    return tuple(
        CompactionTurnCandidate(
            leading_group=leading_group,
            expired_turn_groups=turn_groups[:-retained_count],
            retained_turn_groups=turn_groups[-retained_count:],
        )
        for retained_count in range(
            target_count,
            MINIMUM_COMPACTION_RETAINED_TURNS - 1,
            -1,
        )
    )


class TurnCompressionService:
    """Orchestrate turn-compression policy decisions and persistence coordination."""

    def __init__(
        self,
        *,
        context_window_manager_factory: Optional[ContextWindowManagerFactory] = None,
        context_compression_service_factory: Optional[ContextCompressionServiceFactory] = None,
        compression_snapshot_repository_factory: Optional[CompressionSnapshotRepositoryFactory] = None,
        session_factory: Optional[SessionFactory] = None,
        publish_context_window_lifecycle_event: Optional[
            PublishContextWindowLifecycleEvent
        ] = None,
        run_lifecycle_service: Optional[RunLifecycleService] = None,
    ) -> None:
        self._context_window_manager_factory = context_window_manager_factory or (
            lambda max_tokens: ContextWindowManager(max_tokens=max_tokens)
            if max_tokens is not None
            else ContextWindowManager()
        )
        self._context_compression_service_factory = context_compression_service_factory or (
            lambda: ContextCompressionService()
        )
        self._compression_snapshot_repository_factory = (
            compression_snapshot_repository_factory or CompressionSnapshotRepository
        )
        self._session_factory = session_factory or SessionLocal
        self._publish_context_window_lifecycle_event = (
            publish_context_window_lifecycle_event
            or _default_publish_context_window_lifecycle_event
        )
        self._run_lifecycle_service = (
            run_lifecycle_service or get_run_lifecycle_service()
        )

    @staticmethod
    def extract_context_window_metadata(
        *,
        metadata: Any,
        fallback_conversation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Normalize context-window metadata from pre-computed or checkpoint payloads."""
        if not isinstance(metadata, dict):
            return None

        candidate = metadata.get("context_window")
        source = candidate if isinstance(candidate, dict) else metadata

        ceiling_reached_raw = source.get("ceiling_reached")
        if not isinstance(ceiling_reached_raw, bool):
            return None

        max_tokens = source.get("max_tokens")
        used_tokens = source.get("used_tokens")
        remaining_tokens = source.get("remaining_tokens")
        ratio = source.get("ratio")
        if not isinstance(max_tokens, int):
            return None
        if not isinstance(used_tokens, int):
            return None
        if not isinstance(remaining_tokens, int):
            return None
        if not isinstance(ratio, (int, float)):
            return None

        conversation_id = source.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            conversation_id = fallback_conversation_id

        recommended_next_action = source.get("recommended_next_action")
        if not isinstance(recommended_next_action, str):
            recommended_next_action = "compress" if ceiling_reached_raw else "none"
        compression_candidate = source.get("compression_candidate")
        if not isinstance(compression_candidate, bool):
            compression_candidate = ceiling_reached_raw
        compression = source.get("compression")

        normalized = {
            "ceiling_reached": ceiling_reached_raw,
            "recommended_next_action": recommended_next_action,
            "compression_candidate": compression_candidate,
            "max_tokens": max_tokens,
            "used_tokens": used_tokens,
            "remaining_tokens": remaining_tokens,
            "ratio": float(ratio),
            "conversation_id": conversation_id,
        }
        for field_name in ("turn_sequence", "revision"):
            field_value = source.get(field_name)
            if isinstance(field_value, int) and not isinstance(field_value, bool):
                normalized[field_name] = field_value
        snapshot_kind = source.get("snapshot_kind")
        if snapshot_kind in {"measured", "bootstrap_estimate"}:
            normalized["snapshot_kind"] = snapshot_kind
        for field_name in (
            "usable_prompt_tokens",
            "trigger_tokens",
            "reserved_output_tokens",
        ):
            field_value = source.get(field_name)
            if isinstance(field_value, int) and not isinstance(field_value, bool):
                normalized[field_name] = field_value
        trigger_override_active = source.get("trigger_override_active")
        if isinstance(trigger_override_active, bool):
            normalized["trigger_override_active"] = trigger_override_active
        if isinstance(compression, dict):
            normalized["compression"] = dict(compression)
        return normalized

    @staticmethod
    def context_window_handoff_fields(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return normalized handoff fields for workflow metadata."""
        if metadata is None:
            return {}
        return {
            "context_window": dict(metadata),
            "ceiling_reached": bool(metadata.get("ceiling_reached", False)),
            "recommended_next_action": metadata.get("recommended_next_action", "none"),
            "compression_candidate": bool(metadata.get("compression_candidate", False)),
        }

    @staticmethod
    def compression_handoff_fields(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return normalized compression metadata for workflow audit records."""
        if not isinstance(metadata, dict):
            return {}
        source = metadata
        nested = metadata.get("compression")
        if isinstance(nested, dict):
            source = nested
        applied = bool(source.get("applied"))
        compression: Dict[str, Any] = {"applied": applied}
        for field_name in (
            "pass_count",
            "degraded",
            "original_tokens",
            "final_tokens",
            "fallback_reason",
            "epoch_id",
            "source_tokens",
            "reason",
            "warning",
            "pending_next_turn",
        ):
            if field_name in source:
                compression[field_name] = source[field_name]
        if not applied and not bool(compression.get("pending_next_turn")):
            return {}
        handoff = {"compression": compression, "compression_applied": applied}
        if bool(compression.get("pending_next_turn")):
            handoff["compression_pending_next_turn"] = True
        return handoff

    def emit_context_window_event(
        self,
        *,
        task_id: int,
        metadata: Optional[Dict[str, Any]],
        emit_context_window_event: Optional[EmitContextWindowEvent] = None,
    ) -> None:
        """Emit additive context-window status if normalized metadata is present."""
        if not metadata:
            return
        ceiling_reached = bool(metadata.get("ceiling_reached", False))
        recommended_next_action = metadata.get("recommended_next_action")
        if not isinstance(recommended_next_action, str):
            recommended_next_action = "compress" if ceiling_reached else "none"
        compression_candidate = metadata.get("compression_candidate")
        if not isinstance(compression_candidate, bool):
            compression_candidate = ceiling_reached
        compression = metadata.get("compression")
        compression_pass_count: Optional[int] = None
        compression_tokens_before: Optional[int] = None
        compression_tokens_after: Optional[int] = None
        compression_degraded: Optional[bool] = None
        if isinstance(compression, dict):
            pass_count = compression.get("pass_count")
            original_tokens = compression.get("original_tokens")
            final_tokens = compression.get("final_tokens")
            degraded = compression.get("degraded")
            if isinstance(pass_count, int):
                compression_pass_count = pass_count
            if isinstance(original_tokens, int):
                compression_tokens_before = original_tokens
            if isinstance(final_tokens, int):
                compression_tokens_after = final_tokens
            if isinstance(degraded, bool):
                compression_degraded = degraded

        context_event_emitter = emit_context_window_event or _default_emit_context_window_event
        if context_event_emitter is None:
            return
        context_event_emitter(
            task_id=task_id,
            conversation_id=str(metadata.get("conversation_id") or ""),
            max_tokens=int(metadata.get("max_tokens", 0)),
            used_tokens=int(metadata.get("used_tokens", 0)),
            remaining_tokens=int(metadata.get("remaining_tokens", 0)),
            ratio=float(metadata.get("ratio", 0.0)),
            ceiling_reached=ceiling_reached,
            recommended_next_action=recommended_next_action,
            compression_candidate=compression_candidate,
            compression_pass_count=compression_pass_count,
            compression_tokens_before=compression_tokens_before,
            compression_tokens_after=compression_tokens_after,
            compression_degraded=compression_degraded,
            turn_sequence=metadata.get("turn_sequence"),
            revision=metadata.get("revision"),
            snapshot_kind=metadata.get("snapshot_kind"),
        )

    def extract_and_emit_context_window_metadata(
        self,
        *,
        task_id: int,
        metadata: Any,
        fallback_conversation_id: str,
        emit_context_window_event: Optional[EmitContextWindowEvent] = None,
    ) -> Optional[Dict[str, Any]]:
        """Normalize context metadata, emit event, and inject handoff metadata."""
        context_window_metadata = self.extract_context_window_metadata(
            metadata=metadata,
            fallback_conversation_id=fallback_conversation_id,
        )
        if context_window_metadata is not None and isinstance(metadata, dict):
            self.emit_context_window_event(
                task_id=task_id,
                metadata=context_window_metadata,
                emit_context_window_event=emit_context_window_event,
            )
            metadata.update(self.context_window_handoff_fields(context_window_metadata))
        return context_window_metadata

    async def prepare_preturn_history(
        self,
        *,
        task_id: int,
        conversation_id: str,
        history: List[Dict[str, Any]],
        history_source_message_ids: List[int],
        context_limit_tokens: int,
        request_prompt_tokens: int,
        reserved_output_tokens: int,
        candidate_classifier_prompt_counter: CandidateClassifierPromptCounter,
        model: str,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        provider: str = "openai",
        llm_runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        runtime_user_id: Optional[int] = None,
        on_context_window_snapshot: Optional[ContextWindowSnapshotHandoff] = None,
    ) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any], bool]:
        """Run pre-turn compaction with one ordered lifecycle when provider work starts."""
        normalized_turn_id = turn_id.strip() if isinstance(turn_id, str) else ""
        lifecycle_epoch_id: Optional[str] = None
        owner_task = asyncio.current_task()
        if normalized_turn_id and owner_task is not None:
            self._run_lifecycle_service.register_cancellable_task(
                task_id=task_id,
                turn_id=normalized_turn_id,
                task=owner_task,
            )

        async def _start_compaction_lifecycle(epoch_id: str) -> None:
            nonlocal lifecycle_epoch_id
            if not normalized_turn_id:
                return
            lifecycle_epoch_id = epoch_id
            await self._publish_context_window_lifecycle_event(
                task_id=task_id,
                conversation_id=conversation_id,
                state="compacting",
                turn_id=normalized_turn_id,
                epoch_id=epoch_id,
            )

        async def _publish_terminal_lifecycle(state: str) -> None:
            if lifecycle_epoch_id is None:
                return
            await self._publish_context_window_lifecycle_event(
                task_id=task_id,
                conversation_id=conversation_id,
                state=state,
                turn_id=normalized_turn_id,
                epoch_id=lifecycle_epoch_id,
            )

        try:
            result = await self._prepare_preturn_history_impl(
                task_id=task_id,
                conversation_id=conversation_id,
                history=history,
                history_source_message_ids=history_source_message_ids,
                context_limit_tokens=context_limit_tokens,
                request_prompt_tokens=request_prompt_tokens,
                reserved_output_tokens=reserved_output_tokens,
                candidate_classifier_prompt_counter=(
                    candidate_classifier_prompt_counter
                ),
                model=model,
                turn_sequence=turn_sequence,
                provider=provider,
                llm_runtime_selection=llm_runtime_selection,
                runtime_services=runtime_services,
                runtime_user_id=runtime_user_id,
                on_compaction_start=_start_compaction_lifecycle,
                on_context_window_snapshot=on_context_window_snapshot,
            )
        except asyncio.CancelledError:
            await _publish_terminal_lifecycle("cancelled")
            raise
        except Exception:
            await _publish_terminal_lifecycle("failed")
            raise
        else:
            terminal_state = (
                "failed" if result[2].get("warning") is True else "completed"
            )
            await _publish_terminal_lifecycle(terminal_state)
            return result
        finally:
            if normalized_turn_id and owner_task is not None:
                self._run_lifecycle_service.unregister_cancellable_task(
                    task_id=task_id,
                    turn_id=normalized_turn_id,
                    task=owner_task,
                )

    async def _prepare_preturn_history_impl(
        self,
        *,
        task_id: int,
        conversation_id: str,
        history: List[Dict[str, Any]],
        history_source_message_ids: List[int],
        context_limit_tokens: int,
        request_prompt_tokens: int,
        reserved_output_tokens: int,
        candidate_classifier_prompt_counter: CandidateClassifierPromptCounter,
        model: str,
        turn_sequence: Optional[int] = None,
        provider: str = "openai",
        llm_runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        runtime_user_id: Optional[int] = None,
        on_compaction_start: Optional[CompactionLifecycleStart] = None,
        on_context_window_snapshot: Optional[ContextWindowSnapshotHandoff] = None,
    ) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any], bool]:
        """Prepare facade history and signal immediately before compressor work."""
        history_for_facade: List[Dict[str, Any]] = list(history)
        compression_metadata: Dict[str, Any] = {
            "applied": False,
            "reason": "below_trigger",
        }
        try:
            if (
                isinstance(context_limit_tokens, bool)
                or not isinstance(context_limit_tokens, int)
                or context_limit_tokens <= 0
            ):
                raise ValueError("context_limit_tokens must be a positive integer")
            context_window_manager = self._context_window_manager_factory(
                context_limit_tokens
            )
            context_window_decision, prompt_budget = (
                context_window_manager.evaluate_classifier_prompt(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    prompt_tokens=request_prompt_tokens,
                    reserved_output_tokens=reserved_output_tokens,
                )
            )
        except Exception:
            logger.warning(
                "Failed to evaluate pre-turn context window (task=%s, conversation_id=%s)",
                task_id,
                conversation_id,
                exc_info=True,
            )
            raise

        snapshot = context_window_decision.snapshot
        canonical_turn_sequence = (
            turn_sequence
            if isinstance(turn_sequence, int)
            and not isinstance(turn_sequence, bool)
            and turn_sequence >= 0
            else None
        )
        original_request_fits = (
            request_prompt_tokens + reserved_output_tokens <= snapshot.max_tokens
        )
        context_window_metadata: Dict[str, Any] = {
            "ceiling_reached": context_window_decision.ceiling_reached,
            "recommended_next_action": context_window_decision.recommended_next_action,
            "compression_candidate": context_window_decision.compression_candidate,
            "max_tokens": snapshot.max_tokens,
            "used_tokens": snapshot.used_tokens,
            "remaining_tokens": snapshot.remaining_tokens,
            "ratio": snapshot.ratio,
            "conversation_id": snapshot.conversation_id,
            "turn_sequence": canonical_turn_sequence,
            "revision": (
                measured_snapshot_revision(canonical_turn_sequence)
                if canonical_turn_sequence is not None
                else -1
            ),
            "snapshot_kind": (
                "measured"
                if canonical_turn_sequence is not None
                else "bootstrap_estimate"
            ),
        }
        context_window_metadata.update(
            {
                "usable_prompt_tokens": prompt_budget.usable_prompt_tokens,
                "trigger_tokens": prompt_budget.trigger_tokens,
                "reserved_output_tokens": prompt_budget.reserved_output_tokens,
                "trigger_override_active": prompt_budget.override_active,
            }
        )
        if on_context_window_snapshot is not None:
            on_context_window_snapshot(context_window_metadata)

        if context_window_decision.ceiling_reached:
            from backend.services.metrics.utils import safe_inc

            safe_inc("compression_trigger_total")
            turn_candidates = build_compaction_turn_candidates(
                history_for_facade,
                history_source_message_ids,
            )
            compression_service = self._context_compression_service_factory()
            compression_epoch_id = _build_compression_epoch_id(
                task_id=task_id,
                conversation_id=conversation_id,
                source_tokens=snapshot.used_tokens,
                source_message_ids=history_source_message_ids,
            )
            if on_compaction_start is not None:
                await on_compaction_start(compression_epoch_id)

            def _fail_open(reason: str) -> bool:
                nonlocal compression_metadata
                compression_metadata = {
                    "applied": False,
                    "reason": reason,
                    "warning": original_request_fits,
                    "original_request_fits": original_request_fits,
                }
                context_window_metadata["compression"] = dict(
                    compression_metadata
                )
                self.emit_context_window_event(
                    task_id=task_id,
                    metadata=context_window_metadata,
                )
                return original_request_fits

            if not compression_service.is_enabled():
                if _fail_open("disabled_by_flag"):
                    return (
                        history_for_facade,
                        context_window_metadata,
                        compression_metadata,
                        True,
                    )
                raise CompressionRequiredError(
                    reason=_COMPRESSION_REQUIRED_FAILED,
                    detail="compression service disabled at context ceiling",
                )

            compress_kwargs: Dict[str, Any] = {}
            if (
                llm_runtime_selection is not None
                or runtime_services is not None
                or runtime_user_id is not None
            ):
                compress_kwargs.update(
                    {
                        "runtime_selection": llm_runtime_selection,
                        "runtime_services": runtime_services,
                        "runtime_user_id": runtime_user_id,
                        "purpose": "context_compression",
                    }
                )
            candidate_prompt_tokens: Optional[int] = None
            candidate_request_fits: Optional[bool] = None
            candidate_retained_turns: Optional[int] = None
            validated_candidate: Optional[CompactionTurnCandidate] = None
            compression_outcome: Any = None
            final_text = ""
            previous_summary: Optional[str] = None
            previous_expired_count = 0
            candidates_to_compress = tuple(turn_candidates)

            if not candidates_to_compress:
                if _fail_open(_CONTEXT_UNCOMPACTABLE):
                    return (
                        history_for_facade,
                        context_window_metadata,
                        compression_metadata,
                        True,
                    )
                raise CompressionRequiredError(
                    reason=_CONTEXT_UNCOMPACTABLE,
                    detail="fewer than three complete turns are available",
                )

            for candidate in candidates_to_compress:
                if previous_summary is None:
                    grouped_history = list(candidate.summary_input_messages)
                else:
                    grouped_history = [
                        {"role": "system", "content": previous_summary}
                    ]
                    for newly_expired_group in candidate.expired_turn_groups[
                        previous_expired_count:
                    ]:
                        grouped_history.extend(newly_expired_group.messages)

                try:
                    compression_outcome = await compression_service.compress(
                        ContextCompressionRequest(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            max_tokens=snapshot.max_tokens,
                            model=model,
                            provider=provider,
                            credential_ref=(
                                dict(llm_runtime_selection.get("credential_ref"))
                                if isinstance(llm_runtime_selection, Mapping)
                                and isinstance(
                                    llm_runtime_selection.get("credential_ref"),
                                    Mapping,
                                )
                                else None
                            ),
                            conversation_history=grouped_history,
                            projected_user_message=None,
                        ),
                        **compress_kwargs,
                    )
                except Exception as exc:
                    if _fail_open("compression_call_failed"):
                        return (
                            history_for_facade,
                            context_window_metadata,
                            compression_metadata,
                            True,
                        )
                    raise CompressionRequiredError(
                        reason=_COMPRESSION_REQUIRED_FAILED,
                        detail="compression call failed at context ceiling",
                    ) from exc

                final_text = (compression_outcome.final_text or "").strip()
                if not final_text:
                    if _fail_open("invalid_compressed_text"):
                        return (
                            history_for_facade,
                            context_window_metadata,
                            compression_metadata,
                            True,
                        )
                    raise CompressionRequiredError(
                        reason=_COMPRESSION_REQUIRED_FAILED,
                        detail="compression returned empty context at ceiling",
                    )

                candidate_history = candidate.classifier_history(final_text)
                candidate_prompt_tokens = candidate_classifier_prompt_counter(
                    candidate_history
                )
                if (
                    isinstance(candidate_prompt_tokens, bool)
                    or not isinstance(candidate_prompt_tokens, int)
                    or candidate_prompt_tokens < 0
                ):
                    raise ValueError(
                        "candidate classifier prompt counter must return a "
                        "non-negative integer"
                    )
                candidate_retained_turns = candidate.retained_turn_count
                candidate_request_fits = (
                    candidate_prompt_tokens + reserved_output_tokens
                    <= snapshot.max_tokens
                )
                if (
                    candidate_request_fits
                    and candidate.summarized_through_message_id is not None
                ):
                    validated_candidate = candidate
                    break
                previous_summary = final_text
                previous_expired_count = len(candidate.expired_turn_groups)

            if validated_candidate is None:
                if _fail_open(_CONTEXT_UNCOMPACTABLE):
                    return (
                        history_for_facade,
                        context_window_metadata,
                        compression_metadata,
                        True,
                    )
                compression_metadata = {
                    "applied": False,
                    "reason": _CONTEXT_UNCOMPACTABLE,
                    "candidate_retained_turns": candidate_retained_turns,
                    "candidate_prompt_tokens": candidate_prompt_tokens,
                }
                context_window_metadata["compression"] = dict(compression_metadata)
                self.emit_context_window_event(
                    task_id=task_id,
                    metadata=context_window_metadata,
                )
                raise CompressionRequiredError(
                    reason=_CONTEXT_UNCOMPACTABLE,
                    detail="summary plus three complete turns exceeds context limit",
                )

            through_message_id = validated_candidate.summarized_through_message_id
            if through_message_id is None:
                raise CompressionRequiredError(
                    reason=_COMPRESSION_PERSIST_FAILED,
                    detail="validated compression candidate has no exact cutoff",
                )
            db_session = self._session_factory()
            try:
                snapshot_repository = self._compression_snapshot_repository_factory(
                    db_session
                )
                persisted_snapshot = snapshot_repository.persist_snapshot(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    summary_text=final_text,
                    token_count=int(compression_outcome.final_tokens),
                    compression_epoch_id=compression_epoch_id,
                    source_tokens=snapshot.used_tokens,
                    through_message_id=through_message_id,
                )
                persisted_summary = getattr(persisted_snapshot, "message", None)
                if not isinstance(persisted_summary, str) or not persisted_summary.strip():
                    raise ValueError(
                        "persisted compression snapshot has no summary content"
                    )
                persisted_summary = persisted_summary.strip()
                persisted_token_count = getattr(
                    persisted_snapshot,
                    "token_count",
                    compression_outcome.final_tokens,
                )
                if (
                    isinstance(persisted_token_count, bool)
                    or not isinstance(persisted_token_count, int)
                    or persisted_token_count < 0
                ):
                    persisted_token_count = compression_outcome.final_tokens
                if persisted_summary != final_text:
                    final_text = persisted_summary
                    authoritative_history = validated_candidate.classifier_history(
                        final_text
                    )
                    candidate_prompt_tokens = candidate_classifier_prompt_counter(
                        authoritative_history
                    )
                    if (
                        isinstance(candidate_prompt_tokens, bool)
                        or not isinstance(candidate_prompt_tokens, int)
                        or candidate_prompt_tokens < 0
                    ):
                        raise ValueError(
                            "candidate classifier prompt counter must return a "
                            "non-negative integer"
                        )
                    candidate_request_fits = (
                        candidate_prompt_tokens + reserved_output_tokens
                        <= snapshot.max_tokens
                    )
                    if not candidate_request_fits:
                        raise CompressionRequiredError(
                            reason=_CONTEXT_UNCOMPACTABLE,
                            detail=(
                                "persisted summary plus retained turns exceeds "
                                "context limit"
                            ),
                        )
            except CompressionRequiredError:
                raise
            except Exception as exc:
                if _fail_open(_COMPRESSION_PERSIST_FAILED):
                    return (
                        history_for_facade,
                        context_window_metadata,
                        compression_metadata,
                        True,
                    )
                raise CompressionRequiredError(
                    reason=_COMPRESSION_PERSIST_FAILED,
                    detail="failed to persist validated compression snapshot",
                ) from exc
            finally:
                try:
                    db_session.close()
                except Exception:
                    pass

            # The facade installs the validated candidate through its
            # classifier-only projection. Keep canonical history unchanged for
            # every other prompt role and retain metadata for workflow audit.
            compression_metadata = {
                "applied": True,
                "degraded": compression_outcome.degraded,
                "pass_count": compression_outcome.pass_count,
                "original_tokens": compression_outcome.original_tokens,
                "final_tokens": persisted_token_count,
                "fallback_reason": compression_outcome.fallback_reason,
                "epoch_id": compression_epoch_id,
                "source_tokens": snapshot.used_tokens,
            }
            compression_metadata.update(
                {
                    "candidate_prompt_tokens": candidate_prompt_tokens,
                    "candidate_retained_turns": candidate_retained_turns,
                    "candidate_request_fits": candidate_request_fits,
                    "snapshot_persisted": True,
                }
            )
            context_window_metadata["compression"] = dict(compression_metadata)

        self.emit_context_window_event(task_id=task_id, metadata=context_window_metadata)
        return history_for_facade, context_window_metadata, compression_metadata, True
