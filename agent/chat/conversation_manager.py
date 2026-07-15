"""Conversation Manager

Thread-safe conversation state manager for basic LLM chat.

Design goals
- Thin index/cache: ChatMessage remains source of truth for messages
- Cross-platform locking: lock files under management_state task roots
- Atomic writes: temp + os.replace to avoid corruption
- Management-plane storage: task-scoped state outside runtime workspaces

Notes
- This module does NOT persist individual chat messages. It only manages
  lightweight conversation metadata (ids, titles, active conversation, etc.).
- Message persistence and streaming are handled elsewhere (ChatMessageService, InMemoryStreamHub).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

try:
    import fcntl  # type: ignore
    _HAS_FCNTL = True
except Exception:  # pragma: no cover - on Windows
    fcntl = None
    _HAS_FCNTL = False

logger = logging.getLogger(__name__)


class FileLock:
    """Cross-platform file lock using fcntl on Unix and lock-file on Windows.

    Lock files are created under `management_state/.../locks` and should be unique per index.
    """

    def __init__(self, lock_path: Path, timeout: float = 10.0, poll_interval: float = 0.05) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fh = None  # type: ignore

    def __enter__(self) -> "FileLock":
        start = time.time()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if _HAS_FCNTL:
            # Unix: use advisory file locking
            self._fh = open(self.lock_path, "a+")
            while True:
                try:
                    fcntl.flock(self._fh, fcntl.LOCK_EX)
                    break
                except Exception:
                    if time.time() - start > self.timeout:
                        raise TimeoutError(f"Timeout acquiring lock: {self.lock_path}")
                    time.sleep(self.poll_interval)
        else:
            # Windows: emulate with exclusive file creation
            while True:
                try:
                    # Use exclusive create to acquire
                    self._fh = open(self.lock_path, "x")
                    break
                except FileExistsError:
                    if time.time() - start > self.timeout:
                        raise TimeoutError(f"Timeout acquiring lock: {self.lock_path}")
                    time.sleep(self.poll_interval)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if _HAS_FCNTL and self._fh is not None:
                try:
                    fcntl.flock(self._fh, fcntl.LOCK_UN)
                except Exception:
                    pass
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
        finally:
            # On Windows, removal of the lock file releases the lock.
            try:
                if self.lock_path.exists():
                    self.lock_path.unlink()
            except Exception:
                # Best effort; do not raise on cleanup issues
                pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConversationRecord:
    id: str
    title: Optional[str]
    created_at: str
    updated_at: str
    status: str = "active"  # active | archived
    metadata: Optional[Dict[str, Any]] = None


class ConversationManager:
    """Manage conversation IDs and metadata per task with strong guarantees.

    This manager stores only a compact index file under management-plane state:
    agent/management_state/conversations/task-<id>/conversations/index.json

    Format:
    {
      "active_id": "uuid",
      "order": ["uuid", ...],
      "conversations": {
        "uuid": { ... ConversationRecord ... }
      }
    }
    """

    INDEX_DIR_NAME = "conversations"
    INDEX_FILE_NAME = "index.json"
    LOCK_FILE_NAME = "conversations.lock"

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        project_root = Path(__file__).resolve().parents[2]
        self.state_root: Path = (
            project_root
            / "agent"
            / "management_state"
            / "conversations"
            / f"task-{int(task_id)}"
        )
        self.index_dir: Path = self.state_root / self.INDEX_DIR_NAME
        self.index_file: Path = self.index_dir / self.INDEX_FILE_NAME
        self.lock_file: Path = self.state_root / "locks" / self.LOCK_FILE_NAME
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------- Public API ---------------------------
    def create_conversation(self, title: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Create a new conversation and return its id.

        Thread-safe with cross-platform file locking and atomic write.
        """
        conv_id = str(uuid4())
        now = _utc_now_iso()
        record = ConversationRecord(
            id=conv_id,
            title=title or "Conversation",
            created_at=now,
            updated_at=now,
            status="active",
            metadata=metadata or {},
        )

        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            conversations = index.setdefault("conversations", {})
            order = index.setdefault("order", [])

            conversations[conv_id] = asdict(record)
            order.append(conv_id)
            index["active_id"] = conv_id

            self._write_index_unlocked(index)

        logger.debug("Created conversation %s for task %s", conv_id, self.task_id)
        return conv_id

    def list_conversations(self) -> List[ConversationRecord]:
        """List known conversations for this task."""
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            conversations = index.get("conversations", {})
            # Preserve order if available
            order = index.get("order", list(conversations.keys()))
            result: List[ConversationRecord] = []
            for cid in order:
                data = conversations.get(cid)
                if not data:
                    continue
                try:
                    result.append(ConversationRecord(**data))
                except Exception:
                    # Skip malformed entries
                    continue
            return result

    def get_active_conversation_id(self) -> Optional[str]:
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            return index.get("active_id")

    def set_active_conversation_id(self, conversation_id: str) -> None:
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            conversations = index.get("conversations", {})
            if conversation_id not in conversations:
                raise ValueError(f"Conversation not found: {conversation_id}")
            index["active_id"] = conversation_id
            # Touch updated_at for bookkeeping
            conversations[conversation_id]["updated_at"] = _utc_now_iso()
            self._write_index_unlocked(index)

    def ensure_default_conversation(self) -> str:
        """Return the active conversation id, creating one if none exist."""
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            active = index.get("active_id")
            if active and active in index.get("conversations", {}):
                return active
        # Outside lock, create a new conversation to avoid holding lock long
        return self.create_conversation(title="Conversation")
    # -------------------- OpenAI conversation id helpers --------------------
    def get_openai_conversation_id(self, conversation_id: str) -> Optional[str]:
        """Return the persisted OpenAI conversation id for a local conversation.

        Stored under conversations[conversation_id].metadata['openai_conversation_id']
        """
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            conversations = index.get("conversations", {})
            data = conversations.get(conversation_id)
            if not data:
                return None
            metadata = data.get("metadata") or {}
            ocid = metadata.get("openai_conversation_id")
            return str(ocid) if isinstance(ocid, str) and ocid else None

    def set_openai_conversation_id(self, conversation_id: str, openai_conversation_id: str) -> None:
        """Persist the OpenAI conversation id for a local conversation.

        Creates metadata dict if missing and writes atomically.
        """
        if not openai_conversation_id:
            return
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            conversations = index.setdefault("conversations", {})
            if conversation_id not in conversations:
                raise ValueError(f"Conversation not found: {conversation_id}")
            data = conversations[conversation_id]
            metadata = data.get("metadata") or {}
            metadata["openai_conversation_id"] = openai_conversation_id
            data["metadata"] = metadata
            data["updated_at"] = _utc_now_iso()
            conversations[conversation_id] = data
            self._write_index_unlocked(index)
        # Also mirror in management-plane task state root for simple seeding
        try:
            mirror_path = self.state_root / "conversation.json"
            tmp = mirror_path.with_suffix(".tmp")
            payload = {
                "conversation_id": conversation_id,
                "openai_conversation_id": openai_conversation_id,
                "updated_at": _utc_now_iso(),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, mirror_path)
        except Exception:
            logger.debug("Failed to write conversation mirror file", exc_info=True)

    def reset_openai_conversation(self, conversation_id: Optional[str] = None) -> None:
        """Clear persisted OpenAI conversation id and remove mirror file (best-effort)."""
        with FileLock(self.lock_file):
            index = self._read_index_unlocked()
            conversations = index.get("conversations", {})
            if conversation_id is None:
                # If no id provided, clear for active if any
                conversation_id = index.get("active_id")
            if conversation_id and conversation_id in conversations:
                data = conversations[conversation_id]
                md = data.get("metadata") or {}
                if "openai_conversation_id" in md:
                    try:
                        del md["openai_conversation_id"]
                    except Exception:
                        pass
                    data["metadata"] = md
                    data["updated_at"] = _utc_now_iso()
                    conversations[conversation_id] = data
                    self._write_index_unlocked(index)
        try:
            mirror_path = self.state_root / "conversation.json"
            if mirror_path.exists():
                mirror_path.unlink()
        except Exception:
            logger.debug("Failed to delete conversation mirror file", exc_info=True)

    # ----------------------- Internal helpers -----------------------
    def _read_index_unlocked(self) -> Dict[str, Any]:
        if not self.index_file.exists():
            return {"conversations": {}, "order": [], "active_id": None}
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                raw = f.read().strip()
                if not raw:
                    return {"conversations": {}, "order": [], "active_id": None}
                data = json.loads(raw)
                # Defensive defaults
                data.setdefault("conversations", {})
                data.setdefault("order", [])
                data.setdefault("active_id", None)
                return data
        except json.JSONDecodeError:
            # Corruption fallback: rename broken file and start fresh
            try:
                backup = self.index_file.with_suffix(".corrupt.json")
                os.replace(self.index_file, backup)
                logger.warning("Conversation index corrupted; backed up to %s", backup)
            except Exception:
                logger.debug("Failed to backup corrupted index file", exc_info=True)
            return {"conversations": {}, "order": [], "active_id": None}
        except Exception:
            logger.debug("Failed to read conversation index", exc_info=True)
            return {"conversations": {}, "order": [], "active_id": None}

    def _write_index_unlocked(self, data: Dict[str, Any]) -> None:
        tmp = self.index_file.with_suffix(".tmp")
        self.index_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.index_file)
        except Exception:
            # Best effort cleanup of temp file
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise


__all__ = [
    "ConversationManager",
    "ConversationRecord",
]


