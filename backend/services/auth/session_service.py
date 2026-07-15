"""Refresh-session persistence and cookie policy for MVP auth continuity.

Responsibilities:
- create, rotate, validate, and revoke opaque refresh sessions
- hash refresh tokens before persistence
- centralize refresh-cookie attributes used by auth routes

Boundary:
- no access-JWT issuance, password checks, tenant resolution, or route orchestration
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import secrets

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import DEBUG
from backend.core.time_utils import to_utc, utc_now
from backend.models.core import User, UserSession

REFRESH_COOKIE_NAME = "drowai_refresh_token"
REFRESH_COOKIE_PATH = "/api/auth"
REFRESH_TOKEN_IDLE_TIMEOUT = timedelta(minutes=30)
REFRESH_TOKEN_ABSOLUTE_TIMEOUT = timedelta(days=7)
REFRESH_TOKEN_BYTES = 48


class RefreshSessionError(ValueError):
    """Raised when a refresh session cannot be used to mint a new access token."""


@dataclass(frozen=True)
class RefreshSessionIssue:
    """Result of creating or rotating an opaque refresh session token."""

    user: User
    refresh_token: str
    session: UserSession


def hash_refresh_token(token: str) -> str:
    """Return a stable digest for a high-entropy opaque refresh token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Attach the refresh-token cookie with the canonical MVP policy."""
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=int(REFRESH_TOKEN_ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=not DEBUG,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response: Response) -> None:
    """Clear the refresh-token cookie using the same path/policy as issuance."""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        secure=not DEBUG,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


class RefreshSessionService:
    """Coordinate refresh-session persistence without owning route transactions."""

    def __init__(self, db: Session):
        self._db = db

    def create_session(self, user: User) -> RefreshSessionIssue:
        """Create a new refresh session for an authenticated active user."""
        if not self._is_active_user(user):
            raise RefreshSessionError("User account is inactive")

        refresh_token = self._generate_token()
        now = utc_now()
        session = UserSession(
            user_id=int(user.id),
            refresh_token_hash=hash_refresh_token(refresh_token),
            last_activity_at=now,
            idle_expires_at=now + REFRESH_TOKEN_IDLE_TIMEOUT,
            absolute_expires_at=now + REFRESH_TOKEN_ABSOLUTE_TIMEOUT,
            revoked_at=None,
        )
        self._db.add(session)
        self._db.flush()
        return RefreshSessionIssue(user=user, refresh_token=refresh_token, session=session)

    def refresh_session(self, raw_refresh_token: str | None) -> RefreshSessionIssue:
        """Validate and rotate a refresh session, returning the owning active user."""
        session = self._lookup_session(raw_refresh_token)
        now = utc_now()

        if session.revoked_at is not None:
            raise RefreshSessionError("Refresh session has been revoked")
        if to_utc(session.idle_expires_at) <= now:
            raise RefreshSessionError("Refresh session idle timeout expired")
        if to_utc(session.absolute_expires_at) <= now:
            raise RefreshSessionError("Refresh session absolute timeout expired")

        user = self._db.get(User, int(session.user_id))
        if user is None:
            raise RefreshSessionError("Refresh session user no longer exists")
        if not self._is_active_user(user):
            session.revoked_at = now
            self._db.flush()
            raise RefreshSessionError("User account is inactive")

        next_refresh_token = self._generate_token()
        session.refresh_token_hash = hash_refresh_token(next_refresh_token)
        session.last_activity_at = now
        session.idle_expires_at = min(
            now + REFRESH_TOKEN_IDLE_TIMEOUT,
            to_utc(session.absolute_expires_at),
        )
        self._db.flush()
        return RefreshSessionIssue(
            user=user,
            refresh_token=next_refresh_token,
            session=session,
        )

    def revoke_session(self, raw_refresh_token: str | None) -> bool:
        """Revoke the refresh session represented by a cookie token if it exists."""
        try:
            session = self._lookup_session(raw_refresh_token)
        except RefreshSessionError:
            return False
        if session.revoked_at is not None:
            return False
        session.revoked_at = utc_now()
        self._db.flush()
        return True

    def _lookup_session(self, raw_refresh_token: str | None) -> UserSession:
        token = str(raw_refresh_token or "").strip()
        if len(token) < 32:
            raise RefreshSessionError("Refresh token is missing or malformed")
        token_hash = hash_refresh_token(token)
        session = self._db.execute(
            select(UserSession).where(UserSession.refresh_token_hash == token_hash)
        ).scalar_one_or_none()
        if session is None:
            raise RefreshSessionError("Refresh session was not found")
        return session

    @staticmethod
    def _generate_token() -> str:
        return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)

    @staticmethod
    def _is_active_user(user: User) -> bool:
        return bool(getattr(user, "is_active", False))
