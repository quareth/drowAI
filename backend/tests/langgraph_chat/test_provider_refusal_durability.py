"""Database-backed refusal durability tests across interactive turn entrypoints."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.providers.llm.core.exceptions import LLMRefusalOutcome
from backend.database import Base
from backend.models.chat import ChatMessage
from backend.models.core import Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.services.chat.transcript_query_service import ChatTranscriptQueryService
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowService,
    TurnWorkflowState,
)
from backend.services.langgraph_chat.execution.refusal_service import (
    TurnExecutionRefusalService,
)
from backend.services.langgraph_chat.runtime.run_lifecycle import RunLifecycleService


class _RecordingHub:
    """Collect boundary packets emitted by the refusal service."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, task_id: int, event: dict[str, Any]) -> None:
        self.events.append({"task_id": task_id, "event": event})


async def _publish_refusal_boundary(
    *,
    task_id: int,
    hub: _RecordingHub,
    content: str,
    base_metadata: dict[str, Any],
    **_kwargs: Any,
) -> None:
    """Publish the same final snapshot shape consumed by chat clients."""
    await hub.publish(
        task_id,
        {
            "type": "message_delta",
            "content": content,
            "metadata": dict(base_metadata),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "end_status"),
    (("start", "failed"), ("resume", "completed"), ("checkpoint", "completed")),
)
async def test_refusal_remains_declined_after_lifecycle_and_transcript_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    flow: str,
    end_status: str,
) -> None:
    """Start, resume, and retry refusals survive lifecycle finalization and refresh."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.SessionLocal",
        session_factory,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.refusal_service.SessionLocal",
        session_factory,
    )

    with session_factory() as db:
        user = User(username=f"refusal-{flow}", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug=f"refusal-{flow}", name=f"Refusal {flow}")
        db.add(tenant)
        db.flush()
        db.add(
            TenantMembership(
                tenant_id=tenant.id,
                user_id=user.id,
                role="owner",
                status="active",
            )
        )
        task = Task(
            user_id=user.id,
            tenant_id=tenant.id,
            name=f"refusal-{flow}-task",
            status="running",
        )
        db.add(task)
        db.flush()
        assistant = ChatMessage(
            task_id=task.id,
            tenant_id=tenant.id,
            conversation_id=f"conv-{flow}",
            turn_number=1,
            message_type="assistant",
            message="",
            error="stale_error",
        )
        db.add(assistant)
        db.commit()
        task_id = task.id
        assistant_id = assistant.id

    turn_id = f"task-{task_id}-turn-1"
    lifecycle = RunLifecycleService()
    hub = _RecordingHub()
    try:
        with session_factory() as db:
            workflow_service = TurnWorkflowService(db)
            workflow = workflow_service.start_turn(
                task_id=task_id,
                conversation_id=f"conv-{flow}",
                turn_id=turn_id,
                turn_sequence=1,
                graph_name="simple_tool",
                reserved_message_id=assistant_id,
            )
            if flow == "resume":
                workflow_service.mark_waiting_for_human(
                    workflow_id=workflow.id,
                    checkpoint_id="ckpt-resume",
                    graph_name="simple_tool",
                    resume_key="ckpt-resume",
                )
                workflow_service.mark_resumed(
                    workflow_id=workflow.id,
                    checkpoint_id="ckpt-resume",
                    graph_name="simple_tool",
                    resume_key="ckpt-resume",
                )
            elif flow == "checkpoint":
                workflow_service.mark_failed(
                    workflow_id=workflow.id,
                    checkpoint_id="ckpt-retry",
                    graph_name="simple_tool",
                    metadata={
                        "retryable": True,
                        "retry_mode": "checkpoint",
                        "retry_max_attempts": 2,
                    },
                )
                claim = workflow_service.claim_checkpoint_retry(
                    task_id=task_id,
                    turn_id=turn_id,
                    graph_name="simple_tool",
                )
                assert claim.status == "claimed"

            lifecycle.start_run(
                task_id=task_id,
                turn_id=turn_id,
                conversation_id=f"conv-{flow}",
                db_session=db,
            )
            await TurnExecutionRefusalService().handle_terminal_turn_refusal(
                outcome=LLMRefusalOutcome(
                    provider="openai",
                    model="gpt-4o-mini",
                    category="content_filter",
                    explanation="Blocked by policy.",
                ),
                task_id=task_id,
                hub=hub,
                workflow_id=workflow.id,
                reserved_message_id=assistant_id,
                conversation_id=f"conv-{flow}",
                turn_id=turn_id,
                turn_sequence=1,
                graph_name="simple_tool",
                checkpoint_id=(
                    "ckpt-resume"
                    if flow == "resume"
                    else "ckpt-retry" if flow == "checkpoint" else None
                ),
                mark_turn_workflow_failed=workflow_service.mark_failed,
                publish_boundary_completion_events=_publish_refusal_boundary,
            )
            lifecycle.end_run(
                task_id=task_id,
                turn_id=turn_id,
                status=end_status,
                db_session=db,
            )

            refreshed = workflow_service.get_workflow(workflow.id)
            page = ChatTranscriptQueryService(db).list_latest_transcript_page(
                task_id=task_id,
                requested_conversation_id=f"conv-{flow}",
                limit=10,
            )
            persisted_message = db.get(ChatMessage, assistant_id)

        assert refreshed is not None
        assert refreshed.state == TurnWorkflowState.FAILED.value
        assert refreshed.workflow_metadata["terminal_status"] == "declined"
        assert refreshed.workflow_metadata["outcome_type"] == "provider_refusal"
        assistant_item = next(item for item in page.items if item.kind == "assistant")
        assert assistant_item.metadata["status"] == "declined"
        assert assistant_item.metadata["stop_reason"] == "refusal"
        assert assistant_item.metadata["outcome_type"] == "provider_refusal"
        assert assistant_item.metadata["retryable"] is False
        assert "error_code" not in assistant_item.metadata
        assert persisted_message is not None
        assert persisted_message.error is None
        assert persisted_message.message.startswith("The provider declined")
        assert len(hub.events) == 1
        boundary_metadata = hub.events[0]["event"]["metadata"]
        assert boundary_metadata["status"] == "declined"
        assert "error_code" not in boundary_metadata
    finally:
        lifecycle._registry.finish(
            task_id=task_id,
            turn_id=turn_id,
            state="failed",
        )
        engine.dispose()
