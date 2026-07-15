"""Backend application entrypoint and global WebSocket transport router.

This module wires HTTP routers, application lifecycle hooks, and the `/ws`
multiplex endpoint used by task-scoped realtime channels.
"""

from fastapi import FastAPI, Depends, WebSocket
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
from contextlib import asynccontextmanager

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv not installed, skip
    pass

from .core.logging import configure_backend_logging

configure_backend_logging()

from .database import (
    SessionLocal,
    ensure_reporting_lifecycle_schema_ready,
    ensure_tenant_baseline_schema_ready,
    ensure_runner_control_schema_ready,
)
from .routers import (
    agent_reasoning,
    artifact_provenance,
    auth,
    cve_settings,
    data_management_settings,
    docker_logs,
    engagements_crud,
    engagement_knowledge,
    knowledge,
    health,
    llm,
    network_overview,
    reports,
    reporting,
    retention,
    runner_control,
    settings,
    setup,
    system_metrics,
    tasks,
    tenants,
    usage,
)
from .routers import chat
from .models import User
from .auth import get_current_user
from .config import ALLOWED_ORIGINS, REASONING_WS_MAX_SUBSCRIPTIONS
from .services.platform.background_services import (
    background_service_status,
    start_background_services,
    stop_background_services,
)
from .services.websocket.gateway import (
    authorize_ws_connection,
    authenticate_ws,
    enforce_ws_task_ownership,
    resolve_ws_user_id,
    send_ws_error,
)
from .services.websocket.channel_handlers import (
    serve_agent_multi_websocket as _serve_agent_multi_websocket,
    serve_docker_task_websocket as _serve_docker_task_websocket,
    serve_metrics_task_websocket as _serve_metrics_task_websocket,
    serve_terminal_task_websocket as _serve_terminal_task_websocket,
    serve_vpn_status_task_websocket as _serve_vpn_status_task_websocket,
)
from .services.tenant.bootstrap import bootstrap_default_tenant_state
from .config.deployment_topology import DeploymentProfile, get_deployment_profile_state
from .services.task.local_placement_migration import fail_closed_active_local_placement_tasks
from .services.langgraph_chat.checkpoint.schema_bootstrap import (
    initialize_checkpointer_schema,
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed when deployment profile and runtime selection are unsafe.
    profile_state = get_deployment_profile_state()
    ensure_runner_control_schema_ready()
    ensure_tenant_baseline_schema_ready()
    ensure_reporting_lifecycle_schema_ready()
    bootstrap_default_tenant_state()
    await initialize_checkpointer_schema()

    from backend.services.platform.installation_service import PlatformInstallationService

    if profile_state.profile is not DeploymentProfile.DEV_LOCAL:
        migration_db = SessionLocal()
        try:
            migration_result = fail_closed_active_local_placement_tasks(
                migration_db,
                deployment_profile=profile_state.profile.value,
            )
            migration_db.commit()
            if migration_result.changed_count:
                logger.warning(migration_result.message)
        except Exception as exc:
            migration_db.rollback()
            raise RuntimeError(
                "Failed to reject active local-placement tasks during startup. "
                "Manually mark active tasks with runtime_placement_mode=local as failed "
                "or restore database access before starting this product profile."
            ) from exc
        finally:
            migration_db.close()

    setup_db = SessionLocal()
    try:
        installation_service = PlatformInstallationService(setup_db)
        installation_service.repair_legacy_installation_if_needed()
        setup_db.commit()
        setup_incomplete = installation_service.is_setup_required()
    except Exception:
        setup_db.rollback()
        setup_incomplete = False
    finally:
        setup_db.close()

    if not setup_incomplete:
        await start_background_services()
    else:
        logging.getLogger(__name__).info("Setup wizard pending; deferring background services until installation completes")

    yield

    # Cleanup on shutdown
    await stop_background_services()

# Create FastAPI app
app = FastAPI(
    title="DrowAI Red Team Platform",
    description="Pre-v1 platform for task-isolated AI security workflows",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers without authentication dependencies for WebSocket endpoints
app.include_router(setup.router, tags=["setup"])
app.include_router(auth.router, prefix="/api/auth", tags=["authentication"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])

app.include_router(docker_logs.router, prefix="/api/docker", tags=["docker"])
app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
app.include_router(reporting.router)
app.include_router(settings.router, tags=["settings"])
app.include_router(data_management_settings.router, tags=["settings"])
app.include_router(cve_settings.router, tags=["settings"])
app.include_router(system_metrics.router)
app.include_router(network_overview.router)
app.include_router(retention.router)
app.include_router(agent_reasoning.router, prefix="/api", tags=["agent-reasoning"])
app.include_router(llm.router, tags=["llm"])
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(artifact_provenance.router)
app.include_router(engagement_knowledge.router)
app.include_router(engagements_crud.router)
app.include_router(knowledge.router)
app.include_router(usage.router, tags=["usage"])
app.include_router(runner_control.router)
app.include_router(tenants.router)

# Health check endpoint
@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "message": "DrowAI API is running",
        **background_service_status(),
    }

# Protected route example
@app.get("/api/user")
async def get_user(current_user: User = Depends(get_current_user)):
    return current_user

# Global WebSocket endpoint for frontend connections
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Global WebSocket endpoint matching frontend connections
    """
    try:
        # Query metadata used for channel routing and diagnostics.
        query_params = websocket.query_params
        connection_type = query_params.get("type", "unknown")
        task_id = query_params.get("taskId")
        task_id_value = None
        if task_id is not None:
            try:
                task_id_value = int(task_id)
            except Exception:
                task_id_value = None
        logger.info(
            f"WebSocket params - type: {connection_type}, taskId: {task_id}"
        )
        auth_ctx = await authorize_ws_connection(
            websocket,
            authenticate_func=authenticate_ws,
            resolve_user_id_func=resolve_ws_user_id,
        )
        if auth_ctx is None:
            return
        user_data = auth_ctx.user_data
        user_id = auth_ctx.user_id
        logger.info("WebSocket authentication successful for user_id=%s", user_id)

        # Send immediate confirmation after successful auth.
        await websocket.send_text('{"type":"connection_accepted","connection_type":"' + connection_type + '"}')
             
        # Route to appropriate handler based on type
        if connection_type == "agent-multi":
            await handle_agent_multi_websocket(websocket, user_id)
        elif connection_type == "terminal" and task_id_value is not None:
            await handle_terminal_websocket(websocket, task_id_value, user_data, user_id)
        elif connection_type == "docker" and task_id_value is not None:
            await handle_docker_websocket(websocket, task_id_value, user_data, user_id)
        elif connection_type == "metrics" and task_id_value is not None:
            await handle_metrics_websocket(websocket, task_id_value, user_id)
        elif connection_type == "vpn_status" and task_id_value is not None:
            await handle_vpn_status_websocket(websocket, task_id_value, user_id)
        else:
            await websocket.send_text('{"type":"error","message":"Invalid connection type or missing taskId"}')
            await websocket.close(code=1003, reason="Invalid Request")
             
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await send_ws_error(
                websocket,
                message="Internal server error",
                code="internal_error",
                close_code=1011,
                close_reason="Internal Error",
            )
        except Exception as close_error:
            logger.error(f"Error closing WebSocket: {close_error}")
            pass

async def handle_docker_websocket(websocket, task_id: int, user_data: dict, user_id: int):
    """Compatibility wrapper for docker websocket handler."""
    await _serve_docker_task_websocket(
        websocket,
        task_id,
        user_id=user_id,
        user_sub=user_data.get("sub"),
        ownership_enforcer=enforce_ws_task_ownership,
    )


async def handle_agent_multi_websocket(websocket, user_id: int):
    """Compatibility wrapper for multiplexed agent websocket handler."""
    await _serve_agent_multi_websocket(
        websocket,
        user_id=user_id,
        max_subscriptions=REASONING_WS_MAX_SUBSCRIPTIONS,
        ownership_enforcer=enforce_ws_task_ownership,
    )


async def handle_terminal_websocket(websocket, task_id: int, user_data: dict, user_id: int):
    """Compatibility wrapper for terminal websocket handler."""
    await _serve_terminal_task_websocket(
        websocket,
        task_id,
        user_id=user_id,
        user_sub=user_data.get("sub"),
        include_connection_user=True,
        ownership_enforcer=enforce_ws_task_ownership,
    )


async def handle_metrics_websocket(websocket, task_id: int, user_id: int):
    """Compatibility wrapper for metrics websocket handler."""
    await _serve_metrics_task_websocket(
        websocket,
        task_id,
        user_id=user_id,
        ownership_enforcer=enforce_ws_task_ownership,
    )


async def handle_vpn_status_websocket(websocket, task_id: int, user_id: int):
    """Compatibility wrapper for VPN status websocket handler."""
    await _serve_vpn_status_task_websocket(
        websocket,
        task_id,
        user_id=user_id,
        ownership_enforcer=enforce_ws_task_ownership,
    )

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_config=None,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
