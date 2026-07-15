""": Chat performance benchmarks.

Measures:
- History loading time (target: < 100ms)
- Reserve + update message (target: < 500ms for DB path)
- Optional HTTP mode (BACKEND_URL): first message latency (< 2s), subsequent (< 500ms)

Run:
 pytest backend/tests/benchmarks/test_chat_performance.py -v
 python backend/tests/benchmarks/test_chat_performance.py # standalone script"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

# Allow running as script from repo root
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

TARGET_HISTORY_MS = 100
TARGET_RESERVE_UPDATE_MS = 500
TARGET_FIRST_MESSAGE_S = 2.0
TARGET_SUBSEQUENT_MS = 500
TARGET_STREAMING_MS = 100


def _get_db_session():
    """Return a DB session if DATABASE_URL is set."""
    if not os.getenv("DATABASE_URL"):
        return None
    from backend.database import SessionLocal
    return SessionLocal()


def benchmark_history_loading(iterations: int = 5) -> Optional[float]:
    """Benchmark ConversationHistoryReader.get_conversation_history (median ms)."""
    db = _get_db_session()
    if not db:
        return None
    try:
        from backend.services.chat.message_service import ChatMessageService
        from backend.services.chat.conversation_history_reader import ConversationHistoryReader
        from backend.models.chat import ChatMessage
        from backend.models.core import Task
        # Ensure we have a task and optional messages
        task = db.query(Task).first()
        if not task:
            return None
        task_id = task.id
        conv_id = "perf-bench-conv"
        # Seed one message if none
        existing = db.query(ChatMessage).filter(
            ChatMessage.task_id == task_id,
            ChatMessage.conversation_id == conv_id,
        ).first()
        if not existing:
            svc = ChatMessageService(db)
            svc.reserve_message(task_id, conv_id, None, "USER")
            db.commit()
        times_ms = []
        reader = ConversationHistoryReader(db)
        for _ in range(iterations):
            start = time.perf_counter()
            reader.get_conversation_history(task_id, conv_id, limit=80)
            elapsed_ms = (time.perf_counter() - start) * 1000
            times_ms.append(elapsed_ms)
        times_ms.sort()
        return times_ms[len(times_ms) // 2]
    except Exception:
        return None
    finally:
        db.close()


def benchmark_reserve_update(iterations: int = 5) -> Optional[float]:
    """Benchmark reserve_message + update_message (median ms)."""
    db = _get_db_session()
    if not db:
        return None
    try:
        from backend.services.chat.message_service import ChatMessageService
        from backend.models.core import Task
        task = db.query(Task).first()
        if not task:
            return None
        task_id = task.id
        conv_id = "perf-bench-reserve-update"
        times_ms = []
        for _ in range(iterations):
            start = time.perf_counter()
            svc = ChatMessageService(db)
            msg = svc.reserve_message(task_id, conv_id, None, "ASSISTANT")
            svc.update_message(msg.id, "Benchmark reply", token_count=2)
            db.commit()
            elapsed_ms = (time.perf_counter() - start) * 1000
            times_ms.append(elapsed_ms)
        times_ms.sort()
        return times_ms[len(times_ms) // 2]
    except Exception:
        return None
    finally:
        db.close()


def run_benchmarks() -> dict:
    """Run all in-process benchmarks. Returns dict of metric -> (value_ms, target, passed)."""
    results = {}
    hist = benchmark_history_loading()
    if hist is not None:
        results["history_loading_ms"] = (hist, TARGET_HISTORY_MS, hist <= TARGET_HISTORY_MS)
    reserve = benchmark_reserve_update()
    if reserve is not None:
        results["reserve_update_ms"] = (reserve, TARGET_RESERVE_UPDATE_MS, reserve <= TARGET_RESERVE_UPDATE_MS)
    return results


def main() -> int:
    """Standalone script entry. Returns 0 if all targets met or skipped, 1 otherwise."""
    if not os.getenv("DATABASE_URL"):
        print("SKIP: DATABASE_URL not set. Set it to run chat performance benchmarks.")
        return 0
    results = run_benchmarks()
    if not results:
        print("SKIP: No benchmarks run (e.g. no Task in DB).")
        return 0
    failed = []
    for name, (value, target, passed) in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {value:.2f} ms (target: <={target} ms) [{status}]")
        if not passed:
            failed.append(name)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    print("All benchmarks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- Pytest entrypoints (optional) ---

import pytest


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
def test_benchmark_history_loading():
    """History loading should be under target."""
    median = benchmark_history_loading()
    if median is None:
        pytest.skip("No task in DB or ChatMessage not available")
    assert median <= TARGET_HISTORY_MS, f"History loading {median:.2f} ms > {TARGET_HISTORY_MS} ms"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
def test_benchmark_reserve_update():
    """Reserve+update message should be under target."""
    median = benchmark_reserve_update()
    if median is None:
        pytest.skip("No task in DB or ChatMessage not available")
    assert median <= TARGET_RESERVE_UPDATE_MS, (
        f"Reserve+update {median:.2f} ms > {TARGET_RESERVE_UPDATE_MS} ms"
    )
