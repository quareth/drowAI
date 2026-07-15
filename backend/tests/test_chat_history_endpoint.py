"""
Endpoint-level tests for GET /api/tasks/{task_id}/chat/history.

Validates transcript startup and older-page reads through the transcript
query service path.
"""

from __future__ import annotations

from typing import Dict

import pytest
from fastapi.testclient import TestClient

from backend.auth import create_access_token
from backend.database import Base, SessionLocal, engine
from backend.main import app
from backend.models.core import Task, User
from backend.tests.test_chat_history import _create_chat_message

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)


def _ensure_user_and_task(db, username: str = "stream-only-user"):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        user = User(username=username, password="x", email=f"{username}@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)

    task = db.query(Task).filter(Task.user_id == user.id).first()
    if not task:
        task = Task(user_id=user.id, name=f"{username}-task")
        db.add(task)
        db.commit()
        db.refresh(task)

    return user, task


def _auth_header_for(user: User) -> Dict[str, str]:
    token = create_access_token({"sub": user.username, "user_id": user.id})
    return {"Authorization": f"Bearer {token}"}


def _seed_transcript_messages(db, *, task_id: int, conversation_id: str, count: int) -> None:
    for idx in range(1, count + 1):
        _create_chat_message(
            db,
            task_id,
            conversation_id,
            None,
            "user" if idx % 2 else "assistant",
            f"message-{idx}",
            turn_number=idx,
        )


def test_history_returns_empty_when_no_messages_exist() -> None:
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db, "transcript-empty")
        headers = _auth_header_for(user)
        conv_id = "conv-empty-transcript"

        response = client.get(
            f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["items"] == []
        assert payload["nextBeforeTurn"] is None
        assert payload["hasMoreOlder"] is False
        assert payload.get("startup") is None
    finally:
        db.close()


def test_history_initial_true_returns_startup_payload_and_page() -> None:
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db, "transcript-initial-startup")
        headers = _auth_header_for(user)
        conv_id = "conv-initial-startup"
        _create_chat_message(db, task.id, conv_id, None, "user", "hello", turn_number=1)
        _create_chat_message(db, task.id, conv_id, None, "assistant", "world", turn_number=2)

        response = client.get(
            f"/api/tasks/{task.id}/chat/history?initial=true&conversation_id={conv_id}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["contractVersion"] == "2026-03-01.chat-history.v2"
        assert isinstance(payload.get("startup"), dict)
        startup = payload["startup"]
        assert startup["task_id"] == task.id
        assert startup["conversation_id"] == conv_id
        assert "chat_ready" in startup
        assert len(payload["items"]) >= 2
        assert payload["items"][0]["kind"] == "user"
        assert payload["items"][0]["content"] == "hello"
        assert payload["items"][1]["kind"] == "assistant"
        assert payload["items"][1]["content"] == "world"
        assert payload["hasMoreOlder"] is False
        assert payload["nextBeforeTurn"] is None
        assert "events" not in payload
    finally:
        db.close()


def test_history_initial_true_rejects_before_cursor() -> None:
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db, "transcript-initial-guard")
        headers = _auth_header_for(user)

        response_before = client.get(
            f"/api/tasks/{task.id}/chat/history?initial=true&before_turn=1",
            headers=headers,
        )
        assert response_before.status_code == 422, response_before.text
        assert "`before_turn` is not allowed when `initial=true`." in response_before.text
    finally:
        db.close()


def test_history_before_cursor_pagination_transcript_only() -> None:
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db, "transcript-before-pagination")
        headers = _auth_header_for(user)
        conv_id = "conv-before-pagination"
        _seed_transcript_messages(db, task_id=task.id, conversation_id=conv_id, count=8)

        response = client.get(
            f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}&limit=10",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert len(payload["items"]) == 8
        assert payload["hasMoreOlder"] is False
        assert payload["nextBeforeTurn"] is None

        latest_two = client.get(
            f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}&limit=2",
            headers=headers,
        )
        assert latest_two.status_code == 200, latest_two.text
        latest_payload = latest_two.json()
        assert len(latest_payload["items"]) == 2
        assert latest_payload["hasMoreOlder"] is True
        before_cursor = latest_payload["nextBeforeTurn"]
        assert isinstance(before_cursor, int)

        older_response = client.get(
            f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}&before_turn={before_cursor}&limit=2",
            headers=headers,
        )
        assert older_response.status_code == 200, older_response.text
        older_payload = older_response.json()
        assert len(older_payload["items"]) == 2
        assert older_payload["hasMoreOlder"] is True
        older_contents = [item["content"] for item in older_payload["items"]]
        assert older_contents == ["message-5", "message-6"]
    finally:
        db.close()


def test_history_before_cursor_must_exist_in_conversation() -> None:
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db, "transcript-invalid-before")
        headers = _auth_header_for(user)
        conv_id = "conv-invalid-before"
        _seed_transcript_messages(db, task_id=task.id, conversation_id=conv_id, count=3)

        response = client.get(
            f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}&before_turn=999999&limit=2",
            headers=headers,
        )
        assert response.status_code == 422, response.text
        assert "`before_turn` cursor is invalid for this conversation." in response.text
    finally:
        db.close()


def test_history_limit_validation() -> None:
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db, "transcript-limit-validation")
        headers = _auth_header_for(user)
        response = client.get(
            f"/api/tasks/{task.id}/chat/history?limit=300",
            headers=headers,
        )
        assert response.status_code == 422, response.text
    finally:
        db.close()


def test_history_requires_authentication() -> None:
    db = SessionLocal()
    try:
        _, task = _ensure_user_and_task(db, "transcript-auth")
        response = client.get(f"/api/tasks/{task.id}/chat/history")
        assert response.status_code in (401, 403), response.text
    finally:
        db.close()


def test_history_enforces_task_ownership() -> None:
    db = SessionLocal()
    try:
        user1, _ = _ensure_user_and_task(db, "transcript-owner-a")
        _, task2 = _ensure_user_and_task(db, "transcript-owner-b")
        headers = _auth_header_for(user1)

        response = client.get(f"/api/tasks/{task2.id}/chat/history", headers=headers)
        assert response.status_code == 404, response.text
    finally:
        db.close()


def test_history_nonexistent_task_returns_404() -> None:
    db = SessionLocal()
    try:
        user, _ = _ensure_user_and_task(db, "transcript-404")
        headers = _auth_header_for(user)
        response = client.get("/api/tasks/999999/chat/history", headers=headers)
        assert response.status_code == 404, response.text
    finally:
        db.close()
