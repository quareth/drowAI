"""
LangGraph graph execution with streaming and batch invocation support.

Responsibilities:
- Execute compiled graphs with streaming (astream) or batch (ainvoke)
- Capture final state from stream or checkpointer
- Forward streaming events to stream hub
- Handle streaming fallback to batch on errors
- Emit execution metrics

Out of scope:
- Graph compilation (handled by handlers)
- Checkpointer management (handled by checkpointer service)
- Event processing (handled by streaming adapter)
- State building (handled by handlers/facade)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

from agent.graph.utils.provider_model_resolution import resolve_graph_provider_model_ref
from backend.database import SessionLocal
from backend.core.time_utils import format_iso, utc_now
from backend.services.chat import event_builders
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
from backend.services.metrics.utils import safe_inc
from backend.services.langgraph_chat.hitl_constants import DEFAULT_GRAPH_NAME
from backend.services.langgraph_chat.compression.window_manager import (
    ContextWindowManager,
    resolve_context_window_max_tokens,
)
from backend.services.langgraph_chat.streaming.status_events import (
    emit_context_window_event,
)
from backend.services.llm_provider.runtime_services import strip_runtime_services
from backend.services.langgraph_chat.diagnostic_logger import (
    get_diagnostic_logger,
    log_graph_execution,
    log_streaming_event,
    log_state_capture,
)

logger = logging.getLogger(__name__)
diag = get_diagnostic_logger()


@dataclass(slots=True)
class GraphExecutionResult:
    final_state: Optional[Dict[str, Any]]
    interrupt: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def interrupted(self) -> bool:
        return self.interrupt is not None


class LangGraphExecutor:
    """Executes LangGraph graphs with streaming and batch support."""

    def __init__(self, streaming_adapter: Optional[Any] = None) -> None:
        """Initialize executor with streaming adapter.
        
        Args:
            streaming_adapter: Adapter for processing streaming events.
                             If None, will not process custom events.
        """
        self._streaming_adapter = streaming_adapter
        self._stream_hub = get_in_memory_stream_hub()

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: Any,
        config: Dict[str, Any],
        task_id: int,
        state_container: Optional["ChatStateContainer"] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> GraphExecutionResult:
        """Stream graph execution with real-time event forwarding.
        
        Uses LangGraph's astream with stream_mode="custom" to receive
        custom events emitted by nodes via StreamWriter.
        
        When state_container is provided (Phase 3), the adapter accumulates
        answer/reasoning/tool_call state for ChatMessage updates.
        
        Args:
            compiled_graph: Compiled LangGraph instance
            graph_input: Initial state for graph
            config: LangGraph config with thread settings
            task_id: Task identifier for routing events
        state_container: Optional ChatStateContainer to accumulate state (Phase 3)
            
        Returns:
            GraphExecutionResult with the final state and optional interrupt payload.
            
        Raises:
            Exception: If streaming fails (caller should handle fallback)
        """
        logger.info(f"[STREAMING] Starting stream for task {task_id}")
        safe_inc("langgraph_streaming_sessions_started")

        return await self._stream_graph_impl(
            compiled_graph, graph_input, config, task_id, state_container, should_cancel
        )

    async def _stream_graph_impl(
        self,
        compiled_graph: Any,
        graph_input: Any,
        config: Dict[str, Any],
        task_id: int,
        state_container: Optional["ChatStateContainer"] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> GraphExecutionResult:
        """Inner stream implementation; events are forwarded to hub after adapter processing."""
        event_count = 0
        final_state = None
        checkpoint_context_window_metadata: Optional[Dict[str, Any]] = None

        try:
            # Log graph execution details to diagnostic file
            # Handle both dict inputs and Command objects (for resume)
            input_info = (
                list(graph_input.keys()) 
                if isinstance(graph_input, dict) 
                else f"Command({type(graph_input).__name__})"
            )
            log_graph_execution(
                task_id,
                compiled_graph.__class__.__name__,
                {
                    "has_astream": hasattr(compiled_graph, 'astream'),
                    "config_keys": list(config.keys()),
                    "thread_id": config.get('configurable', {}).get('thread_id'),
                    "input_keys": input_info,
                }
            )
            
            # ✅ Use BOTH custom (for writer) and values (for checkpointing)
            # Official LangGraph docs confirm this works!
            # - custom mode: enables StreamWriter injection for real-time events
            # - values mode: triggers automatic checkpointing AND state capture
            #   (also the channel on which __interrupt__ payloads arrive when
            #    only `custom` and `values` are subscribed)
            diag.info(
                f"EXECUTOR | Task {task_id} | Starting astream with modes: ['custom', 'values']"
            )
            logger.info(f"[EXECUTOR] Starting astream for task {task_id}")

            last_state = None  # Track the latest complete state from values events
            detected_interrupt = None  # Track interrupt data if detected
            
            # ⚠️ CRITICAL: With multiple stream_mode, events come as (mode, data) tuples!
            async for mode, chunk in compiled_graph.astream(
                graph_input,
                config=config,
                stream_mode=["custom", "values"],  # custom for writer + values for checkpointing/interrupts
            ):
                if should_cancel and should_cancel():
                    logger.info("[EXECUTOR] Cancellation requested for task %s", task_id)
                    raise RuntimeError("run_cancelled")
                logger.info(f"[EXECUTOR] Stream event: mode={mode}, chunk_type={type(chunk).__name__}")
                event_count += 1
                
                # Handle VALUES mode events: full state dict after each node
                if mode == "values":
                    state_keys = list(chunk.keys()) if isinstance(chunk, dict) else []
                    log_streaming_event(task_id, event_count, "values", f"state_keys={len(state_keys)}")
                    logger.info(f"[EXECUTOR] Values event keys: {state_keys[:10]}")
                    last_state = chunk  # Keep the latest complete state
                    observed_context_window_metadata = await self._observe_context_window_checkpoint(
                        task_id=task_id,
                        chunk=chunk,
                    )
                    if isinstance(observed_context_window_metadata, dict):
                        checkpoint_context_window_metadata = observed_context_window_metadata
                    if isinstance(chunk, dict) and "__interrupt__" in chunk:
                        logger.info(f"[EXECUTOR] Interrupt detected in values event for task {task_id}")
                        detected_interrupt = chunk["__interrupt__"]
                        await self._handle_interrupt(task_id, config, detected_interrupt)
                        logger.info("[EXECUTOR] _handle_interrupt completed, continuing stream to allow checkpoint save")
                        # DON'T return early - let the stream complete so checkpoint is saved!
                
                # Handle CUSTOM mode events: {"type": "...", "data": ...} dicts
                elif mode == "custom":
                    event_type = chunk.get('type') if isinstance(chunk, dict) else 'unknown'
                    log_streaming_event(task_id, event_count, "custom", event_type)
                    
                    # Process CUSTOM event through adapter (validation, enrichment)
                    if isinstance(chunk, dict) and "type" in chunk:
                        if self._streaming_adapter:
                            processed_event = self._streaming_adapter.process_streaming_event(
                                chunk, state_container=state_container
                            )
                            
                            if processed_event:
                                # Forward to stream hub for SSE/WebSocket delivery
                                await self._forward_streaming_event(task_id, processed_event)
                                logger.info(f"[STREAMING] Forwarded custom event: {chunk.get('type')}")
                            else:
                                logger.debug("[STREAMING] Custom event filtered by adapter")
                        else:
                            logger.debug("[STREAMING] No adapter configured")

                # Unknown mode
                else:
                    diag.warning(f"EXECUTOR | Task {task_id} | Unknown stream mode: {mode}")

            diag.info(f"EXECUTOR | Task {task_id} | Stream loop completed | total_events={event_count}")
            
            # ✅ If interrupt was detected, return it now (after stream completed and checkpoint saved)
            if detected_interrupt is not None:
                logger.info(f"[EXECUTOR] Stream completed, returning interrupt state for task {task_id}")
                safe_inc("langgraph_streaming_sessions_interrupted")
                return GraphExecutionResult(
                    final_state=last_state,
                    interrupt=detected_interrupt,
                    metadata=self._build_context_window_metadata_payload(
                        checkpoint_context_window_metadata
                    ),
                )
            
            # ✅ Use the last state from VALUES events (most recent complete state)
            # Values mode automatically checkpoints, so this state is persisted AND we get it in events!
            if last_state:
                state_keys = list(last_state.keys()) if isinstance(last_state, dict) else []
                log_state_capture(task_id, True, state_keys)
                final_state = last_state
            else:
                # Some resume paths can complete without emitting a values event.
                # Recover from the checkpointer-backed graph state when available.
                final_state = await self._recover_state_from_checkpoint(
                    compiled_graph=compiled_graph,
                    config=config,
                    task_id=task_id,
                )
                if final_state is None:
                    log_state_capture(task_id, False)
            
            # ✅ With values mode, we get state directly from events!
            # No need to query checkpointer - it auto-persists AND streams state to us
            # Old checkpointer retrieval code removed since VALUES mode handles everything
            
            logger.info(f"[STREAMING] Stream completed for task {task_id}")
            safe_inc("langgraph_streaming_sessions_completed")
            
            return GraphExecutionResult(
                final_state=final_state,
                metadata=self._build_context_window_metadata_payload(
                    checkpoint_context_window_metadata
                ),
            )

        except Exception as exc:
            logger.error(f"[STREAMING] Stream error for task {task_id}: {exc}", exc_info=True)
            safe_inc("langgraph_streaming_sessions_failed")
            raise

    async def _observe_context_window_checkpoint(
        self,
        *,
        task_id: int,
        chunk: Any,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate and emit context-window status without rewriting checkpoint state."""
        extracted = self._extract_context_window_inputs(chunk)
        if extracted is None:
            return None

        conversation_id, provider, model, history, projected_user_message = extracted
        try:
            max_tokens = resolve_context_window_max_tokens(
                provider=provider,
                model=model,
            )
            decision = ContextWindowManager(max_tokens=max_tokens).evaluate_history(
                task_id=task_id,
                conversation_id=conversation_id,
                history=history,
                provider=provider,
                model=model,
                projected_user_message=projected_user_message,
            )
        except Exception:
            logger.debug(
                "Checkpoint context-window evaluation failed (task=%s, conversation_id=%s)",
                task_id,
                conversation_id,
                exc_info=True,
            )
            return None

        if not decision.ceiling_reached:
            return None

        snapshot = decision.snapshot
        logger.info(
            "[EXECUTOR] Checkpoint context ceiling reached (task=%s, conversation_id=%s, used=%s, max=%s)",
            task_id,
            snapshot.conversation_id,
            snapshot.used_tokens,
            snapshot.max_tokens,
        )
        context_window_metadata: Dict[str, Any] = {
            "conversation_id": snapshot.conversation_id,
            "max_tokens": snapshot.max_tokens,
            "used_tokens": snapshot.used_tokens,
            "remaining_tokens": snapshot.remaining_tokens,
            "ratio": snapshot.ratio,
            "ceiling_reached": decision.ceiling_reached,
            "recommended_next_action": decision.recommended_next_action,
            "compression_candidate": decision.compression_candidate,
            "turn_sequence": None,
            "revision": -1,
            "snapshot_kind": "bootstrap_estimate",
        }
        self._emit_context_window_status(task_id, context_window_metadata)
        return context_window_metadata

    @staticmethod
    def _emit_context_window_status(task_id: int, metadata: Dict[str, Any]) -> None:
        """Emit context-window status with additive compression schema fields."""
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
        emit_context_window_event(
            task_id=task_id,
            conversation_id=str(metadata.get("conversation_id") or ""),
            max_tokens=int(metadata.get("max_tokens", 0)),
            used_tokens=int(metadata.get("used_tokens", 0)),
            remaining_tokens=int(metadata.get("remaining_tokens", 0)),
            ratio=float(metadata.get("ratio", 0.0)),
            ceiling_reached=bool(metadata.get("ceiling_reached", False)),
            recommended_next_action=str(metadata.get("recommended_next_action", "none")),
            compression_candidate=bool(metadata.get("compression_candidate", False)),
            compression_pass_count=compression_pass_count,
            compression_tokens_before=compression_tokens_before,
            compression_tokens_after=compression_tokens_after,
            compression_degraded=compression_degraded,
            turn_sequence=metadata.get("turn_sequence"),
            revision=metadata.get("revision"),
            snapshot_kind=metadata.get("snapshot_kind"),
        )

    @staticmethod
    def _build_context_window_metadata_payload(
        context_window: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build stable executor metadata payload for upstream handoff."""
        if not isinstance(context_window, dict):
            return {}
        return {"context_window": dict(context_window)}

    @staticmethod
    def _extract_context_window_inputs(
        chunk: Any,
    ) -> Optional[tuple[str, str, str, list[dict[str, Any]], Optional[str]]]:
        """Extract context-window evaluation inputs from a values chunk."""
        if not isinstance(chunk, dict):
            return None

        facts = chunk.get("facts")
        if not isinstance(facts, dict):
            return None

        metadata = facts.get("metadata")
        if not isinstance(metadata, dict):
            return None

        conversation_id = facts.get("conversation_id") or metadata.get("conversation_id")
        provider_model = resolve_graph_provider_model_ref(metadata)
        history = metadata.get("conversation_history")
        projected_user_message_raw = facts.get("message")

        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return None
        if provider_model is None:
            return None
        if not isinstance(history, list):
            return None

        normalized_history = [entry for entry in history if isinstance(entry, dict)]
        projected_user_message: Optional[str] = None
        if isinstance(projected_user_message_raw, str):
            projected = projected_user_message_raw.strip()
            if projected:
                projected_user_message = projected

        return (
            conversation_id.strip(),
            provider_model.provider,
            provider_model.model,
            normalized_history,
            projected_user_message,
        )

    async def _recover_state_from_checkpoint(
        self,
        *,
        compiled_graph: Any,
        config: Dict[str, Any],
        task_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Recover final state from graph snapshots when stream values are missing.

        Architectural invariant:
        - `checkpoint_id` is the resume anchor (where execution starts), not the
          canonical identity of the latest state after execution completes.
        - Final-state recovery must therefore query latest thread state first.
        """
        aget_state = getattr(compiled_graph, "aget_state", None)
        get_state = getattr(compiled_graph, "get_state", None)
        if not callable(aget_state) and not callable(get_state):
            return None

        candidate_configs = self._build_recovery_configs(config)
        for idx, candidate in enumerate(candidate_configs, start=1):
            try:
                if callable(aget_state):
                    snapshot = await aget_state(candidate)
                else:
                    snapshot = get_state(candidate)
            except Exception:
                logger.debug(
                    "[STREAMING] State snapshot recovery attempt %s failed for task %s",
                    idx,
                    task_id,
                    exc_info=True,
                )
                continue

            recovered = self._extract_state_values(snapshot)
            if recovered is None:
                continue

            state_keys = list(recovered.keys())
            logger.info(
                "[STREAMING] Recovered final state from snapshot attempt %s for task %s (keys=%s)",
                idx,
                task_id,
                state_keys[:10],
            )
            log_state_capture(task_id, True, state_keys)
            return recovered

        return None

    @staticmethod
    def _build_recovery_configs(config: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Build ordered snapshot configs for reliable post-resume state recovery."""
        base = strip_runtime_services(config)
        base_configurable = dict(base.get("configurable") or {})
        has_checkpoint = "checkpoint_id" in base_configurable and base_configurable.get("checkpoint_id") is not None

        if not has_checkpoint:
            return [base]

        latest = dict(base)
        latest_configurable = dict(base_configurable)
        latest_configurable.pop("checkpoint_id", None)
        latest["configurable"] = latest_configurable

        anchored = dict(base)
        anchored["configurable"] = base_configurable
        return [latest, anchored]

    @staticmethod
    def _extract_state_values(snapshot: Any) -> Optional[Dict[str, Any]]:
        """Normalize graph state snapshot into a state dict."""
        if isinstance(snapshot, Mapping):
            return dict(snapshot)

        values = getattr(snapshot, "values", None)
        if isinstance(values, Mapping):
            return dict(values)

        return None
    
    async def _forward_streaming_event(
        self,
        task_id: int,
        event: Dict[str, Any],
    ) -> None:
        """Forward streaming event to stream hub.
        
        Args:
            task_id: Task identifier
            event: Processed event ready for delivery
        """
        try:
            await self._stream_hub.publish(
                task_id=task_id,
                event=event,
            )
            safe_inc("langgraph_streaming_events_forwarded")
        except Exception as exc:
            # Don't fail entire stream for forwarding errors
            logger.warning(f"Failed to forward event for task {task_id}: {exc}")

    async def _handle_interrupt(
        self,
        task_id: int,
        config: Dict[str, Any],
        interrupt_data: Any,
    ) -> None:
        """Emit interrupt event to stream hub.
        
        Note: Interrupt state is persisted by the LangGraph checkpointer automatically.
        We only need to emit the event for real-time UI updates.
        """
        thread_id = config.get("configurable", {}).get("thread_id", "unknown")
        graph_name = config.get("configurable", {}).get("graph_name", DEFAULT_GRAPH_NAME)
        payload = self._extract_interrupt_payload(interrupt_data)
        interrupt_type = payload.get("type", "unknown")
        # IMPORTANT: config.checkpoint_id is the resume anchor supplied by caller.
        # It is not guaranteed to be the identity of the newly created interrupt.
        # Only trust payload-provided checkpoint_id for interrupt identity.
        payload_checkpoint = payload.get("checkpoint_id")
        checkpoint_id = None
        if isinstance(payload_checkpoint, (int, str)):
            payload_checkpoint_str = str(payload_checkpoint).strip()
            checkpoint_id = payload_checkpoint_str or None
        interrupt_id = payload.get("interrupt_id")
        if not isinstance(interrupt_id, str) or not interrupt_id.strip():
            interrupt_id = self._synthesize_interrupt_id(
                task_id=task_id,
                graph_name=graph_name,
                payload=payload,
                checkpoint_id=checkpoint_id,
            )
        payload["interrupt_id"] = interrupt_id
        if checkpoint_id is not None:
            payload.setdefault("checkpoint_id", checkpoint_id)

        self._register_observed_interrupt_ticket(
            task_id=task_id,
            graph_name=graph_name,
            interrupt_type=interrupt_type,
            interrupt_id=interrupt_id,
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            payload=payload,
        )

        event_builder = getattr(event_builders, "build_interrupt_event", None)
        if not callable(event_builder):
            logger.warning(
                "build_interrupt_event missing; falling back to local builder for task %s",
                task_id,
            )
            event_builder = self._fallback_interrupt_event

        event = event_builder(
            task_id=task_id,
            thread_id=thread_id,
            interrupt_type=interrupt_type,
            payload=payload,
            graph_name=graph_name,
            interrupt_id=interrupt_id,
            checkpoint_id=checkpoint_id,
        )
        await self._stream_hub.publish(task_id=task_id, event=event)
        logger.info(
            "[EXECUTOR] Emitted interrupt event for task %s, graph=%s",
            task_id,
            graph_name,
        )
        self._schedule_runtime_warmup(
            task_id=task_id,
            graph_name=graph_name,
            workspace_path=self._extract_workspace_path(config),
        )

    def _schedule_runtime_warmup(
        self,
        *,
        task_id: int,
        graph_name: str,
        workspace_path: Optional[str],
    ) -> None:
        """Schedule best-effort runtime warmup during HITL wait window."""
        try:
            from backend.services.langgraph_chat.runtime.warmup_service import (
                get_shared_runtime_warmup_service,
            )

            warmup_service = get_shared_runtime_warmup_service()
        except Exception:
            logger.warning(
                "Failed to initialize runtime warmup service for task %s",
                task_id,
                exc_info=True,
            )
            return

        async def _run_warmup() -> None:
            try:
                await warmup_service.warm_task_runtime(
                    task_id=task_id,
                    graph_name=graph_name,
                    workspace_path=workspace_path,
                )
            except Exception:
                logger.warning(
                    "Best-effort runtime warmup failed (task_id=%s graph=%s)",
                    task_id,
                    graph_name,
                    exc_info=True,
                )

        asyncio.create_task(_run_warmup())

    def _register_observed_interrupt_ticket(
        self,
        *,
        task_id: int,
        graph_name: str,
        interrupt_type: str,
        interrupt_id: str,
        checkpoint_id: Optional[str],
        thread_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Best-effort authoritative registration for observed interrupts."""
        db = SessionLocal()
        try:
            from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import InterruptTicketService

            turn_id_raw = payload.get("turn_id")
            turn_id = turn_id_raw.strip() if isinstance(turn_id_raw, str) and turn_id_raw.strip() else None
            turn_sequence_raw = payload.get("turn_sequence")
            turn_sequence = (
                turn_sequence_raw
                if isinstance(turn_sequence_raw, int) and not isinstance(turn_sequence_raw, bool)
                else None
            )
            tool_call_id_raw = payload.get("tool_call_id")
            tool_call_id = (
                tool_call_id_raw.strip()
                if isinstance(tool_call_id_raw, str) and tool_call_id_raw.strip()
                else None
            )

            ticket_service = InterruptTicketService(db)
            ticket_service.create_or_update_pending(
                interrupt_id=interrupt_id,
                task_id=task_id,
                graph_name=graph_name,
                interrupt_type=interrupt_type,
                checkpoint_id=checkpoint_id,
                thread_id=thread_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                tool_call_id=tool_call_id,
                payload_snapshot=dict(payload) if isinstance(payload, dict) else None,
            )
        except Exception:
            logger.warning(
                "Failed to create/update interrupt ticket (task=%s, interrupt_id=%s)",
                task_id,
                interrupt_id,
                exc_info=True,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

    @staticmethod
    def _extract_interrupt_payload(interrupt_data: Any) -> Dict[str, Any]:
        """Normalize interrupt data into a payload dict."""
        candidate = interrupt_data
        if isinstance(interrupt_data, (list, tuple)) and interrupt_data:
            candidate = interrupt_data[0]
        if hasattr(candidate, "value"):
            candidate = candidate.value
        if isinstance(candidate, dict):
            return dict(candidate)
        return {"type": "unknown"}

    @staticmethod
    def _extract_workspace_path(config: Dict[str, Any]) -> Optional[str]:
        """Extract workspace path from known LangGraph config sources."""
        configurable = config.get("configurable", {})
        runtime_context = configurable.get("graph_runtime_context")
        if isinstance(runtime_context, dict):
            workspace_path = runtime_context.get("workspace_path")
            if isinstance(workspace_path, str) and workspace_path.strip():
                return workspace_path

        runtime_projection = configurable.get("runtime_projection")
        if isinstance(runtime_projection, dict):
            workspace_path = runtime_projection.get("workspace_path")
            if isinstance(workspace_path, str) and workspace_path.strip():
                return workspace_path
        return None

    @staticmethod
    def _fallback_interrupt_event(
        *,
        task_id: int,
        thread_id: str,
        interrupt_type: str,
        payload: Dict[str, Any],
        graph_name: str,
        interrupt_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a minimal interrupt event if the shared builder is unavailable."""
        return {
            "type": "graph_interrupt",
            "task_id": task_id,
            "thread_id": thread_id,
            "interrupt_id": interrupt_id,
            "checkpoint_id": checkpoint_id,
            "interrupt_type": interrupt_type,
            "payload": payload,
            "graph_name": graph_name,
            "timestamp": format_iso(utc_now()),
        }

    @staticmethod
    def _synthesize_interrupt_id(
        *,
        task_id: int,
        graph_name: str,
        payload: Dict[str, Any],
        checkpoint_id: Optional[str],
    ) -> str:
        if isinstance(checkpoint_id, str) and checkpoint_id.strip():
            return f"{graph_name}:checkpoint:{checkpoint_id.strip()}"
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id.strip():
            return f"{graph_name}:turn:{turn_id.strip()}"
        turn_sequence = payload.get("turn_sequence")
        if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
            return f"{graph_name}:turn-seq:{turn_sequence}"
        return f"{graph_name}:task:{task_id}:legacy"

    async def invoke_graph(
        self,
        compiled_graph: Any,
        graph_input: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Batch invoke graph execution.
        
        Args:
            compiled_graph: Compiled LangGraph instance
            graph_input: Initial state for graph
            config: LangGraph config with thread settings
            
        Returns:
            Final state dict from graph execution
        """
        if hasattr(compiled_graph, "ainvoke"):
            result_state = await compiled_graph.ainvoke(graph_input, config=config)
        else:  # pragma: no cover - fallback for sync-only runtimes
            result_state = compiled_graph.invoke(graph_input, config=config)
        return result_state


__all__ = ["LangGraphExecutor"]
