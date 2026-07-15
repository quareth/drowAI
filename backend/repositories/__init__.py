"""
Repository package for artifact provenance persistence.

This package contains thin data-access classes for artifact provenance tables.
Repositories do not commit transactions; callers manage commit/rollback.
"""

from .execution_artifact_repository import ExecutionArtifactRepository
from .tool_execution_repository import ToolExecutionRepository

__all__ = [
    "ExecutionArtifactRepository",
    "ToolExecutionRepository",
]
