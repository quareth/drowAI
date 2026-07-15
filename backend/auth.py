"""Authentication and JWT token utilities for API and WebSocket paths.

Responsibilities:
- hash/verify user passwords
- issue and validate JWT access tokens
- resolve authenticated users for FastAPI dependency injection
"""

from datetime import timedelta
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
import os
import logging

from backend.config.generated_config import resolve_config_value
from .database import get_db
from .models.core import User
from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES as CONFIG_ACCESS_TOKEN_EXPIRE_MINUTES,
    DEBUG,
)
from .schemas.core import UserResponse
from backend.core.time_utils import utc_now

if TYPE_CHECKING:
    from .schemas.core import UserResponse


class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    user: "UserResponse"


Token.model_rebuild(_types_namespace={"UserResponse": UserResponse})

# Setup logging
logger = logging.getLogger(__name__)

_DEV_JWT_SECRET = "your-super-secret-jwt-key-change-in-production"
_JWT_SECRET_ENV = "JWT_SECRET"


class JWTSecretConfigurationError(RuntimeError):
    """Raised when JWT signing secret configuration is unsafe for the active mode."""


def _resolve_jwt_secret() -> str:
    """Resolve JWT signing secret from ``JWT_SECRET``; fail closed outside debug mode."""
    jwt_secret = (resolve_config_value(_JWT_SECRET_ENV) or os.getenv(_JWT_SECRET_ENV) or "").strip()
    if jwt_secret:
        if not DEBUG and jwt_secret == _DEV_JWT_SECRET:
            raise JWTSecretConfigurationError(
                f"{_JWT_SECRET_ENV} must not use the development default when DEBUG is false."
            )
        return jwt_secret

    if DEBUG:
        logger.warning(
            "%s is not set; using development JWT secret. Set %s for stable local tokens.",
            _JWT_SECRET_ENV,
            _JWT_SECRET_ENV,
        )
        return _DEV_JWT_SECRET

    raise JWTSecretConfigurationError(
        f"{_JWT_SECRET_ENV} is required when DEBUG is false."
    )


# JWT settings (algorithm is an internal constant; not environment-configurable).
JWT_ALGORITHM = "HS256"
SECRET_KEY = _resolve_jwt_secret()
ACCESS_TOKEN_EXPIRE_MINUTES = CONFIG_ACCESS_TOKEN_EXPIRE_MINUTES

# Password hashing with error handling for bcrypt version issues
try:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except Exception as e:
    logger.warning(f"Bcrypt version warning: {e}")
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# HTTP Bearer token
security = HTTPBearer(auto_error=False)


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _is_user_active(user: User) -> bool:
    return bool(getattr(user, "is_active", False))


def decode_token_payload(token: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Decode JWT payload with shared HTTP/WebSocket error codes."""

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        exp = payload.get("exp")
        if exp is None:
            return None, "missing_exp"
        return payload, None
    except ExpiredSignatureError as e:
        logger.error(f"Token verification failed: {e}")
        return None, "token_expired"
    except JWTError as e:
        logger.error(f"Token verification failed: {e}")
        return None, "invalid_token"


def resolve_user_from_token_payload(db: Session, payload: Mapping[str, object]) -> User:
    """Resolve and validate the authenticated user represented by a JWT payload."""

    username = payload.get("sub")
    if username is None or not isinstance(username, str) or not username.strip():
        raise _credentials_exception()

    user = get_user_by_username(db, username=username)
    if user is None:
        raise _credentials_exception()

    token_user_id = payload.get("user_id")
    if token_user_id is not None:
        try:
            parsed_user_id = int(token_user_id)
        except (TypeError, ValueError):
            raise _credentials_exception() from None
        if parsed_user_id <= 0 or parsed_user_id != int(user.id):
            raise _credentials_exception()

    if not _is_user_active(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    return user

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its hash with error handling."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False

def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt with error handling."""
    try:
        return pwd_context.hash(password)
    except Exception as e:
        logger.error(f"Password hashing error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Password hashing failed"
        )

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token with validation."""
    if not data or "sub" not in data:
        raise ValueError("Token data must contain 'sub' field")
    
    try:
        to_encode = data.copy()
        if expires_delta:
            expire = utc_now() + expires_delta
        else:
            expire = utc_now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

        to_encode.update({"exp": expire, "iat": utc_now()})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=JWT_ALGORITHM)
        return encoded_jwt
    except Exception as e:
        logger.error(f"JWT token creation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token creation failed"
        )

def get_user_by_username(db: Session, username: str) -> Optional[User]:
    """Get user by username from database with error handling."""
    try:
        result = db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_user_by_username: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred"
        )

def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    """Authenticate user by username and password with enhanced validation."""
    if not username or not password:
        return None
    
    try:
        user = get_user_by_username(db, username)
        if not user:
            return None
        
        # Check if user is active
        if hasattr(user.is_active, '__bool__'):
            is_active = bool(user.is_active)
        else:
            is_active = user.is_active
        if not is_active:
            return None
            
        if not verify_password(password, str(user.password)):
            return None
            
        return user
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        return None

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Get current user from JWT token with enhanced error handling."""
    credentials_exception = _credentials_exception()
    
    if not credentials:
        raise credentials_exception
    
    payload, error_code = decode_token_payload(credentials.credentials)
    if payload is None:
        if error_code == "token_expired":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise credentials_exception
    
    try:
        user = resolve_user_from_token_payload(db, payload)
        from backend.services.tenant.rls import set_user_lookup_rls_context

        set_user_lookup_rls_context(db, user_id=int(user.id), actor_type="user")
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User retrieval error: {e}")
        raise credentials_exception

def verify_token_with_error(token: str) -> tuple[Optional[dict], Optional[str]]:
    """Verify JWT token and return `(payload, error_code)` for caller decisions.

    Error codes:
    - `missing_exp`: token is structurally valid but lacks `exp`
    - `token_expired`: token signature or exp claim is expired
    - `invalid_token`: any other JWT validation failure
    """
    return decode_token_payload(token)


def extract_active_tenant_hint(payload: Mapping[str, object] | None) -> int | None:
    """Extract a validated active-tenant hint from JWT claims when present."""

    if payload is None:
        return None
    for claim_key in ("active_tenant_id", "tenant_id"):
        claim_value = payload.get(claim_key)
        if claim_value is None:
            continue
        try:
            parsed = int(claim_value)
        except (TypeError, ValueError):
            return None
        if parsed > 0:
            return parsed
        return None
    return None
