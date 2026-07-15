"""Shared session and identifier normalization for reporting repositories.

This module owns only database-session storage and UUID/memo-ID normalization;
it contains no ORM queries or concrete repository dependencies.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session


class ReportingRepositoryBase:
    """Store shared reporting repository state and normalize memo identifiers."""

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _parse_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @classmethod
    def normalize_selected_memo_ids(
        cls,
        selected_task_memo_ids: Sequence[str | uuid.UUID],
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Return unique selected memo IDs and duplicates in first-seen order."""

        normalized_memo_ids: list[uuid.UUID] = []
        duplicate_memo_ids: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        duplicate_seen: set[uuid.UUID] = set()
        for memo_id in selected_task_memo_ids:
            parsed_memo_id = cls._parse_uuid(memo_id)
            if parsed_memo_id is None:
                continue
            if parsed_memo_id in seen:
                if parsed_memo_id not in duplicate_seen:
                    duplicate_memo_ids.append(parsed_memo_id)
                    duplicate_seen.add(parsed_memo_id)
                continue
            seen.add(parsed_memo_id)
            normalized_memo_ids.append(parsed_memo_id)
        return normalized_memo_ids, duplicate_memo_ids

    @classmethod
    def _canonical_memo_id_strings(
        cls,
        selected_task_memo_ids: Sequence[str | uuid.UUID],
    ) -> list[str]:
        normalized_memo_ids, _ = cls.normalize_selected_memo_ids(selected_task_memo_ids)
        return sorted(str(memo_id) for memo_id in normalized_memo_ids)
