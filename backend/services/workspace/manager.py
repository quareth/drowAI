"""
Workspace Management Service

Manages directory structures and file organization for penetration testing tasks.
Creates isolated workspaces for each task with proper structure and permissions.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from backend.core.time_utils import format_iso, utc_now

from backend.config.workspace_config import WorkspaceConfig
from runtime_shared.workspace_filesystem import WorkspaceFilesystem

logger = logging.getLogger("backend.services.workspace_manager")

class WorkspaceManager:
    """
    Manages workspace directories for penetration testing tasks.
    
    Each task gets an isolated workspace with standardized directory structure
    for scope files, configurations, logs, artifacts, and reports.
    """
    
    def __init__(self):
        """
        Initialize workspace manager with robust path configuration.
        """
        # Ensure base workspaces directory exists
        base_path = WorkspaceConfig.get_workspaces_base_path()
        base_path.mkdir(parents=True, exist_ok=True)
        self.base_path = base_path
        
        # Ensure proper permissions for container access
        try:
            os.chmod(base_path, 0o755)
        except OSError as e:
            logger.warning(f"Could not set permissions on {base_path}: {e}")
        
        logger.info(f"Workspace manager initialized with base path: {base_path}")
    
    def create_workspace(self, task_id: int) -> str:
        """
        Create workspace directory structure for a task.
        
        Args:
            task_id: Unique task identifier
            
        Returns:
            Absolute path to created workspace
            
        Raises:
            OSError: If directory creation fails
        """
        workspace_path = WorkspaceConfig.ensure_workspace_structure(task_id)
        
        try:
            # Set workspace permissions for container access
            os.chmod(workspace_path, 0o755)
            
            logger.info(f"Created workspace for task {task_id} at {workspace_path}")
            return str(workspace_path)
            
        except OSError as e:
            logger.error(f"Failed to create workspace for task {task_id}: {e}")
            raise
    
    def get_workspace_path(self, task_id: int) -> Path:
        """
        Get absolute path to task workspace.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Path object for workspace directory
        """
        return WorkspaceConfig.get_task_workspace_path(task_id)
    
    def workspace_exists(self, task_id: int) -> bool:
        """
        Check if workspace exists for task.
        
        Args:
            task_id: Task identifier
            
        Returns:
            True if workspace directory exists
        """
        return WorkspaceConfig.validate_workspace_exists(task_id)
    
    def save_scope_file(self, task_id: int, file_content: str, filename: str = "scope.md") -> str:
        """
        Save scope file to task workspace.
        
        Args:
            task_id: Task identifier
            file_content: Content to save
            filename: Name of scope file (default: scope.md)
            
        Returns:
            Absolute path to saved file
            
        Raises:
            OSError: If file creation fails
            ValueError: If workspace doesn't exist
        """
        # Ensure workspace exists
        WorkspaceConfig.ensure_workspace_structure(task_id)
        
        try:
            workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
            relative_path = "scope.md" if filename == "scope.md" else filename
            WorkspaceFilesystem(workspace_path).write_bytes_atomic(
                relative_path,
                file_content.encode("utf-8"),
                mode=0o644,
            )
            scope_file = workspace_path / relative_path
            
            logger.info(f"Saved scope file for task {task_id}: {scope_file}")
            return str(scope_file)
            
        except OSError as e:
            logger.error(f"Failed to save scope file for task {task_id}: {e}")
            raise
    
    def save_config_file(self, task_id: int, config_data: Dict[str, Any]) -> str:
        """
        Save task configuration to workspace.
        
        Args:
            task_id: Task identifier
            config_data: Configuration dictionary
            
        Returns:
            Absolute path to saved config file
            
        Raises:
            OSError: If file creation fails
            ValueError: If workspace doesn't exist
        """
        # Ensure workspace exists
        WorkspaceConfig.ensure_workspace_structure(task_id)
        
        try:
            config_file = WorkspaceConfig.get_config_file_path(task_id)
            workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
            
            # Add metadata to config
            config_with_meta = {
                "task_id": task_id,
                "created_at": format_iso(utc_now()),
                "workspace_path": str(workspace_path),
                **config_data
            }
            
            WorkspaceFilesystem(workspace_path).write_bytes_atomic(
                "config.json",
                json.dumps(config_with_meta, indent=2).encode("utf-8"),
                mode=0o644,
            )
            
            logger.info(f"Saved config file for task {task_id}: {config_file}")
            return str(config_file)
            
        except (OSError, Exception) as e:
            logger.error(f"Failed to save config file for task {task_id}: {e}")
            raise
    
    def get_config_data(self, task_id: int) -> Optional[Dict[str, Any]]:
        """
        Load task configuration from workspace.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Configuration dictionary or None if not found
        """
        workspace_path = self.get_workspace_path(task_id)
        try:
            content = WorkspaceFilesystem(workspace_path).read_bytes("config.json")
            return json.loads(content.decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load config for task {task_id}: {e}")
            return None
    
    def get_log_file_path(self, task_id: int, log_name: str = "agent.log") -> str:
        """
        Get path to log file in workspace.
        
        Args:
            task_id: Task identifier
            log_name: Name of log file
            
        Returns:
            Absolute path to log file
        """
        workspace_path = self.get_workspace_path(task_id)
        return str(workspace_path / "logs" / log_name)
    
    def get_artifacts_dir(self, task_id: int) -> str:
        """
        Get path to artifacts directory.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Absolute path to artifacts directory
        """
        workspace_path = self.get_workspace_path(task_id)
        return str(workspace_path / "artifacts")
    
    def get_reports_dir(self, task_id: int) -> str:
        """
        Get path to reports directory.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Absolute path to reports directory
        """
        workspace_path = self.get_workspace_path(task_id)
        return str(workspace_path / "reports")
    
    def list_workspace_files(self, task_id: int) -> Dict[str, List[str]]:
        """
        List all files in workspace by category.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Dictionary with file lists by category
        """
        workspace_path = self.get_workspace_path(task_id)
        
        if not self.workspace_exists(task_id):
            return {"error": ["Workspace does not exist"]}
        
        try:
            result = {
                "root": [],
                "logs": [],
                "artifacts": [],
                "reports": []
            }
            
            entries = WorkspaceFilesystem(workspace_path).list_entries(recursive=True)
            for entry in entries:
                if entry.kind != "file":
                    continue
                path = Path(entry.relative_path)
                if len(path.parts) == 1:
                    result["root"].append(path.name)
                elif path.parts[0] in {"logs", "artifacts", "reports"} and len(path.parts) == 2:
                    result[path.parts[0]].append(path.name)
            
            return result
            
        except OSError as e:
            logger.error(f"Failed to list files for task {task_id}: {e}")
            return {"error": [str(e)]}
    
    def archive_workspace(
        self,
        task_id: int,
        archive_name: Optional[str] = None,
        *,
        engagement_id: Optional[int] = None,
    ) -> str:
        """
        Create ZIP archive of workspace.
        
        Args:
            task_id: Task identifier
            archive_name: Custom archive name (optional)
            engagement_id: Optional durable owner for engagement-owned archive storage
            
        Returns:
            Absolute path to created archive
            
        Raises:
            OSError: If archiving fails
            ValueError: If workspace doesn't exist
        """
        workspace_path = self.get_workspace_path(task_id)
        
        if not self.workspace_exists(task_id):
            raise ValueError(f"Workspace for task {task_id} does not exist")
        
        if archive_name is None:
            timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
            archive_name = f"task_{task_id}_workspace_{timestamp}.zip"
        
        if engagement_id is not None:
            durable_paths = WorkspaceConfig.ensure_engagement_durable_structure(engagement_id)
            archive_path = durable_paths["workspace_archives"]
        else:
            archive_path = self.base_path / "archives"
            archive_path.mkdir(exist_ok=True)
        
        full_archive_path = archive_path / archive_name
        
        try:
            filesystem = WorkspaceFilesystem(workspace_path)
            top_level = tuple(
                entry.relative_path for entry in filesystem.list_entries(recursive=False)
            )
            if top_level:
                filesystem.create_zip(top_level, destination=full_archive_path)
            else:
                import zipfile

                with zipfile.ZipFile(full_archive_path, "w"):
                    pass
            logger.info(f"Archived workspace for task {task_id}: {full_archive_path}")
            return str(full_archive_path)
            
        except OSError as e:
            logger.error(f"Failed to archive workspace for task {task_id}: {e}")
            raise
    
    def cleanup_workspace(
        self,
        task_id: int,
        archive_first: bool = True,
        *,
        engagement_id: Optional[int] = None,
    ) -> bool:
        """
        Remove workspace directory and all contents.
        
        Args:
            task_id: Task identifier
            archive_first: Whether to create archive before deletion
            engagement_id: Optional durable owner for engagement-owned fallback ZIP retention
            
        Returns:
            True if cleanup successful
        """
        workspace_path = self.get_workspace_path(task_id)
        
        if not self.workspace_exists(task_id):
            logger.warning(f"Workspace for task {task_id} does not exist")
            WorkspaceConfig.cleanup_control(task_id)
            return True
        
        try:
            # Create archive if requested
            if archive_first:
                try:
                    self.archive_workspace(task_id, engagement_id=engagement_id)
                except Exception as e:
                    logger.error(f"Failed to archive before cleanup for task {task_id}: {e}")
                    # Continue with cleanup even if archiving fails
            
            WorkspaceFilesystem(workspace_path.parent).remove(
                workspace_path.name,
                recursive=True,
            )
            WorkspaceConfig.cleanup_control(task_id)
            
            logger.info(f"Cleaned up workspace for task {task_id}")
            return True
            
        except OSError as e:
            logger.error(f"Failed to cleanup workspace for task {task_id}: {e}")
            return False
    
    def get_workspace_size(self, task_id: int) -> int:
        """
        Calculate total size of workspace in bytes.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Total size in bytes, 0 if workspace doesn't exist
        """
        workspace_path = self.get_workspace_path(task_id)
        
        if not self.workspace_exists(task_id):
            return 0
        
        try:
            entries = WorkspaceFilesystem(workspace_path).list_entries(recursive=True)
            return sum(entry.size for entry in entries if entry.kind == "file")
            
        except OSError as e:
            logger.error(f"Failed to calculate workspace size for task {task_id}: {e}")
            return 0
    
    def get_workspace_info(self, task_id: int) -> Dict[str, Any]:
        """
        Get comprehensive workspace information.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Dictionary with workspace details
        """
        return WorkspaceConfig.get_workspace_info(task_id)
    
    def get_mount_config(self, task_id: int) -> Dict[str, str]:
        """
        Get Docker mount configuration for a task.
        
        Args:
            task_id: Task identifier
            
        Returns:
            Dictionary with mount configuration
        """
        return WorkspaceConfig.get_mount_config(task_id)


# Factory function for dependency injection
def get_workspace_manager() -> WorkspaceManager:
    """Get WorkspaceManager instance for dependency injection."""
    return WorkspaceManager()


# Utility functions for common operations
def create_task_workspace(task_id: int) -> str:
    """Convenience function to create workspace for a task."""
    manager = get_workspace_manager()
    return manager.create_workspace(task_id)


def save_task_scope(task_id: int, scope_content: str) -> str:
    """Convenience function to save scope file for a task."""
    manager = get_workspace_manager()
    return manager.save_scope_file(task_id, scope_content)


def get_task_workspace_path(task_id: int) -> str:
    """Convenience function to get workspace path for a task."""
    manager = get_workspace_manager()
    return str(manager.get_workspace_path(task_id))
