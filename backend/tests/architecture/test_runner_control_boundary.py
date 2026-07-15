"""Runner-control package boundary tests for runner control.

Responsibilities:
- Enforce backend-free imports for shared runner protocol DTOs.
- Keep runner package modules isolated from backend-only management modules.
- Prevent backend runner-control services from importing runner implementation code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import pathlib
import re
from typing import Iterable


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_RUNNER_PROTOCOL_MODULE = _REPO_ROOT / "runtime_shared" / "runner_protocol.py"
_RUNNER_PACKAGE_ROOT = _REPO_ROOT / "drowai_runner"
_BACKEND_RUNNER_CONTROL_ROOT = _REPO_ROOT / "backend" / "services" / "runner_control"

_SHARED_PROTOCOL_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "backend",
    "drowai_runner",
    "fastapi",
    "sqlalchemy",
    "docker",
)
_RUNNER_PACKAGE_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "backend.routers",
    "backend.auth",
    "backend.models",
    "backend.database",
    "backend.services.runtime_provider",
)
_BACKEND_RUNNER_CONTROL_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = ("drowai_runner",)


@dataclass(frozen=True)
class _TemporaryException:
    source_module_prefix: str
    allowed_import_prefix: str
    todo: str
    owner: str
    future_owner: str
    removal_condition: str


@dataclass(frozen=True)
class _ImportMatch:
    file_path: pathlib.Path
    imported_module: str
    line_number: int

    def format(self) -> str:
        rel_path = self.file_path.relative_to(_REPO_ROOT)
        return f"{rel_path}:{self.line_number}: import `{self.imported_module}`"


_TEMPORARY_EXCEPTIONS: tuple[_TemporaryException, ...] = ()
_FUTURE_OWNER_RE = re.compile(
    r"^(runner_control|tooling_plane|execution_plane|remote_runtime|data_plane|tenant_baseline|tenant_isolation|cutover)\+?$"
)


def _iter_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _collect_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                imports.append((node.lineno, node.module))
                for alias in node.names:
                    imports.append((node.lineno, f"{node.module}.{alias.name}"))
    return imports


def _module_matches_prefix(module_name: str, prefix: str) -> bool:
    return module_name == prefix or module_name.startswith(f"{prefix}.")


def _is_forbidden(module_name: str, prefixes: Iterable[str]) -> bool:
    return any(_module_matches_prefix(module_name, prefix) for prefix in prefixes)


def _is_allowlisted(file_path: pathlib.Path, module_name: str) -> bool:
    rel_module = ".".join(file_path.relative_to(_REPO_ROOT).with_suffix("").parts)
    for exception in _TEMPORARY_EXCEPTIONS:
        if not rel_module.startswith(exception.source_module_prefix):
            continue
        if _module_matches_prefix(module_name, exception.allowed_import_prefix):
            return True
    return False


def _find_forbidden_imports(
    *,
    files: Iterable[pathlib.Path],
    forbidden_prefixes: tuple[str, ...],
) -> list[_ImportMatch]:
    matches: list[_ImportMatch] = []
    for file_path in files:
        for line_number, imported_module in _collect_imports(file_path):
            if not _is_forbidden(imported_module, forbidden_prefixes):
                continue
            if _is_allowlisted(file_path, imported_module):
                continue
            matches.append(
                _ImportMatch(
                    file_path=file_path,
                    imported_module=imported_module,
                    line_number=line_number,
                )
            )
    return matches


def test_runner_protocol_module_is_backend_free() -> None:
    """runner control lock: shared runner protocol DTOs stay framework/backend free."""
    matches = _find_forbidden_imports(
        files=(_RUNNER_PROTOCOL_MODULE,),
        forbidden_prefixes=_SHARED_PROTOCOL_FORBIDDEN_IMPORT_PREFIXES,
    )
    assert not matches, (
        "runtime_shared.runner_protocol must stay backend/runner/framework-free. Found:\n  - "
        + "\n  - ".join(match.format() for match in matches)
    )


def test_runner_package_blocks_backend_management_imports() -> None:
    """runner control lock: runner modules must not import backend management modules."""
    matches = _find_forbidden_imports(
        files=_iter_python_files(_RUNNER_PACKAGE_ROOT),
        forbidden_prefixes=_RUNNER_PACKAGE_FORBIDDEN_IMPORT_PREFIXES,
    )
    assert not matches, (
        "drowai_runner modules must not import backend routers/models/database/auth/runtime-provider modules. Found:\n  - "
        + "\n  - ".join(match.format() for match in matches)
    )


def test_backend_runner_control_services_do_not_import_runner_impl() -> None:
    """runner control lock: backend runner-control services consume shared DTOs, not runner internals."""
    matches = _find_forbidden_imports(
        files=_iter_python_files(_BACKEND_RUNNER_CONTROL_ROOT),
        forbidden_prefixes=_BACKEND_RUNNER_CONTROL_FORBIDDEN_IMPORT_PREFIXES,
    )
    assert not matches, (
        "backend runner_control services must not import drowai_runner implementation modules. Found:\n  - "
        + "\n  - ".join(match.format() for match in matches)
    )


def test_runner_control_temporary_exceptions_are_explicit() -> None:
    """runner control lock: boundary exceptions require ownership and future phase cleanup."""
    for exception in _TEMPORARY_EXCEPTIONS:
        assert exception.source_module_prefix
        assert exception.allowed_import_prefix
        assert exception.todo.startswith("TODO(")
        assert exception.owner
        assert _FUTURE_OWNER_RE.fullmatch(exception.future_owner), (
            "Temporary exception must declare a future domain owner (tooling_plane+)."
        )
        assert exception.removal_condition
