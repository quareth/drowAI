"""Persistence for hidden context-compression ChatMessage snapshots.

This repository owns the temporary schema overload where compression epoch
metadata is stored in ``ChatMessage.citations["context_compression"]``.
Move that payload to a dedicated column or table in a follow-up schema change.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage
from backend.models.core import Task
from backend.services.chat.message_service import ChatMessageService
from backend.services.chat.conversation_history_reader import SYSTEM_SUMMARY_MESSAGE_TYPE
from backend.services.langgraph_chat.compression.context_models import (
    CompressionEpochMetadata,
)

COMPRESSION_EPOCH_METADATA_KEY = "context_compression"


class CompressionSnapshotRepository:
    """Persist and read hidden SYSTEM_SUMMARY snapshots and epoch metadata."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def persist_snapshot(
        self,
        *,
        task_id: int,
        conversation_id: str,
        summary_text: str,
        token_count: int = 0,
        compression_epoch_id: Optional[str] = None,
        source_tokens: Optional[int] = None,
        through_message_id: Optional[int] = None,
    ) -> ChatMessage:
        """Persist one atomic, parentless snapshot per epoch/cutoff pair."""
        if through_message_id is not None and (
            isinstance(through_message_id, bool)
            or not isinstance(through_message_id, int)
            or through_message_id <= 0
        ):
            raise ValueError("through_message_id must be a positive integer")

        try:
            if compression_epoch_id is not None and through_message_id is not None:
                existing = self._existing_snapshot_for_epoch(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    compression_epoch_id=compression_epoch_id,
                    through_message_id=through_message_id,
                )
                if existing is not None:
                    self.db.commit()
                    self.db.refresh(existing)
                    return existing

            chat_messages = ChatMessageService(self.db)
            summary_msg = chat_messages.reserve_message(
                task_id=task_id,
                conversation_id=conversation_id,
                parent_message_id=None,
                message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
                turn_number=None,
            )

            citations: Optional[dict[str, Any]] = None
            if compression_epoch_id is not None:
                epoch_metadata = CompressionEpochMetadata(
                    epoch_id=compression_epoch_id,
                    source_tokens=max(0, int(source_tokens or 0)),
                    through_message_id=through_message_id,
                )
                citations = {
                    COMPRESSION_EPOCH_METADATA_KEY: {
                        "epoch_id": epoch_metadata.epoch_id,
                        "source_tokens": epoch_metadata.source_tokens,
                    }
                }
                if epoch_metadata.through_message_id is not None:
                    citations[COMPRESSION_EPOCH_METADATA_KEY]["through_message_id"] = (
                        epoch_metadata.through_message_id
                    )

            if citations is None:
                chat_messages.update_message(
                    summary_msg.id,
                    summary_text,
                    token_count=token_count,
                )
            else:
                chat_messages.update_message(
                    summary_msg.id,
                    summary_text,
                    token_count=token_count,
                    citations=citations,
                )

            self.db.commit()
            self.db.refresh(summary_msg)
            return summary_msg
        except Exception:
            self.db.rollback()
            raise

    def _existing_snapshot_for_epoch(
        self,
        *,
        task_id: int,
        conversation_id: str,
        compression_epoch_id: str,
        through_message_id: int,
    ) -> Optional[ChatMessage]:
        """Serialize task writes and resolve an existing epoch/cutoff pair."""
        task_row_id = self.db.execute(
            select(Task.id).where(Task.id == task_id).with_for_update()
        ).scalar_one_or_none()
        if task_row_id is None:
            raise ValueError(f"Task id={task_id} not found for snapshot persistence")

        snapshots = list(
            self.db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.task_id == task_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.message_type == SYSTEM_SUMMARY_MESSAGE_TYPE,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            )
            .scalars()
            .unique()
            .all()
        )
        for snapshot in snapshots:
            citations = getattr(snapshot, "citations", None)
            if not isinstance(citations, dict):
                continue
            payload = citations.get(COMPRESSION_EPOCH_METADATA_KEY)
            if not isinstance(payload, dict):
                continue
            if payload.get("epoch_id") != compression_epoch_id:
                continue
            persisted_cutoff = payload.get("through_message_id")
            if (
                not isinstance(persisted_cutoff, bool)
                and isinstance(persisted_cutoff, int)
                and persisted_cutoff == through_message_id
            ):
                return snapshot
            raise ValueError(
                "compression epoch already exists with a different cutoff"
            )
        return None

    def latest_snapshot(
        self,
        *,
        task_id: int,
        conversation_id: str,
    ) -> Optional[ChatMessage]:
        """Return the latest hidden compressed-context snapshot message."""
        return self.db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.task_id == task_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.message_type == SYSTEM_SUMMARY_MESSAGE_TYPE,
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def latest_epoch_metadata(
        self,
        *,
        task_id: int,
        conversation_id: str,
    ) -> Optional[CompressionEpochMetadata]:
        """Read compression epoch metadata from the latest summary snapshot."""
        snapshot = self.latest_snapshot(
            task_id=task_id,
            conversation_id=conversation_id,
        )
        if snapshot is None:
            return None
        citations = getattr(snapshot, "citations", None) or {}
        if not isinstance(citations, dict):
            return None
        payload = citations.get(COMPRESSION_EPOCH_METADATA_KEY)
        if not isinstance(payload, dict):
            return None
        epoch_id = payload.get("epoch_id")
        source_tokens = payload.get("source_tokens")
        through_message_id = payload.get("through_message_id")
        if not isinstance(epoch_id, str):
            return None
        if through_message_id is not None and (
            isinstance(through_message_id, bool)
            or not isinstance(through_message_id, int)
            or through_message_id <= 0
        ):
            return None
        try:
            source_tokens_int = int(source_tokens)
        except (TypeError, ValueError):
            return None
        try:
            return CompressionEpochMetadata(
                epoch_id=epoch_id,
                source_tokens=source_tokens_int,
                through_message_id=through_message_id,
            )
        except ValueError:
            return None
