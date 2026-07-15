"""
Shared backend test environment configuration.

This module enforces a sqlite test database by default and initializes schema
for backend test suites that import the production FastAPI app.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

_DEFAULT_SQLITE_PATH = Path("backend_test.sqlite3").resolve()
_DEFAULT_SQLITE_URL = f"sqlite:///{_DEFAULT_SQLITE_PATH.as_posix()}"
_TEST_DATABASE_URL = os.getenv("BACKEND_TEST_DATABASE_URL", _DEFAULT_SQLITE_URL)
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET", "backend-test-jwt-secret")

from backend.database import engine
from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base


@pytest.fixture(scope="session", autouse=True)
def _init_backend_test_db() -> Iterator[None]:
    """Create schema once for backend tests and clean up sqlite fixture db."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if _TEST_DATABASE_URL == _DEFAULT_SQLITE_URL and _DEFAULT_SQLITE_PATH.exists():
        try:
            _DEFAULT_SQLITE_PATH.unlink()
        except PermissionError:
            # Best-effort cleanup on Windows where a late-close handle can linger.
            pass


@pytest.fixture(autouse=True)
def _isolate_durable_knowledge_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent backend tests from writing durable evidence files into the repository tree."""
    durable_root = tmp_path / "agent" / "durable_knowledge"
    durable_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_durable_knowledge_base_path",
        staticmethod(lambda: durable_root),
    )
