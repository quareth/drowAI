"""Legacy container helper utilities with backward-compatible shims.

Scope:
- Keep compatibility helpers used by legacy call paths and tests.
- Avoid duplicating logic that now lives under ``backend.services.docker``.
"""

import logging
import os
from typing import Optional, Tuple

from docker.errors import DockerException

logger = logging.getLogger(__name__)

def __getattr__(name: str):
    """Lazily expose deprecated compatibility symbols without import cycles."""
    if name == "check_docker_cli_availability":
        from .docker.client import check_docker_cli_availability as checker
        return checker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def get_cached_api_key(user_id: int) -> Optional[str]:
    """Deprecated decrypted API-key cache lookup.

    Execution Plane routes runtime credential access through provider credential
    services, so decrypted keys are no longer cached process-wide.
    """
    _ = user_id
    return None

def cache_api_key(user_id: int, api_key: Optional[str]) -> None:
    """Deprecated no-op retained for old imports.

    Decrypted provider credentials must not be cached in this module.
    """
    _ = user_id, api_key

def clear_api_key_cache(user_id: int) -> None:
    """Deprecated no-op retained for credential-service invalidation calls."""
    _ = user_id

def get_container_name(task_id: int) -> str:
    """Generate standardized container name for a task."""
    return f"kali-container-{task_id}"

def parse_task_id_from_container(container_name: str) -> Optional[int]:
    """Extract task ID from a container name."""
    prefix = "kali-container-"
    if container_name.startswith(prefix):
        try:
            return int(container_name[len(prefix):])
        except ValueError:
            return None
    return None

def get_agent_source_path() -> str:
    """Dynamically resolve agent directory path with fallbacks"""
    # Method 1: Relative to current file
    current_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
    agent_src = os.path.join(project_root, "agent")
    
    if os.path.exists(agent_src):
        return agent_src
        
    # Method 2: Workspace root fallback
    workspace_root = "/home/runner/workspace"
    agent_src_workspace = os.path.join(workspace_root, "agent")
    if os.path.exists(agent_src_workspace):
        return agent_src_workspace
        
    # Method 3: Current working directory
    cwd_agent = os.path.join(os.getcwd(), "agent")
    if os.path.exists(cwd_agent):
        return cwd_agent
        
    logger.error("Agent directory not found in any expected location")
    return ""

def get_kali_executor_path() -> str:
    """Resolve the kali_executor directory path."""
    current_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
    exec_src = os.path.join(project_root, "kali_executor")

    if os.path.exists(exec_src):
        return exec_src

    workspace_root = "/home/runner/workspace"
    exec_workspace = os.path.join(workspace_root, "kali_executor")
    if os.path.exists(exec_workspace):
        return exec_workspace

    cwd_exec = os.path.join(os.getcwd(), "kali_executor")
    if os.path.exists(cwd_exec):
        return cwd_exec

    logger.error("kali_executor directory not found in any expected location")
    return ""

def get_workspace_path(task_id: int) -> str:
    """Generate task workspace path using WorkspaceConfig"""
    from backend.config.workspace_config import WorkspaceConfig
    return str(WorkspaceConfig.get_task_workspace_path(task_id))

def get_container_status(docker_client, task_id: int) -> str:
    """Return container status or 'not_found'.

    .. deprecated::
        Use ``unified_docker_service.get_container_status(task_id)`` instead.
        This function is retained for backward compatibility only.
    """
    name = get_container_name(task_id)
    try:
        container = docker_client.containers.get(name)
        container.reload()
        return container.status
    except DockerException:
        return "not_found"

def validate_container_exists(docker_client, task_id: int) -> bool:
    """Check if a container exists for the given task.

    .. deprecated::
        Use ``unified_docker_service`` APIs instead.
        This function is retained for backward compatibility only.
    """
    name = get_container_name(task_id)
    try:
        docker_client.containers.get(name)
        return True
    except DockerException:
        return False

def validate_container_state(docker_client, task_id: int, required_state: str) -> bool:
    """Validate that a container is in the required state.

    .. deprecated::
        Use ``unified_docker_service`` APIs instead.
        This function is retained for backward compatibility only.
    """
    name = get_container_name(task_id)
    try:
        container = docker_client.containers.get(name)
        container.reload()
        return container.status == required_state
    except DockerException:
        return False


def execute_container_operation(docker_client, task_id: int, operation: str, **kwargs) -> Tuple[bool, str]:
    """Execute a Docker operation on a container.

    .. deprecated::
        Use ``unified_docker_service`` APIs instead.
        This function is retained for backward compatibility only.
    """
    name = get_container_name(task_id)
    try:
        container = docker_client.containers.get(name)
        op = getattr(container, operation)
        op(**kwargs)
        container.reload()
        return True, container.status
    except DockerException as e:
        return False, str(e)
    except AttributeError:
        return False, f"Invalid operation: {operation}"


def handle_container_timeout(task_id: int, operation: str, timeout: int) -> bool:
    """Log a container operation timeout.

    .. deprecated::
        Use ``unified_docker_service`` APIs instead.
        This function is retained for backward compatibility only.
    """
    logger.error(f"{operation} timed out after {timeout}s for task {task_id}")
    return False


def log_container_operation(task_id: int, operation: str, success: bool, message: str) -> None:
    """Log container operation result.

    .. deprecated::
        Use ``unified_docker_service`` APIs instead.
        This function is retained for backward compatibility only.
    """
    if success:
        logger.info(f"Task {task_id} {operation} succeeded: {message}")
    else:
        logger.error(f"Task {task_id} {operation} failed: {message}")


def create_container_error_response(operation: str, error: Exception) -> dict:
    """Create a standardized error response.

    .. deprecated::
        Use ``unified_docker_service`` APIs instead.
        This function is retained for backward compatibility only.
    """
    return {"operation": operation, "error": str(error)}
