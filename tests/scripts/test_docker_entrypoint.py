"""Regression tests for backend container migration startup behavior."""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO_ROOT / "backend/scripts/docker-entrypoint.sh"
_VERSIONS_DIR = _REPO_ROOT / "backend/migrations/versions"


def test_backend_entrypoint_runs_alembic_only() -> None:
    content = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "alembic upgrade head" in content
    assert "init_db" not in content
    assert "create_all" not in content
    assert "alembic stamp" not in content
    assert "CREATE_ALL_BOOTSTRAP" not in content
    assert "CREATE_ALL_BASE_REVISION" not in content


def test_backend_entrypoint_lets_alembic_fail_normally() -> None:
    content = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "Tables exist but alembic_version is missing" not in content
    assert "Refusing to stamp head" not in content


def test_active_migrations_do_not_reference_create_all_bootstrap() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(_VERSIONS_DIR.glob("*.py"))
    )

    assert "created by create_all first" not in content
    assert "create_all bootstrap" not in content
    assert "stamp baseline" not in content
