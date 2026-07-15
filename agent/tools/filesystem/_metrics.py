"""Filesystem operation metrics and limits.

 -: Operation Metrics/Limits

This module provides:
- Configurable limits for filesystem operations per task
- Metrics tracking for monitoring and alerting
- Rate limiting to prevent runaway operations"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Generator, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class FilesystemLimits:
    """Configurable limits for filesystem operations.
    
    These limits are per-task and help prevent runaway operations.
    Set any limit to 0 to disable it.
    """
    # Read operations
    max_reads_per_task: int = 1000
    max_bytes_read_per_task: int = 500_000_000  # 500MB
    
    # Write operations
    max_writes_per_task: int = 500
    max_bytes_written_per_task: int = 100_000_000  # 100MB
    max_files_created_per_task: int = 200
    
    # Edit operations
    max_edits_per_task: int = 500
    
    # Delete operations
    max_deletes_per_task: int = 100
    
    # Rate limiting (ops per second, 0 = unlimited)
    max_ops_per_second: float = 100.0
    
    # Individual operation limits
    max_file_size_bytes: int = 50_000_000  # 50MB per file
    max_lines_per_file: int = 1_000_000
    
    @classmethod
    def from_env(cls) -> "FilesystemLimits":
        """Load limits from environment variables.
        
        Environment variables (all optional):
            FS_MAX_READS_PER_TASK
            FS_MAX_BYTES_READ_PER_TASK
            FS_MAX_WRITES_PER_TASK
            FS_MAX_BYTES_WRITTEN_PER_TASK
            FS_MAX_FILES_CREATED_PER_TASK
            FS_MAX_EDITS_PER_TASK
            FS_MAX_DELETES_PER_TASK
            FS_MAX_OPS_PER_SECOND
            FS_MAX_FILE_SIZE_BYTES
            FS_MAX_LINES_PER_FILE
        """
        return cls(
            max_reads_per_task=int(os.getenv("FS_MAX_READS_PER_TASK", 1000)),
            max_bytes_read_per_task=int(os.getenv("FS_MAX_BYTES_READ_PER_TASK", 500_000_000)),
            max_writes_per_task=int(os.getenv("FS_MAX_WRITES_PER_TASK", 500)),
            max_bytes_written_per_task=int(os.getenv("FS_MAX_BYTES_WRITTEN_PER_TASK", 100_000_000)),
            max_files_created_per_task=int(os.getenv("FS_MAX_FILES_CREATED_PER_TASK", 200)),
            max_edits_per_task=int(os.getenv("FS_MAX_EDITS_PER_TASK", 500)),
            max_deletes_per_task=int(os.getenv("FS_MAX_DELETES_PER_TASK", 100)),
            max_ops_per_second=float(os.getenv("FS_MAX_OPS_PER_SECOND", 100.0)),
            max_file_size_bytes=int(os.getenv("FS_MAX_FILE_SIZE_BYTES", 50_000_000)),
            max_lines_per_file=int(os.getenv("FS_MAX_LINES_PER_FILE", 1_000_000)),
        )


# =============================================================================
# Metrics Tracking
# =============================================================================


@dataclass
class TaskMetrics:
    """Metrics for a single task's filesystem operations."""
    task_id: str
    
    # Operation counts
    read_count: int = 0
    write_count: int = 0
    edit_count: int = 0
    delete_count: int = 0
    list_count: int = 0
    find_count: int = 0
    
    # Byte counts
    bytes_read: int = 0
    bytes_written: int = 0
    
    # File counts
    files_created: int = 0
    files_deleted: int = 0
    
    # Timing
    total_read_time_ms: float = 0.0
    total_write_time_ms: float = 0.0
    
    # Errors
    error_count: int = 0
    last_error: Optional[str] = None
    
    # Rate limiting (0.0 means no previous operation)
    last_op_time: float = 0.0
    
    def to_dict(self) -> Dict[str, object]:
        """Convert metrics to dictionary for logging/reporting."""
        return {
            "task_id": self.task_id,
            "operations": {
                "read": self.read_count,
                "write": self.write_count,
                "edit": self.edit_count,
                "delete": self.delete_count,
                "list": self.list_count,
                "find": self.find_count,
            },
            "bytes": {
                "read": self.bytes_read,
                "written": self.bytes_written,
            },
            "files": {
                "created": self.files_created,
                "deleted": self.files_deleted,
            },
            "timing_ms": {
                "read": self.total_read_time_ms,
                "write": self.total_write_time_ms,
            },
            "errors": {
                "count": self.error_count,
                "last": self.last_error,
            },
        }


class FilesystemMetricsStore:
    """Thread-safe store for filesystem operation metrics.
    
    Tracks metrics per task and enforces limits.
    """
    
    def __init__(self, limits: Optional[FilesystemLimits] = None):
        self._limits = limits or FilesystemLimits.from_env()
        self._metrics: Dict[str, TaskMetrics] = {}
        self._lock = threading.RLock()
    
    @property
    def limits(self) -> FilesystemLimits:
        """Get current limits configuration."""
        return self._limits
    
    def get_metrics(self, task_id: str) -> TaskMetrics:
        """Get or create metrics for a task."""
        with self._lock:
            if task_id not in self._metrics:
                self._metrics[task_id] = TaskMetrics(task_id=task_id)
            return self._metrics[task_id]
    
    def clear_metrics(self, task_id: str) -> None:
        """Clear metrics for a completed task."""
        with self._lock:
            self._metrics.pop(task_id, None)
    
    def check_limit(
        self,
        task_id: str,
        operation: str,
        increment: int = 1,
        bytes_count: int = 0,
    ) -> Optional[str]:
        """Check if an operation would exceed limits.
        
        Args:
            task_id: Task identifier
            operation: Operation type (read, write, edit, delete, list, find)
            increment: Number of operations to add
            bytes_count: Number of bytes involved
            
        Returns:
            Error message if limit exceeded, None if OK
        """
        metrics = self.get_metrics(task_id)
        limits = self._limits
        
        # Check operation counts
        if operation == "read":
            if limits.max_reads_per_task > 0:
                if metrics.read_count + increment > limits.max_reads_per_task:
                    return f"Read limit exceeded: {metrics.read_count}/{limits.max_reads_per_task} operations"
            if limits.max_bytes_read_per_task > 0:
                if metrics.bytes_read + bytes_count > limits.max_bytes_read_per_task:
                    return f"Read bytes limit exceeded: {metrics.bytes_read}/{limits.max_bytes_read_per_task} bytes"
        
        elif operation == "write":
            if limits.max_writes_per_task > 0:
                if metrics.write_count + increment > limits.max_writes_per_task:
                    return f"Write limit exceeded: {metrics.write_count}/{limits.max_writes_per_task} operations"
            if limits.max_bytes_written_per_task > 0:
                if metrics.bytes_written + bytes_count > limits.max_bytes_written_per_task:
                    return f"Write bytes limit exceeded: {metrics.bytes_written}/{limits.max_bytes_written_per_task} bytes"
        
        elif operation == "edit":
            if limits.max_edits_per_task > 0:
                if metrics.edit_count + increment > limits.max_edits_per_task:
                    return f"Edit limit exceeded: {metrics.edit_count}/{limits.max_edits_per_task} operations"
        
        elif operation == "delete":
            if limits.max_deletes_per_task > 0:
                if metrics.delete_count + increment > limits.max_deletes_per_task:
                    return f"Delete limit exceeded: {metrics.delete_count}/{limits.max_deletes_per_task} operations"
        
        # Check file size limit
        if bytes_count > 0 and limits.max_file_size_bytes > 0:
            if bytes_count > limits.max_file_size_bytes:
                return f"File size exceeds limit: {bytes_count}/{limits.max_file_size_bytes} bytes"
        
        # Check rate limit (only after first operation)
        if limits.max_ops_per_second > 0 and metrics.last_op_time > 0:
            now = time.time()
            min_interval = 1.0 / limits.max_ops_per_second
            if now - metrics.last_op_time < min_interval:
                return f"Rate limit: max {limits.max_ops_per_second} ops/sec"
        
        return None
    
    def record_operation(
        self,
        task_id: str,
        operation: str,
        bytes_count: int = 0,
        duration_ms: float = 0.0,
        error: Optional[str] = None,
        files_created: int = 0,
        files_deleted: int = 0,
    ) -> None:
        """Record a completed operation.
        
        Args:
            task_id: Task identifier
            operation: Operation type
            bytes_count: Bytes involved
            duration_ms: Operation duration in milliseconds
            error: Error message if operation failed
            files_created: Number of files created
            files_deleted: Number of files deleted
        """
        with self._lock:
            metrics = self.get_metrics(task_id)
            metrics.last_op_time = time.time()
            
            if error:
                metrics.error_count += 1
                metrics.last_error = error
                return
            
            if operation == "read":
                metrics.read_count += 1
                metrics.bytes_read += bytes_count
                metrics.total_read_time_ms += duration_ms
            elif operation == "write":
                metrics.write_count += 1
                metrics.bytes_written += bytes_count
                metrics.total_write_time_ms += duration_ms
                metrics.files_created += files_created
            elif operation == "edit":
                metrics.edit_count += 1
            elif operation == "delete":
                metrics.delete_count += 1
                metrics.files_deleted += files_deleted
            elif operation == "list":
                metrics.list_count += 1
            elif operation == "find":
                metrics.find_count += 1
    
    @contextmanager
    def track_operation(
        self,
        task_id: str,
        operation: str,
        bytes_estimate: int = 0,
    ) -> Generator[None, None, None]:
        """Context manager for tracking operation timing.
        
        Usage:
            with metrics.track_operation(task_id, "read", 1000):
                # perform read operation
                pass
        """
        # Check limit before operation
        error = self.check_limit(task_id, operation, bytes_count=bytes_estimate)
        if error:
            raise FilesystemLimitExceeded(error)
        
        start = time.time()
        error_msg = None
        try:
            yield
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            duration_ms = (time.time() - start) * 1000
            self.record_operation(
                task_id=task_id,
                operation=operation,
                bytes_count=bytes_estimate,
                duration_ms=duration_ms,
                error=error_msg,
            )


class FilesystemLimitExceeded(Exception):
    """Raised when a filesystem operation limit is exceeded."""
    pass


# =============================================================================
# Global Instance
# =============================================================================

# Global metrics store - initialized on first use
_metrics_store: Optional[FilesystemMetricsStore] = None
_metrics_lock = threading.Lock()


def get_metrics_store() -> FilesystemMetricsStore:
    """Get the global metrics store instance."""
    global _metrics_store
    with _metrics_lock:
        if _metrics_store is None:
            _metrics_store = FilesystemMetricsStore()
        return _metrics_store


def get_task_metrics(task_id: str) -> TaskMetrics:
    """Convenience function to get metrics for a task."""
    return get_metrics_store().get_metrics(task_id)


def check_operation_limit(
    task_id: str,
    operation: str,
    bytes_count: int = 0,
) -> Optional[str]:
    """Convenience function to check if operation is within limits.
    
    Returns error message if limit exceeded, None if OK.
    """
    return get_metrics_store().check_limit(task_id, operation, bytes_count=bytes_count)


def record_filesystem_operation(
    task_id: str,
    operation: str,
    bytes_count: int = 0,
    duration_ms: float = 0.0,
    error: Optional[str] = None,
) -> None:
    """Convenience function to record a filesystem operation."""
    get_metrics_store().record_operation(
        task_id=task_id,
        operation=operation,
        bytes_count=bytes_count,
        duration_ms=duration_ms,
        error=error,
    )
