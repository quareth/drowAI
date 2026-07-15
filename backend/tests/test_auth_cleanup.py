"""Regression tests for canonical auth cleanup behavior."""

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend import auth as auth_module
from backend.core.time_utils import utc_now
from backend.database import Base
from backend.models import User, UserSession
from backend.routers import auth as auth_router
from backend.services.auth.session_service import REFRESH_COOKIE_NAME


def _build_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return session_factory()


def _db_dep(db: Session):
    def _dep():
        yield db

    return _dep


def test_get_current_user_rejects_mismatched_user_id_claim() -> None:
    db = _build_db()
    try:
        user = User(username="claim-mismatch-user", password="secret")
        db.add(user)
        db.commit()
        db.refresh(user)

        token = auth_module.create_access_token({"sub": user.username, "user_id": int(user.id) + 1})
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with pytest.raises(HTTPException) as exc_info:
            auth_module.get_current_user(credentials=credentials, db=db)
        assert exc_info.value.status_code == 401
    finally:
        db.close()


def test_get_current_user_rejects_inactive_user_token() -> None:
    db = _build_db()
    try:
        user = User(username="inactive-token-user", password="secret", is_active=False)
        db.add(user)
        db.commit()
        db.refresh(user)

        token = auth_module.create_access_token({"sub": user.username, "user_id": int(user.id)})
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with pytest.raises(HTTPException) as exc_info:
            auth_module.get_current_user(credentials=credentials, db=db)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "User account is inactive"
    finally:
        db.close()


def test_login_rejects_inactive_user_and_sets_refresh_cookie_for_active_user() -> None:
    db = _build_db()
    try:
        active = User(
            username="active-login-user",
            password=auth_module.get_password_hash("secret123"),
            is_active=True,
        )
        inactive = User(
            username="inactive-login-user",
            password=auth_module.get_password_hash("secret123"),
            is_active=False,
        )
        db.add_all([active, inactive])
        db.commit()

        app = FastAPI()
        app.include_router(auth_router.router, prefix="/api/auth")
        app.dependency_overrides[auth_router.get_db] = _db_dep(db)
        client = TestClient(app)

        inactive_response = client.post(
            "/api/auth/login",
            json={"username": inactive.username, "password": "secret123"},
        )
        assert inactive_response.status_code == 401

        active_response = client.post(
            "/api/auth/login",
            json={"username": active.username, "password": "secret123"},
        )
        assert active_response.status_code == 200, active_response.text
        assert "access_token" in active_response.json()
        assert REFRESH_COOKIE_NAME in active_response.cookies
        set_cookie = active_response.headers.get("set-cookie", "")
        assert REFRESH_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Path=/api/auth" in set_cookie

        session = db.execute(
            select(UserSession).where(UserSession.user_id == active.id)
        ).scalar_one()
        assert session.refresh_token_hash
        assert session.revoked_at is None
    finally:
        db.close()


def test_register_sets_refresh_cookie_and_session_row() -> None:
    db = _build_db()
    try:
        app = FastAPI()
        app.include_router(auth_router.router, prefix="/api/auth")
        app.dependency_overrides[auth_router.get_db] = _db_dep(db)
        client = TestClient(app)

        response = client.post(
            "/api/auth/register",
            json={
                "username": "refresh-register-user",
                "password": "secret123",
                "email": "refresh-register-user@example.test",
            },
        )

        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["access_token"]
        assert REFRESH_COOKIE_NAME in response.cookies

        session = db.execute(
            select(UserSession).where(UserSession.user_id == int(payload["user"]["id"]))
        ).scalar_one()
        assert session.refresh_token_hash
        assert session.revoked_at is None
    finally:
        db.close()


def test_refresh_uses_cookie_and_rotates_refresh_session() -> None:
    db = _build_db()
    try:
        user = User(
            username="refresh-cookie-user",
            password=auth_module.get_password_hash("secret123"),
            is_active=True,
        )
        db.add(user)
        db.commit()

        app = FastAPI()
        app.include_router(auth_router.router, prefix="/api/auth")
        app.dependency_overrides[auth_router.get_db] = _db_dep(db)
        client = TestClient(app)

        login_response = client.post(
            "/api/auth/login",
            json={"username": user.username, "password": "secret123"},
        )
        assert login_response.status_code == 200, login_response.text
        first_cookie = login_response.cookies.get(REFRESH_COOKIE_NAME)
        assert first_cookie
        session = db.execute(
            select(UserSession).where(UserSession.user_id == user.id)
        ).scalar_one()
        first_hash = session.refresh_token_hash

        refresh_response = client.post(
            "/api/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: first_cookie},
        )

        assert refresh_response.status_code == 200, refresh_response.text
        assert refresh_response.json()["access_token"]
        rotated_cookie = refresh_response.cookies.get(REFRESH_COOKIE_NAME)
        assert rotated_cookie
        assert rotated_cookie != first_cookie
        db.refresh(session)
        assert session.refresh_token_hash != first_hash
    finally:
        db.close()


def test_refresh_rejects_idle_expired_revoked_missing_and_inactive_sessions() -> None:
    db = _build_db()
    try:
        active = User(
            username="refresh-failure-user",
            password=auth_module.get_password_hash("secret123"),
            is_active=True,
        )
        inactive = User(
            username="refresh-inactive-user",
            password=auth_module.get_password_hash("secret123"),
            is_active=True,
        )
        db.add_all([active, inactive])
        db.commit()

        app = FastAPI()
        app.include_router(auth_router.router, prefix="/api/auth")
        app.dependency_overrides[auth_router.get_db] = _db_dep(db)
        client = TestClient(app)

        missing_response = client.post("/api/auth/refresh")
        assert missing_response.status_code == 401
        assert REFRESH_COOKIE_NAME in missing_response.headers.get("set-cookie", "")

        expired_login = client.post(
            "/api/auth/login",
            json={"username": active.username, "password": "secret123"},
        )
        expired_cookie = expired_login.cookies.get(REFRESH_COOKIE_NAME)
        expired_session = db.execute(
            select(UserSession).where(UserSession.user_id == active.id)
        ).scalar_one()
        expired_session.idle_expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
        expired_response = client.post(
            "/api/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: expired_cookie},
        )
        assert expired_response.status_code == 401

        revoked_login = client.post(
            "/api/auth/login",
            json={"username": active.username, "password": "secret123"},
        )
        revoked_cookie = revoked_login.cookies.get(REFRESH_COOKIE_NAME)
        revoked_session = db.execute(
            select(UserSession)
            .where(UserSession.user_id == active.id)
            .order_by(UserSession.id.desc())
            .limit(1)
        ).scalar_one()
        revoked_session.revoked_at = utc_now()
        db.commit()
        revoked_response = client.post(
            "/api/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: revoked_cookie},
        )
        assert revoked_response.status_code == 401

        inactive_login = client.post(
            "/api/auth/login",
            json={"username": inactive.username, "password": "secret123"},
        )
        inactive_cookie = inactive_login.cookies.get(REFRESH_COOKIE_NAME)
        inactive.is_active = False
        db.commit()
        inactive_response = client.post(
            "/api/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: inactive_cookie},
        )
        assert inactive_response.status_code == 401
    finally:
        db.close()


def test_logout_revokes_refresh_session_and_clears_cookie() -> None:
    db = _build_db()
    try:
        user = User(
            username="logout-refresh-user",
            password=auth_module.get_password_hash("secret123"),
            is_active=True,
        )
        db.add(user)
        db.commit()

        app = FastAPI()
        app.include_router(auth_router.router, prefix="/api/auth")
        app.dependency_overrides[auth_router.get_db] = _db_dep(db)
        client = TestClient(app)

        login_response = client.post(
            "/api/auth/login",
            json={"username": user.username, "password": "secret123"},
        )
        refresh_cookie = login_response.cookies.get(REFRESH_COOKIE_NAME)
        session = db.execute(
            select(UserSession).where(UserSession.user_id == user.id)
        ).scalar_one()

        logout_response = client.post(
            "/api/auth/logout",
            cookies={REFRESH_COOKIE_NAME: refresh_cookie},
        )

        assert logout_response.status_code == 200, logout_response.text
        assert REFRESH_COOKIE_NAME in logout_response.headers.get("set-cookie", "")
        db.refresh(session)
        assert session.revoked_at is not None
    finally:
        db.close()
