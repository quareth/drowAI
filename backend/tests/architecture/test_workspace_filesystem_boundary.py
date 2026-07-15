"""Guard privileged task-workspace access behind WorkspaceFilesystem."""

from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOUNDARY_MODULES = (
    "backend/config/workspace_config.py",
    "backend/services/runtime_provider/local_docker_provider.py",
    "backend/services/runtime_provider/local_file_comm_cancel.py",
    "backend/services/workspace/environment_collector.py",
    "backend/services/workspace/file_browser_service.py",
    "backend/services/workspace/manager.py",
    "backend/services/workspace/runtime_file_explorer_service.py",
    "backend/services/workspace/runtime_workspace_query_service.py",
    "drowai_runner/artifact_manifest.py",
    "drowai_runner/artifact_uploader.py",
    "drowai_runner/cleanup.py",
    "drowai_runner/environment.py",
    "drowai_runner/file_comm_bridge.py",
    "drowai_runner/lifecycle_operations.py",
    "drowai_runner/logs_metrics.py",
    "drowai_runner/workspace.py",
    "runtime_shared/workspace_files.py",
)
_FORBIDDEN_PATH_METHODS = {
    "open",
    "read_bytes",
    "read_text",
    "rglob",
    "touch",
    "write_bytes",
    "write_text",
}
# These operate on runner-owned durable metadata/retention destinations, not a
# runtime-writable task workspace. Temporary HTTP/ZIP files are likewise out of
# scope and use tempfile/ZipFile.writestr rather than workspace pathname opens.
_TRUSTED_NON_WORKSPACE_EXCLUSIONS = {
    ("drowai_runner/workspace.py", "initialize_runner_root", "write_text"),
    ("drowai_runner/cleanup.py", "_retain_recent_files", "write_bytes"),
}


class _DirectWorkspaceOperationVisitor(ast.NodeVisitor):
    """Collect forbidden pathname operations with their enclosing function."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.function = "<module>"
        self.violations: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    @staticmethod
    def _is_shared_filesystem_receiver(receiver: ast.expr) -> bool:
        if isinstance(receiver, ast.Name):
            return receiver.id.endswith("filesystem")
        if isinstance(receiver, ast.Attribute):
            return receiver.attr.endswith("filesystem")
        return (
            isinstance(receiver, ast.Call)
            and (
                (
                    isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "WorkspaceFilesystem"
                )
                or (
                    isinstance(receiver.func, ast.Attribute)
                    and receiver.func.attr.endswith("filesystem")
                )
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        method = node.func.attr if isinstance(node.func, ast.Attribute) else None
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if (
            method in _FORBIDDEN_PATH_METHODS
            and receiver is not None
            and not self._is_shared_filesystem_receiver(receiver)
        ):
            exclusion = (self.module, self.function, method)
            if exclusion not in _TRUSTED_NON_WORKSPACE_EXCLUSIONS:
                self.violations.append(
                    f"{self.module}:{node.lineno} {self.function} uses .{method}()"
                )
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            self.violations.append(
                f"{self.module}:{node.lineno} {self.function} uses open()"
            )
        if (
            method == "write"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"archive", "zip_file", "zipfile"}
        ):
            self.violations.append(
                f"{self.module}:{node.lineno} {self.function} uses ZipFile.write(path)"
            )
        self.generic_visit(node)


def test_host_workspace_operations_use_shared_filesystem_capability() -> None:
    """Fail when boundary modules add direct runtime-writable pathname I/O."""

    violations: list[str] = []
    for module in _BOUNDARY_MODULES:
        source = (_REPO_ROOT / module).read_text(encoding="utf-8")
        visitor = _DirectWorkspaceOperationVisitor(module)
        visitor.visit(ast.parse(source, filename=module))
        violations.extend(visitor.violations)

    assert violations == []
