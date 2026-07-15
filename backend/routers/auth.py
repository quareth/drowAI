"""Authentication routes for user session lifecycle and profile surfaces.

Responsibilities:
- Expose register/login/logout/password-change APIs.
- Return authenticated profile data enriched with tenant context metadata.
"""

from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..database import get_db
from ..auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    Token,
    authenticate_user,
    create_access_token,
    get_current_user,
    get_password_hash,
    security,
)
from ..models import User
from ..schemas import AuthMeResponse, PasswordChangeRequest, UserCreate, UserLogin, UserResponse
from ..services.tenant.context import TenantContextResolutionError, TenantContextService
from ..services.tenant.dependencies import (
    ACTIVE_TENANT_HEADER,
    map_tenant_context_error,
    resolve_tenant_context_for_request,
)
from ..services.auth.session_service import (
    REFRESH_COOKIE_NAME,
    RefreshSessionError,
    RefreshSessionService,
    clear_refresh_cookie,
    set_refresh_cookie,
)

router = APIRouter()


def _build_token_payload(user: User) -> dict:
    """Build the unchanged access-token API payload for an authenticated user."""
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "user_id": user.id},
        expires_delta=access_token_expires,
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": UserResponse.model_validate(user),
    }


def _refresh_session_error_response() -> JSONResponse:
    """Build a 401 refresh response that also clears the refresh cookie."""
    error_response = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Refresh session is invalid or expired"},
        headers={"WWW-Authenticate": "Bearer"},
    )
    clear_refresh_cookie(error_response)
    return error_response


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register_user(
    user_data: UserCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    """Register a new user account."""
    
    # Check if username already exists
    result = db.execute(select(User).where(User.username == user_data.username))
    existing_user = result.scalar_one_or_none()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already registered"
        )
    
    # Check if email already exists (if provided)
    if user_data.email:
        result = db.execute(select(User).where(User.email == user_data.email))
        existing_email = result.scalar_one_or_none()
        
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered"
            )
    
    # Validate password length
    if len(user_data.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters long"
        )
    
    # Create new user
    hashed_password = get_password_hash(user_data.password)
    db_user = User(
        username=user_data.username,
        password=hashed_password,
        email=user_data.email
    )
    
    db.add(db_user)
    db.flush()
    TenantContextService(db).ensure_default_membership(user_id=int(db_user.id))
    refresh_issue = RefreshSessionService(db).create_session(db_user)
    db.commit()
    db.refresh(db_user)
    set_refresh_cookie(response, refresh_issue.refresh_token)

    return _build_token_payload(db_user)

@router.post("/login", response_model=Token)
def login_user(
    login_data: UserLogin,
    response: Response,
    db: Session = Depends(get_db),
):
    """Authenticate user and receive access token."""
    
    user = authenticate_user(db, login_data.username, login_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    refresh_issue = RefreshSessionService(db).create_session(user)
    db.commit()
    set_refresh_cookie(response, refresh_issue.refresh_token)

    return _build_token_payload(user)

@router.get("/me", response_model=AuthMeResponse)
def get_current_user_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_tenant_header: str | None = Header(default=None, alias=ACTIVE_TENANT_HEADER),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Get current profile plus additive tenant context and permissions."""
    service = TenantContextService(db)
    try:
        context = resolve_tenant_context_for_request(
            tenant_context_service=service,
            current_user=current_user,
            header_tenant_id=active_tenant_header,
            credentials=credentials,
            allow_ambiguous=True,
        )
    except TenantContextResolutionError as exc:
        raise map_tenant_context_error(exc) from exc
    memberships = service.list_membership_summaries_for_user(user_id=int(current_user.id))
    permissions = service.build_effective_permissions(context)

    return AuthMeResponse(
        **UserResponse.model_validate(current_user).model_dump(),
        active_tenant=(
            {
                "tenant_id": int(context.tenant_id),
                "membership_id": int(context.membership_id),
                "role": str(context.role),
                "is_default_tenant": bool(context.is_default_tenant),
                "source": str(context.source),
            }
            if context is not None
            else None
        ),
        membership_summaries=[
            {
                "membership_id": int(membership.membership_id),
                "tenant_id": int(membership.tenant_id),
                "tenant_slug": str(membership.tenant_slug),
                "tenant_name": str(membership.tenant_name),
                "role": str(membership.role),
                "membership_status": str(membership.membership_status),
                "tenant_status": str(membership.tenant_status),
                "is_default_tenant": bool(membership.is_default_tenant),
            }
            for membership in memberships
        ],
        effective_permissions=(
            {
                "actions": list(permissions.actions),
                "role": str(permissions.role),
                "tenant_id": int(permissions.tenant_id),
                "policy_version": str(permissions.policy_version),
            }
            if permissions is not None
            else None
        ),
    )

@router.post("/logout")
def logout_user(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: Session = Depends(get_db),
):
    """Logout current user (stateless JWT)."""
    RefreshSessionService(db).revoke_session(refresh_cookie)
    db.commit()
    clear_refresh_cookie(response)
    return {"message": "Successfully logged out"}

@router.post("/refresh", response_model=Token)
def refresh_token(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: Session = Depends(get_db),
):
    """Refresh access token from an HttpOnly refresh-session cookie."""
    try:
        refresh_issue = RefreshSessionService(db).refresh_session(refresh_cookie)
    except RefreshSessionError:
        db.commit()
        return _refresh_session_error_response()

    db.commit()
    set_refresh_cookie(response, refresh_issue.refresh_token)
    return _build_token_payload(refresh_issue.user)

@router.post("/change-password")
def change_password(
    request: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Change user password."""
    
    # Verify old password
    user = authenticate_user(db, current_user.username, request.old_password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect current password"
        )
    
    # Validate new password length
    if len(request.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters long"
        )
    
    # Update password using SQL update
    from sqlalchemy import update
    db.execute(
        update(User).where(User.id == current_user.id).values(password=get_password_hash(request.new_password))
    )
    db.commit()
    
    return {"message": "Password changed successfully"}
