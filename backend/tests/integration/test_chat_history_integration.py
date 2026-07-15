"""
Integration tests for transcript-authoritative chat history loading.

These tests validate end-to-end history retrieval from persisted ChatMessage
rows, including conversation isolation and older-page cursor pagination.
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


def _ensure_user_and_task(db, username: str = "integ-stream-user"):
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


def _auth_headers(user: User) -> dict:
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


class TestChatHistoryIntegration:
    def test_load_history_from_transcript_messages(self) -> None:
        db = SessionLocal()
        try:
            user, task = _ensure_user_and_task(db, "integ-transcript-a")
            headers = _auth_headers(user)
            conv_id = "conv-transcript-load"
            _seed_transcript_messages(db, task_id=task.id, conversation_id=conv_id, count=3)

            response = client.get(
                f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert len(payload["items"]) == 3
            assert payload["items"][0]["content"] == "message-1"
            assert payload["items"][1]["content"] == "message-2"
            assert payload["items"][2]["content"] == "message-3"
            assert "events" not in payload
        finally:
            db.close()

    def test_multiple_conversations_are_isolated(self) -> None:
        db = SessionLocal()
        try:
            user, task = _ensure_user_and_task(db, "integ-transcript-b")
            headers = _auth_headers(user)
            conv_a = "conv-transcript-a"
            conv_b = "conv-transcript-b"
            _seed_transcript_messages(db, task_id=task.id, conversation_id=conv_a, count=1)
            _seed_transcript_messages(db, task_id=task.id, conversation_id=conv_b, count=1)

            response_a = client.get(
                f"/api/tasks/{task.id}/chat/history?conversation_id={conv_a}",
                headers=headers,
            )
            assert response_a.status_code == 200, response_a.text
            payload_a = response_a.json()
            assert len(payload_a["items"]) == 1
            assert payload_a["items"][0]["content"] == "message-1"
            assert payload_a["items"][0]["metadata"]["conversation_id"] == conv_a

            response_b = client.get(
                f"/api/tasks/{task.id}/chat/history?conversation_id={conv_b}",
                headers=headers,
            )
            assert response_b.status_code == 200, response_b.text
            payload_b = response_b.json()
            assert len(payload_b["items"]) == 1
            assert payload_b["items"][0]["content"] == "message-1"
            assert payload_b["items"][0]["metadata"]["conversation_id"] == conv_b
        finally:
            db.close()

    def test_pagination_with_before_cursor(self) -> None:
        db = SessionLocal()
        try:
            user, task = _ensure_user_and_task(db, "integ-transcript-c")
            headers = _auth_headers(user)
            conv_id = "conv-transcript-page"
            _seed_transcript_messages(db, task_id=task.id, conversation_id=conv_id, count=8)

            latest = client.get(
                f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}&limit=2",
                headers=headers,
            )
            assert latest.status_code == 200, latest.text
            latest_payload = latest.json()
            assert len(latest_payload["items"]) == 2
            assert latest_payload["hasMoreOlder"] is True
            before_cursor = latest_payload["nextBeforeTurn"]
            assert isinstance(before_cursor, int)

            older = client.get(
                f"/api/tasks/{task.id}/chat/history?conversation_id={conv_id}&before_turn={before_cursor}&limit=2",
                headers=headers,
            )
            assert older.status_code == 200, older.text
            older_payload = older.json()
            assert len(older_payload["items"]) == 2
            assert older_payload["hasMoreOlder"] is True
            assert [item["content"] for item in older_payload["items"]] == ["message-5", "message-6"]
        finally:
            db.close()
