"""Race-condition tests for durable interrupt ticket claiming."""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.hitl import InterruptTicketState
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    InterruptTicketClaimConflictError,
    InterruptTicketService,
)


def test_claim_for_resume_rejects_duplicate_claim_across_sessions() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = handle.name

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db_first = session_factory()
    db_second = session_factory()
    try:
        first_service = InterruptTicketService(db_first)
        second_service = InterruptTicketService(db_second)

        first_service.create_or_update_pending(
            interrupt_id="race-1",
            task_id=9,
            graph_name="deep_reasoning",
            interrupt_type="tool_approval",
        )

        first_claim = first_service.claim_for_resume(interrupt_id="race-1", task_id=9)
        assert first_claim.state == InterruptTicketState.RESUMING

        with pytest.raises(InterruptTicketClaimConflictError):
            second_service.claim_for_resume(interrupt_id="race-1", task_id=9)
    finally:
        db_first.close()
        db_second.close()
        engine.dispose()
        os.unlink(db_path)
