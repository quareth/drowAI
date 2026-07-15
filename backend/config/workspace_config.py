"""
Workspace Configuration Management
Provides robust, environment-agnostic workspace path resolution
"""

import os
import warnings
from pathlib import Path

from runtime_shared.file_comm_contracts import (
    LOCKS_DIRECTORY_NAME,
    STANDARD_LOCK_FILES,
    STANDARD_RUNTIME_FILES,
)
from runtime_shared.workspace_filesystem import WorkspaceFilesystem


def _deterministic_e2e_root(env_name: str) -> Path | None:
    """Return a test-owned root only when an explicit isolated E2E mode is active."""
    truthy = {"1", "true", "yes", "on"}
    deterministic = str(os.getenv("E2E_DETERMINISTIC_MODE", "")).strip().lower()
    runtime_local = str(os.getenv("E2E_RUNTIME_LOCAL_MODE", "")).strip().lower()
    if deterministic not in truthy and runtime_local not in truthy:
        return None
    raw_path = str(os.getenv(env_name, "")).strip()
    if not raw_path:
        return None
    return Path(raw_path).expanduser().resolve()


class WorkspaceConfig:
    """Central configuration for workspace management"""
    
    @staticmethod
    def get_project_root() -> Path:
        """Get the project root directory"""
        # Start from this file's location and go up to project root
        current_file = Path(__file__).resolve()
        # Go up: workspace_config.py -> config -> backend -> project_root
        return current_file.parent.parent.parent
    
    @staticmethod
    def get_workspaces_base_path() -> Path:
        """Get the base directory for all task workspaces"""
        e2e_root = _deterministic_e2e_root("E2E_WORKSPACE_ROOT")
        if e2e_root is not None:
            return e2e_root
        return WorkspaceConfig.get_project_root() / "agent" / "workspaces"

    @staticmethod
    def get_runtime_control_base_path() -> Path:
        """Get the host-owned root for task runtime control material."""
        e2e_root = _deterministic_e2e_root("E2E_RUNTIME_CONTROL_ROOT")
        if e2e_root is not None:
            return e2e_root
        e2e_workspace_root = _deterministic_e2e_root("E2E_WORKSPACE_ROOT")
        if e2e_workspace_root is not None:
            return e2e_workspace_root.parent / "runtime-control"
        return WorkspaceConfig.get_project_root() / "agent" / "runtime-control"

    @staticmethod
    def get_durable_knowledge_base_path() -> Path:
        """Get the base directory for engagement-owned durable knowledge storage."""
        e2e_root = _deterministic_e2e_root("E2E_DURABLE_KNOWLEDGE_ROOT")
        if e2e_root is not None:
            return e2e_root
        return WorkspaceConfig.get_project_root() / "agent" / "durable_knowledge"

    @staticmethod
    def get_engagement_durable_root_path(engagement_id: int) -> Path:
        """Get the engagement-owned durable storage root."""
        return WorkspaceConfig.get_durable_knowledge_base_path() / f"engagement-{int(engagement_id)}"

    @staticmethod
    def get_engagement_evidence_path(engagement_id: int) -> Path:
        """Get engagement-owned durable evidence archive directory."""
        return WorkspaceConfig.get_engagement_durable_root_path(engagement_id) / "evidence"

    @staticmethod
    def get_engagement_workspace_archives_path(engagement_id: int) -> Path:
        """Get engagement-owned fallback workspace archive directory."""
        return WorkspaceConfig.get_engagement_durable_root_path(engagement_id) / "workspace-archives"

    @staticmethod
    def ensure_engagement_durable_structure(engagement_id: int) -> dict[str, Path]:
        """Ensure engagement-owned durable storage directories exist."""
        root = WorkspaceConfig.get_engagement_durable_root_path(engagement_id)
        evidence = WorkspaceConfig.get_engagement_evidence_path(engagement_id)
        workspace_archives = WorkspaceConfig.get_engagement_workspace_archives_path(engagement_id)
        root.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=True)
        workspace_archives.mkdir(parents=True, exist_ok=True)
        return {
            "root": root,
            "evidence": evidence,
            "workspace_archives": workspace_archives,
        }
    
    @staticmethod
    def get_task_workspace_path(task_id: int) -> Path:
        """Get workspace directory for a specific task"""
        return WorkspaceConfig.get_workspaces_base_path() / f"task-{task_id}"

    @staticmethod
    def get_task_control_path(task_id: int) -> Path:
        """Get the host-owned control root for one task."""
        return WorkspaceConfig.get_runtime_control_base_path() / f"task-{task_id}"

    @staticmethod
    def ensure_control_structure(task_id: int) -> Path:
        """Create the task control root and secured VPN/runtime-input entries."""
        control_path = WorkspaceConfig.get_task_control_path(task_id)
        control_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(control_path, 0o700)
        filesystem = WorkspaceFilesystem(control_path)
        filesystem.mkdirs("vpn", mode=0o700)
        filesystem.mkdirs("runtime-input", mode=0o700)
        filesystem.append_bytes(
            "runtime-input/user_input.jsonl", b"", mode=0o600
        )
        filesystem.chmod_file("runtime-input/user_input.jsonl", 0o600)
        return control_path

    @staticmethod
    def control_filesystem(task_id: int) -> WorkspaceFilesystem:
        """Return the secured filesystem capability for one task control root."""
        return WorkspaceFilesystem(WorkspaceConfig.ensure_control_structure(task_id))

    @staticmethod
    def migrate_legacy_runtime_input(task_id: int) -> None:
        """Copy a safe legacy runtime-input file before recreating the runtime."""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        if not workspace_path.exists():
            return
        try:
            content = WorkspaceFilesystem(workspace_path).read_bytes(
                "user_input.jsonl"
            )
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            warnings.warn(
                "Unsafe legacy runtime input was rejected during control migration.",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        WorkspaceConfig.control_filesystem(task_id).write_bytes_atomic(
            "runtime-input/user_input.jsonl", content, mode=0o600
        )

    @staticmethod
    def finalize_legacy_control_cutover(task_id: int) -> None:
        """Remove legacy control entries after successful runtime recreation."""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        filesystem = WorkspaceFilesystem(workspace_path)
        filesystem.remove("user_input.jsonl", missing_ok=True)
        filesystem.remove(f"vpn/task-{task_id}.ovpn", missing_ok=True)

    @staticmethod
    def cleanup_control(task_id: int) -> None:
        """Remove only the task-matched host control root."""
        control_path = WorkspaceConfig.get_task_control_path(task_id)
        WorkspaceFilesystem(control_path.parent).remove(
            control_path.name,
            recursive=True,
            missing_ok=True,
        )
    
    @staticmethod
    def get_container_workspace_path() -> str:
        """Get the workspace path inside containers"""
        return "/workspace"
    
    @staticmethod
    def get_scope_file_path(task_id: int) -> Path:
        """Get the scope.md file path for a task"""
        return WorkspaceConfig.get_task_workspace_path(task_id) / "scope.md"
    
    @staticmethod
    def get_config_file_path(task_id: int) -> Path:
        """Get the config.json file path for a task"""
        return WorkspaceConfig.get_task_workspace_path(task_id) / "config.json"
    
    @staticmethod
    def get_results_directory_path(task_id: int) -> Path:
        """Get the results directory path for a task"""
        return WorkspaceConfig.get_task_workspace_path(task_id) / "results"
    
    @staticmethod
    def get_logs_directory_path(task_id: int) -> Path:
        """Get the logs directory path for a task"""
        return WorkspaceConfig.get_task_workspace_path(task_id) / "logs"
    
    @staticmethod
    def ensure_workspace_structure(task_id: int) -> Path:
        """Ensure the complete workspace structure exists for a task."""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)

        # Create main workspace directory
        workspace_path.mkdir(parents=True, exist_ok=True)
        filesystem = WorkspaceFilesystem(workspace_path)

        # Create subdirectories
        for sub in [
            "results",
            "logs",
            "scripts",
            "data",
            "artifacts",
            "reports",
            LOCKS_DIRECTORY_NAME,
        ]:
            filesystem.mkdirs(sub, mode=0o755)

        # Communication files
        for file_name in STANDARD_RUNTIME_FILES:
            filesystem.append_bytes(file_name, b"", mode=0o644)

        # Lock files
        for lock_name in STANDARD_LOCK_FILES:
            filesystem.append_bytes(
                f"{LOCKS_DIRECTORY_NAME}/{lock_name}",
                b"",
                mode=0o644,
            )

        return workspace_path
    
    @staticmethod
    def get_mount_config(task_id: int) -> dict:
        """Get Docker mount configuration for a task workspace"""
        host_path = str(WorkspaceConfig.get_task_workspace_path(task_id))
        container_path = WorkspaceConfig.get_container_workspace_path()
        
        return {
            "host_path": host_path,
            "container_path": container_path,
            "mode": "rw"
        }
    
    @staticmethod
    def validate_workspace_exists(task_id: int) -> bool:
        """Check if a task workspace exists and is valid"""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        try:
            WorkspaceFilesystem(workspace_path).list_entries()
            return True
        except (FileNotFoundError, OSError, ValueError):
            return False
    
    @staticmethod
    def cleanup_workspace(task_id: int) -> bool:
        """Clean up a task workspace directory"""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        try:
            WorkspaceFilesystem(workspace_path.parent).remove(
                workspace_path.name,
                recursive=True,
                missing_ok=True,
            )
            WorkspaceConfig.cleanup_control(task_id)
            return True
        except Exception:
            return False
    
    @staticmethod
    def get_workspace_info(task_id: int) -> dict:
        """Get comprehensive workspace information for a task"""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
        
        info = {
            "task_id": task_id,
            "workspace_path": str(workspace_path),
            "exists": workspace_path.exists(),
            "container_path": WorkspaceConfig.get_container_workspace_path(),
            "files": {}
        }
        
        if WorkspaceConfig.validate_workspace_exists(task_id):
            # Check for important files
            scope_file = WorkspaceConfig.get_scope_file_path(task_id)
            config_file = WorkspaceConfig.get_config_file_path(task_id)
            
            filesystem = WorkspaceFilesystem(workspace_path)
            try:
                scope_metadata = filesystem.metadata("scope.md")
            except FileNotFoundError:
                scope_metadata = None
            try:
                config_metadata = filesystem.metadata("config.json")
            except FileNotFoundError:
                config_metadata = None

            info["files"]["scope.md"] = {
                "exists": scope_metadata is not None,
                "path": str(scope_file),
                "size": scope_metadata.size if scope_metadata is not None else 0,
            }
            
            info["files"]["config.json"] = {
                "exists": config_metadata is not None,
                "path": str(config_file),
                "size": config_metadata.size if config_metadata is not None else 0,
            }
            
            # List all files in workspace
            try:
                all_entries = filesystem.list_entries(recursive=True)
                info["total_files"] = len([entry for entry in all_entries if entry.kind == "file"])
                info["total_directories"] = len(
                    [entry for entry in all_entries if entry.kind == "directory"]
                )
            except Exception:
                info["total_files"] = 0
                info["total_directories"] = 0
        
        return info
