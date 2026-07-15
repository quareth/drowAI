"""Shared WebSocket gateway helpers for auth, identity, task access, and error handling.

Scope:
- Token extraction, verification, and identity resolution for all WS endpoints.
- User-owned task access enforcement with deterministic deny responses.
- Sanitized error and close-code helpers.

Boundary:
- No channel-specific logic (no docker logs, no terminal, no agent reasoning).
- No connection tracking or streaming. Those belong in channel handlers and managers.
"""

from dataclasses import dataclass
import json
import logging
from typing import Any, Awaitable, Callable, Sequence

from fastapi import WebSocket

from backend.auth import extract_active_tenant_hint, resolve_user_from_token_payload, verify_token_with_error
from backend.database import SessionLocal
from backend.models.core import Task
from backend.services.task.access_service import get_owned_task
from backend.services.tenant.authorization import ACTION_STREAM_SUBSCRIBE, decide_action
from backend.services.tenant.context import (
    TenantContextResolutionError,
    TenantContextService,
    TenantRequestContext,
)
from backend.services.tenant.rls import (
    clear_rls_session_context,
    set_tenant_rls_context,
    set_user_lookup_rls_context,
)

logger = logging.getLogger("backend.services.ws_gateway")


@dataclass(frozen=True)
class WSAuthContext:
    """Authenticated websocket request context shared by websocket routes."""

    token: str
    selected_protocol: str | None
    user_id: int
    user_data: dict[str, Any]
    tenant_context: TenantRequestContext


class WSTenantContextError(ValueError):
    """Raised when websocket tenant context cannot be resolved safely."""

    def __init__(self, *, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


async def extract_ws_token(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Extract JWT from websocket bearer subprotocol only."""
    protocols_header = websocket.headers.get("sec-websocket-protocol", "")
    token: str | None = None
    selected_protocol: str | None = None

    for proto in protocols_header.split(","):
        proto = proto.strip()
        if proto.startswith("Bearer."):
            token = proto[7:]
            selected_protocol = proto
            break

    return token, selected_protocol


def _parse_positive_tenant_id(raw_value: object, *, source: str) -> int:
    try:
        tenant_id = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise WSTenantContextError(
            code="invalid_tenant_hint",
            message=f"Invalid tenant hint from {source}.",
        ) from exc
    if tenant_id <= 0:
        raise WSTenantContextError(
            code="invalid_tenant_hint",
            message=f"Invalid tenant hint from {source}.",
        )
    return tenant_id


def _extract_tenant_id_from_subprotocols(protocols_header: str) -> int | None:
    tenant_ids: list[int] = []
    for proto in protocols_header.split(","):
        token = proto.strip()
        if not token:
            continue
        if not token.lower().startswith("tenant."):
            continue
        tenant_ids.append(_parse_positive_tenant_id(token[7:], source="subprotocol"))

    if not tenant_ids:
        return None
    if len(set(tenant_ids)) > 1:
        raise WSTenantContextError(
            code="invalid_tenant_hint",
            message="Conflicting websocket tenant hints.",
        )
    return tenant_ids[0]


def _extract_tenant_id_from_query(websocket: WebSocket) -> int | None:
    for key in ("active_tenant_id", "tenant_id", "activeTenantId", "tenantId"):
        value = websocket.query_params.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if not normalized:
            continue
        return _parse_positive_tenant_id(normalized, source=f"query:{key}")
    return None


def _close_ws_session(db) -> None:
    """Clear tenant RLS context before returning websocket sessions to the pool."""
    try:
        clear_rls_session_context(db)
    finally:
        db.close()


def resolve_ws_tenant_context(
    websocket: WebSocket,
    *,
    user_id: int,
    user_data: dict[str, Any],
) -> TenantRequestContext:
    """Resolve strict websocket tenant context from hints and token payload."""
    protocols_header = websocket.headers.get("sec-websocket-protocol", "")
    query_tenant_id = _extract_tenant_id_from_query(websocket)
    protocol_tenant_id = _extract_tenant_id_from_subprotocols(protocols_header)

    requested_tenant_id = query_tenant_id if query_tenant_id is not None else protocol_tenant_id
    requested_source = "query" if query_tenant_id is not None else "subprotocol"
    if (
        query_tenant_id is not None
        and protocol_tenant_id is not None
        and int(query_tenant_id) != int(protocol_tenant_id)
    ):
        raise WSTenantContextError(
            code="invalid_tenant_hint",
            message="Conflicting websocket tenant hints.",
        )

    preferred_tenant_id = None
    if requested_tenant_id is None:
        preferred_tenant_id = extract_active_tenant_hint(user_data)

    db = SessionLocal()
    try:
        set_user_lookup_rls_context(db, user_id=int(user_id), actor_type="user")
        tenant_context = TenantContextService(db).resolve_for_user(
            user_id=int(user_id),
            requested_tenant_id=requested_tenant_id,
            requested_source=requested_source,
            preferred_tenant_id=preferred_tenant_id,
            allow_ambiguous=False,
        )
        set_tenant_rls_context(
            db,
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(user_id),
            actor_type="user",
        )
        return tenant_context
    except TenantContextResolutionError as exc:
        raise WSTenantContextError(code=exc.code, message=str(exc)) from exc
    finally:
        try:
            _close_ws_session(db)
        except Exception:
            pass


def _bind_ws_tenant_context(websocket: WebSocket, tenant_context: TenantRequestContext) -> None:
    """Bind resolved tenant context to websocket state for later authorization checks."""
    if getattr(websocket, "state", None) is not None:
        setattr(websocket.state, "tenant_context", tenant_context)
    setattr(websocket, "_tenant_context", tenant_context)


def _get_bound_ws_tenant_context(websocket: WebSocket) -> TenantRequestContext | None:
    state = getattr(websocket, "state", None)
    if state is not None:
        context = getattr(state, "tenant_context", None)
        if isinstance(context, TenantRequestContext):
            return context
    direct_context = getattr(websocket, "_tenant_context", None)
    if isinstance(direct_context, TenantRequestContext):
        return direct_context
    return None


async def accept_ws(
    websocket: WebSocket,
    *,
    selected_protocol: str | None,
    accept_headers: Sequence[tuple[bytes, bytes]] | None = None,
) -> None:
    """Accept websocket with optional subprotocol and extra handshake headers."""
    headers = list(accept_headers) if accept_headers else None
    if selected_protocol:
        if headers is not None:
            await websocket.accept(subprotocol=selected_protocol, headers=headers)
        else:
            await websocket.accept(subprotocol=selected_protocol)
    else:
        if headers is not None:
            await websocket.accept(headers=headers)
        else:
            await websocket.accept()


async def authenticate_ws(token: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Verify a websocket token and return canonical `(payload, error_code)`."""
    if not token:
        return None, "missing_token"

    user_data, error_code = verify_token_with_error(token)
    if not user_data:
        return None, error_code or "invalid_token"

    db = SessionLocal()
    try:
        user = resolve_user_from_token_payload(db, user_data)
        canonical_payload = dict(user_data)
        canonical_payload["sub"] = str(user.username)
        canonical_payload["user_id"] = int(user.id)
        return canonical_payload, None
    except Exception as exc:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException) and exc.status_code == 403:
            return None, "inactive_user"
        return None, "unauthorized_identity"
    finally:
        try:
            _close_ws_session(db)
        except Exception:
            pass


async def authorize_ws_connection(
    websocket: WebSocket,
    *,
    accept_headers: Sequence[tuple[bytes, bytes]] | None = None,
    authenticate_func: Callable[[str | None], Awaitable[tuple[dict[str, Any] | None, str | None]]] | None = None,
    resolve_user_id_func: Callable[[dict[str, Any]], int | None] | None = None,
    resolve_tenant_context_func: Callable[..., TenantRequestContext] | None = None,
) -> WSAuthContext | None:
    """Authenticate an accepted websocket and return its request context.

    This function owns the shared pipeline:
    1) token extraction
    2) websocket accept
    3) token verification
    4) identity resolution
    5) structured auth errors
    """
    token, selected_protocol = await extract_ws_token(websocket)
    await accept_ws(
        websocket,
        selected_protocol=selected_protocol,
        accept_headers=accept_headers,
    )

    auth_handler = authenticate_func or authenticate_ws
    user_data, auth_error_code = await auth_handler(token)
    if not user_data:
        await send_ws_auth_error(
            websocket,
            message="Invalid authentication token" if token else "Authentication token required",
            code=auth_error_code or "invalid_token",
        )
        return None

    user_id_resolver = resolve_user_id_func or resolve_ws_user_id
    user_id = user_id_resolver(user_data)
    if user_id is None:
        await send_ws_auth_error(
            websocket,
            message="Unauthorized websocket identity",
            code="unauthorized_identity",
        )
        return None

    tenant_context_resolver = resolve_tenant_context_func or resolve_ws_tenant_context
    try:
        tenant_context = tenant_context_resolver(
            websocket,
            user_id=int(user_id),
            user_data=user_data,
        )
    except WSTenantContextError as exc:
        await send_ws_auth_error(
            websocket,
            message=str(exc),
            code=exc.code,
            close_reason="Policy violation",
        )
        return None

    _bind_ws_tenant_context(websocket, tenant_context)

    return WSAuthContext(
        token=token or "",
        selected_protocol=selected_protocol,
        user_id=user_id,
        user_data=user_data,
        tenant_context=tenant_context,
    )


def resolve_ws_user_id(user_data: dict[str, Any]) -> int | None:
    """Resolve numeric user id from a verified canonical websocket JWT payload."""
    if not isinstance(user_data, dict):
        return None

    raw_user_id = user_data.get("user_id")
    try:
        if raw_user_id is not None:
            user_id = int(raw_user_id)
            if user_id > 0:
                return user_id
    except Exception:
        pass

    return None


def get_ws_task_in_tenant(
    task_id: int,
    *,
    tenant_id: int | None = None,
    user_id: int | None = None,
) -> Task | None:
    """Return a user-owned task in the active tenant for websocket authorization checks."""
    if tenant_id is None or user_id is None:
        return None
    db = SessionLocal()
    try:
        if user_id is not None:
            set_tenant_rls_context(
                db,
                tenant_id=int(tenant_id),
                user_id=int(user_id),
                actor_type="user",
            )
        return get_owned_task(
            db=db,
            task_id=task_id,
            user_id=int(user_id),
            tenant_id=int(tenant_id),
        )
    finally:
        try:
            _close_ws_session(db)
        except Exception:
            pass


def is_ws_task_in_tenant(task_id: int, *, tenant_id: int | None = None, user_id: int | None = None) -> bool:
    """Return True when the task exists under the resolved tenant/user boundary."""
    return get_ws_task_in_tenant(task_id, tenant_id=tenant_id, user_id=user_id) is not None


def get_ws_task_for_bound_tenant(websocket: WebSocket, *, task_id: int, user_id: int) -> Task | None:
    """Return a user-owned task using the websocket's resolved tenant context."""
    tenant_context = _get_bound_ws_tenant_context(websocket)
    if tenant_context is None:
        return None
    return get_ws_task_in_tenant(
        task_id=task_id,
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(user_id),
    )


async def send_forbidden_task(
    websocket: WebSocket,
    task_id: int,
    *,
    close_connection: bool,
) -> None:
    """Emit deterministic forbidden_task payload and optionally close the socket."""
    try:
        await websocket.send_text(json.dumps({"type": "error", "message": "forbidden_task", "taskId": task_id}))
    except Exception:
        pass
    if close_connection:
        try:
            await websocket.close(code=1008, reason="Forbidden")
        except Exception:
            pass


async def send_ws_auth_error(
    websocket: WebSocket,
    *,
    message: str,
    code: str,
    close_code: int = 1008,
    close_reason: str = "Unauthorized",
) -> None:
    """Send a structured websocket auth error and close with policy violation."""
    try:
        await websocket.send_text(json.dumps({"type": "error", "message": message, "code": code}))
    except Exception:
        pass
    try:
        await websocket.close(code=close_code, reason=close_reason)
    except Exception:
        pass


async def send_ws_error(
    websocket: WebSocket,
    *,
    message: str,
    code: str,
    close_code: int = 1011,
    close_reason: str = "Internal Error",
) -> None:
    """Send a structured websocket error payload and close."""
    try:
        await websocket.send_text(json.dumps({"type": "error", "message": message, "code": code}))
    except Exception:
        pass
    try:
        await websocket.close(code=close_code, reason=close_reason)
    except Exception:
        pass


async def enforce_ws_task_ownership(
    websocket: WebSocket,
    *,
    connection_type: str,
    task_id: int,
    user_id: int,
    close_on_forbidden: bool,
    action: str = ACTION_STREAM_SUBSCRIBE,
) -> bool:
    """Authorize websocket task access and emit deterministic deny response."""
    tenant_context = _get_bound_ws_tenant_context(websocket)
    if tenant_context is None:
        logger.warning(
            "ws task access denied: type=%s user_id=%s task=%s reason=tenant_context_missing",
            connection_type,
            user_id,
            task_id,
        )
        await send_ws_auth_error(
            websocket,
            message="Tenant context is required for websocket subscriptions.",
            code="tenant_context_missing",
            close_reason="Policy violation",
        )
        return False

    action_decision = decide_action(role=tenant_context.role, action=action)
    if not action_decision.allowed:
        logger.warning(
            "ws task access denied: type=%s user_id=%s task=%s tenant_id=%s reason=%s action=%s role=%s",
            connection_type,
            user_id,
            task_id,
            tenant_context.tenant_id,
            action_decision.reason,
            action,
            tenant_context.role,
        )
        await send_ws_auth_error(
            websocket,
            message="Tenant policy denied websocket stream action.",
            code="stream_action_forbidden",
            close_reason="Policy violation",
        )
        return False

    if is_ws_task_in_tenant(
        task_id=task_id,
        tenant_id=tenant_context.tenant_id,
        user_id=int(user_id),
    ):
        return True
    logger.warning(
        "ws task access denied: type=%s user_id=%s tenant_id=%s task=%s reason=forbidden_task",
        connection_type,
        user_id,
        tenant_context.tenant_id,
        task_id,
    )
    await send_forbidden_task(websocket, task_id, close_connection=close_on_forbidden)
    return False
