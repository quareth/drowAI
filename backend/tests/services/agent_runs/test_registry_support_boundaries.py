"""Structural boundary tests for process-local registry support modules."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).parents[4]
_SUPPORT_MODULES = {
    "backend.services.agent_runs.registry_contracts": _ROOT
    / "backend/services/agent_runs/registry_contracts.py",
    "backend.services.agent_runs.registry_lifecycle": _ROOT
    / "backend/services/agent_runs/registry_lifecycle.py",
    "backend.services.agent_runs.registry_queries": _ROOT
    / "backend/services/agent_runs/registry_queries.py",
    "backend.services.agent_runs.registry_handoffs": _ROOT
    / "backend/services/agent_runs/registry_handoffs.py",
    "backend.services.agent_runs.registry_signaling": _ROOT
    / "backend/services/agent_runs/registry_signaling.py",
}
_FORBIDDEN_ABSOLUTE_PREFIXES = (
    "backend.database",
    "backend.main",
    "backend.models",
    "backend.routers",
    "backend.services.agent_runs.registry",
    "backend.services.langgraph_chat",
    "fastapi",
    "langgraph",
    "sqlalchemy",
)
_FORBIDDEN_RELATIVE_MODULES = {"registry"}


def _parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _relative_imported_modules(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            imports.add(node.module)
    return imports


def test_support_modules_have_purpose_boundary_docstrings() -> None:
    for module_name, path in _SUPPORT_MODULES.items():
        tree = _parse_module(path)

        assert ast.get_docstring(tree), module_name


def test_support_modules_do_not_import_forbidden_layers() -> None:
    for module_name, path in _SUPPORT_MODULES.items():
        tree = _parse_module(path)
        imported_modules = _imported_modules(tree)
        relative_imported_modules = _relative_imported_modules(tree)

        forbidden_absolute = {
            imported
            for imported in imported_modules
            for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES
            if imported == prefix or imported.startswith(f"{prefix}.")
        }
        forbidden_relative = relative_imported_modules & _FORBIDDEN_RELATIVE_MODULES

        assert not forbidden_absolute, (module_name, sorted(forbidden_absolute))
        assert not forbidden_relative, (module_name, sorted(forbidden_relative))
