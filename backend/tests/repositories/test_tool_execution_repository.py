"""Tests for ToolExecutionRepository CRUD and query methods."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.tool_execution_repository import ToolExecutionRepository


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_and_task(db, username: str, task_name: str, *, tenant_id: int = 1) -> Task:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, tenant_id=tenant_id, name=task_name)
    db.add(task)
    db.flush()
    return task


def test_create_and_get_by_tool_call_id_task_scoped() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task_one = _seed_user_and_task(db, "repo-user-1", "task-one")
        task_two = _seed_user_and_task(db, "repo-user-2", "task-two")

        created_one = repo.create(
            task_id=task_one.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo one"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-collision",
        )
        repo.create(
            task_id=task_two.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo two"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-collision",
        )
        db.commit()

        fetched = repo.get_by_tool_call_id(task_id=task_one.id, tool_call_id="tc-collision")
        assert fetched is not None
        assert fetched.id == created_one.id
        assert fetched.task_id == task_one.id
    finally:
        db.close()
        engine.dispose()


def test_update_status_sets_finished_fields() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-3", "task-three")
        created = repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={"path": "README.md"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        db.commit()

        finished_at = datetime.now(timezone.utc)
        updated = repo.update_status(
            execution_id=created.id,
            status="completed",
            exit_code=0,
            finished_at=finished_at,
            duration_ms=123,
        )
        db.commit()

        assert updated is not None
        assert updated.status == "completed"
        assert updated.exit_code == 0
        assert updated.duration_ms == 123
        assert updated.finished_at is not None
    finally:
        db.close()
        engine.dispose()


def test_update_status_merges_execution_metadata_patch() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-3b", "task-three-b")
        created = repo.create(
            task_id=task.id,
            tool_name="network.nmap",
            tool_arguments={"target": "10.0.0.5"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
            execution_metadata={
                "tool_metadata": {"source": "parse_output", "parsed_fields": {"hosts": 1}},
                "existing_key": "keep",
            },
        )
        db.commit()

        updated = repo.update_status(
            execution_id=created.id,
            status="completed",
            execution_metadata_patch={
                "tool_metadata": {"semantic_schema_version": "execution_plane.v1"},
                "capability_family": "network",
            },
        )
        db.commit()

        assert updated is not None
        assert updated.execution_metadata == {
            "tool_metadata": {
                "source": "parse_output",
                "parsed_fields": {"hosts": 1},
                "semantic_schema_version": "execution_plane.v1",
            },
            "existing_key": "keep",
            "capability_family": "network",
        }
    finally:
        db.close()
        engine.dispose()


def test_get_by_task_supports_pagination() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-4", "task-four")
        base_time = datetime.now(timezone.utc) - timedelta(minutes=1)

        for idx in range(5):
            repo.create(
                task_id=task.id,
                tool_name=f"tool-{idx}",
                tool_arguments={"index": idx},
                agent_path="langgraph",
                status="started",
                started_at=base_time + timedelta(seconds=idx),
                tool_call_id=f"tc-{idx}",
            )
        db.commit()

        first_page = repo.get_by_task(task_id=task.id, limit=2, offset=0)
        second_page = repo.get_by_task(task_id=task.id, limit=2, offset=2)

        assert len(first_page) == 2
        assert len(second_page) == 2
        assert {row.id for row in first_page}.isdisjoint({row.id for row in second_page})
    finally:
        db.close()
        engine.dispose()


def test_get_by_id_returns_execution() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-5", "task-five")
        created = repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo id"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        db.commit()

        fetched = repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
    finally:
        db.close()
        engine.dispose()


def test_get_by_conversation_turn_filters_by_conversation_and_turn() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-6", "task-six")
        base_time = datetime.now(timezone.utc)
        repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=base_time,
            conversation_id="conv-a",
            turn_id="turn-1",
        )
        repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=base_time + timedelta(seconds=1),
            conversation_id="conv-a",
            turn_id="turn-2",
        )
        repo.create(
            task_id=task.id,
            tool_name="network.nmap",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=base_time + timedelta(seconds=2),
            conversation_id="conv-b",
            turn_id="turn-1",
        )
        db.commit()

        conv_all = repo.get_by_conversation_turn(task_id=task.id, conversation_id="conv-a")
        conv_turn = repo.get_by_conversation_turn(
            task_id=task.id,
            conversation_id="conv-a",
            turn_id="turn-2",
        )

        assert len(conv_all) == 2
        assert len(conv_turn) == 1
        assert conv_turn[0].turn_id == "turn-2"
    finally:
        db.close()
        engine.dispose()


def test_mark_cancel_requested_by_turn_marks_only_non_terminal_rows() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-cancel", "task-cancel", tenant_id=24)
        now = datetime.now(timezone.utc)
        active = repo.create(
            task_id=task.id,
            tenant_id=24,
            tool_name="shell.exec",
            tool_arguments={"command": "sleep 60"},
            agent_path="langgraph",
            status="started",
            started_at=now,
            command_id="cmd-active",
            turn_id="turn-cancel",
            execution_metadata={"existing": {"keep": True}},
        )
        terminal = repo.create(
            task_id=task.id,
            tenant_id=24,
            tool_name="shell.exec",
            tool_arguments={"command": "echo done"},
            agent_path="langgraph",
            status="completed",
            started_at=now,
            finished_at=now,
            command_id="cmd-terminal",
            turn_id="turn-cancel",
        )
        other_turn = repo.create(
            task_id=task.id,
            tenant_id=24,
            tool_name="shell.exec",
            tool_arguments={"command": "sleep 30"},
            agent_path="langgraph",
            status="started",
            started_at=now,
            command_id="cmd-other",
            turn_id="turn-other",
        )
        db.commit()

        updated = repo.mark_cancel_requested_by_turn(
            tenant_id=24,
            task_id=task.id,
            turn_id="turn-cancel",
            reason="user_stop",
            requested_at=now,
        )
        db.commit()

        assert [row.id for row in updated] == [active.id]
        db.refresh(active)
        db.refresh(terminal)
        db.refresh(other_turn)
        assert active.status == "cancel_requested"
        assert active.execution_metadata["existing"] == {"keep": True}
        assert active.execution_metadata["cancellation"]["cancel_requested"] is True
        assert active.execution_metadata["cancellation"]["process_state"] == "orphaned_until_terminal"
        assert active.execution_metadata["cancellation"]["runtime_kill_supported"] is False
        assert terminal.status == "completed"
        assert terminal.execution_metadata == {}
        assert other_turn.status == "started"
    finally:
        db.close()
        engine.dispose()


def test_mark_cancel_requested_by_turn_does_not_rewrite_already_requested_rows() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-cancel-idem", "task-cancel-idem", tenant_id=24)
        now = datetime.now(timezone.utc)
        existing_requested_at = (now - timedelta(minutes=5)).isoformat()
        active = repo.create(
            task_id=task.id,
            tenant_id=24,
            tool_name="shell.exec",
            tool_arguments={"command": "sleep 60"},
            agent_path="langgraph",
            status="cancel_requested",
            started_at=now,
            command_id="cmd-active",
            turn_id="turn-cancel",
            execution_metadata={
                "cancellation": {
                    "cancel_requested": True,
                    "reason": "first_stop",
                    "requested_at": existing_requested_at,
                    "process_state": "orphaned_until_terminal",
                }
            },
        )
        db.commit()

        updated = repo.mark_cancel_requested_by_turn(
            tenant_id=24,
            task_id=task.id,
            turn_id="turn-cancel",
            reason="second_stop",
            requested_at=now,
        )
        db.commit()

        assert updated == []
        db.refresh(active)
        assert active.status == "cancel_requested"
        assert active.execution_metadata["cancellation"]["reason"] == "first_stop"
        assert active.execution_metadata["cancellation"]["requested_at"] == existing_requested_at
    finally:
        db.close()
        engine.dispose()


def test_create_sets_tenant_id_from_task_ownership() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-tenant", "task-tenant", tenant_id=42)
        created = repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "id"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        db.commit()

        assert created.tenant_id == 42
    finally:
        db.close()
        engine.dispose()


def test_get_by_tool_name_filters_by_tool() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-7", "task-seven")
        base_time = datetime.now(timezone.utc)
        repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=base_time,
        )
        repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=base_time + timedelta(seconds=1),
        )
        db.commit()

        shell_rows = repo.get_by_tool_name(task_id=task.id, tool_name="shell.exec")
        assert len(shell_rows) == 1
        assert shell_rows[0].tool_name == "shell.exec"
    finally:
        db.close()
        engine.dispose()


def test_get_by_time_range_filters_started_at_window() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-8", "task-eight")
        t0 = datetime.now(timezone.utc)
        repo.create(
            task_id=task.id,
            tool_name="tool-before",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=t0,
        )
        repo.create(
            task_id=task.id,
            tool_name="tool-middle",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=t0 + timedelta(minutes=1),
        )
        repo.create(
            task_id=task.id,
            tool_name="tool-after",
            tool_arguments={},
            agent_path="langgraph",
            status="started",
            started_at=t0 + timedelta(minutes=2),
        )
        db.commit()

        rows = repo.get_by_time_range(
            task_id=task.id,
            start_time=t0 + timedelta(seconds=30),
            end_time=t0 + timedelta(minutes=1, seconds=30),
        )
        assert len(rows) == 1
        assert rows[0].tool_name == "tool-middle"
    finally:
        db.close()
        engine.dispose()


def test_create_enforces_unique_tool_call_id_per_task() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-9", "task-nine")
        started_at = datetime.now(timezone.utc)

        repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo first"},
            agent_path="langgraph",
            status="started",
            started_at=started_at,
            tool_call_id="tc-unique",
        )
        db.commit()

        with pytest.raises(IntegrityError):
            repo.create(
                task_id=task.id,
                tool_name="shell.exec",
                tool_arguments={"command": "echo second"},
                agent_path="langgraph",
                status="started",
                started_at=started_at + timedelta(seconds=1),
                tool_call_id="tc-unique",
            )
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def test_get_by_tenant_task_execution_id_denies_cross_tenant_lookup() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-10", "task-ten", tenant_id=77)
        created = repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo tenant"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        db.commit()

        owned = repo.get_by_tenant_task_execution_id(
            tenant_id=77,
            task_id=task.id,
            execution_id=created.id,
        )
        cross_tenant = repo.get_by_tenant_task_execution_id(
            tenant_id=78,
            task_id=task.id,
            execution_id=created.id,
        )

        assert owned is not None
        assert owned.id == created.id
        assert cross_tenant is None
    finally:
        db.close()
        engine.dispose()


def test_update_status_by_tenant_scope_updates_only_owned_execution() -> None:
    engine, db = _build_session()
    try:
        repo = ToolExecutionRepository(db)
        task = _seed_user_and_task(db, "repo-user-11", "task-eleven", tenant_id=88)
        created = repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo tenant-update"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        db.commit()

        denied = repo.update_status_by_tenant_scope(
            tenant_id=89,
            task_id=task.id,
            execution_id=created.id,
            status="failed",
        )
        updated = repo.update_status_by_tenant_scope(
            tenant_id=88,
            task_id=task.id,
            execution_id=created.id,
            status="completed",
            exit_code=0,
        )
        db.commit()

        assert denied is None
        assert updated is not None
        assert updated.status == "completed"
        assert updated.exit_code == 0
    finally:
        db.close()
        engine.dispose()
