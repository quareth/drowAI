"""Execution-plane packaging boundary tests for Execution Plane.

Responsibilities:
- Enforce static import boundaries for runtime-image modules.
- Enforce static import boundaries for runner-package modules.
- Keep temporary exceptions explicit and TODO-tagged in the manifest.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Iterable


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MANIFEST_PATH = (
    _REPO_ROOT / "runtime/manifests/runtime-package-manifest.md"
)
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", flags=re.DOTALL)
_FUTURE_OWNER_RE = re.compile(
    r"^(runner_control|tooling_plane|execution_plane|remote_runtime|data_plane|tenant_baseline|tenant_isolation|cutover)\+?$"
)

_RUNTIME_IMAGE_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "backend.database",
    "backend.models",
    "backend.routers",
    "backend.services.knowledge",
    "backend.services.artifact",
    "backend.services.terminal",
    "backend.services.llm_provider",
    "backend.services.runtime_provider",
    "agent.tools",
    "agent.tool_runtime",
    "agent.graph",
)
_RUNTIME_IMAGE_FORBIDDEN_EXACT_IMPORTS: tuple[str, ...] = ("docker",)

_RUNNER_PACKAGE_FORBIDDEN_IMPORT_PREFIXES_FALLBACK: tuple[str, ...] = (
    "backend.routers",
    "backend.auth",
    "backend.models",
    "backend.database",
    "backend.services.knowledge",
    "backend.services.artifact",
    "backend.services.terminal",
    "backend.services.llm_provider",
    "backend.services.runtime_provider",
    "backend.services.unified_docker_service",
    "client",
    "server",
)
_RUNNER_PACKAGE_REQUIRED_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "backend.routers",
    "backend.auth",
    "backend.models",
    "backend.database",
    "backend.services",
)
_RUNNER_PACKAGE_REQUIRED_FORBIDDEN_EXACT_IMPORTS: tuple[str, ...] = (
    "fastapi",
    "sqlalchemy",
)

_CLOUD_RUNNER_PROVIDER_GLOB_PATTERNS: tuple[str, ...] = (
    "backend/services/runtime_provider/*cloud*.py",
    "backend/services/runtime_provider/*remote*.py",
    "backend/services/runtime_provider/cloud_runner/**/*.py",
)
_CLOUD_RUNNER_PROVIDER_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "backend.services.runtime_provider.local_docker_provider",
    "backend.services.unified_docker_service",
    "backend.services.container_utils",
    "drowai_runner",
    "docker",
)
_RUNTIME_IMAGE_FORBIDDEN_COPY_SOURCES: tuple[str, ...] = (
    "agent/tools",
    "agent/tool_runtime",
    "agent/graph",
    "agent/semantic",
    "agent/models.py",
    "agent/execution_strategy.py",
)
_RUNTIME_DOCKERFILE_PATH = _REPO_ROOT / "runtime/image/Dockerfile"


@dataclass(frozen=True)
class _TemporaryException:
    source_module_prefix: str
    allowed_management_import_prefix: str
    todo: str
    owner: str
    future_owner: str
    removal_condition: str


@dataclass(frozen=True)
class _RuntimePackageManifest:
    runtime_image_roots: tuple[str, ...]
    runtime_image_excluded_module_prefixes: tuple[str, ...]
    runner_package_roots: tuple[str, ...]
    management_module_prefixes: tuple[str, ...]
    temporary_exceptions: tuple[_TemporaryException, ...]


@dataclass(frozen=True)
class _ImportMatch:
    file_path: pathlib.Path
    module_name: str
    imported_module: str
    line_number: int

    def format(self) -> str:
        rel_path = self.file_path.relative_to(_REPO_ROOT)
        return f"{rel_path}:{self.line_number}: import `{self.imported_module}`"


def _extract_manifest_json(markdown: str) -> dict[str, object]:
    match = _JSON_BLOCK_RE.search(markdown)
    if not match:
        raise AssertionError("Runtime package manifest JSON block is missing.")
    return json.loads(match.group(1))


def _load_manifest() -> _RuntimePackageManifest:
    payload = _extract_manifest_json(_MANIFEST_PATH.read_text(encoding="utf-8"))
    runtime_image = payload.get("runtime_image", {})
    runner_package = payload.get("runner_package", {})
    management_only = payload.get("management_only", {})
    raw_exceptions = payload.get("temporary_exceptions", [])

    exceptions = tuple(
        _TemporaryException(
            source_module_prefix=item["source_module_prefix"],
            allowed_management_import_prefix=item["allowed_management_import_prefix"],
            todo=item["todo"],
            owner=item["owner"],
            future_owner=item["future_owner"],
            removal_condition=item["removal_condition"],
        )
        for item in raw_exceptions
    )

    return _RuntimePackageManifest(
        runtime_image_roots=tuple(runtime_image.get("python_roots", [])),
        runtime_image_excluded_module_prefixes=tuple(
            runtime_image.get("excluded_module_prefixes", [])
        ),
        runner_package_roots=tuple(runner_package.get("python_roots", [])),
        management_module_prefixes=tuple(
            management_only.get("python_module_prefixes", [])
            if isinstance(management_only, dict)
            else ()
        ),
        temporary_exceptions=exceptions,
    )


def _iter_python_files(roots: Iterable[str]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        root_path = _REPO_ROOT / root
        if root_path.is_file() and root_path.suffix == ".py":
            files.append(root_path)
            continue
        if root_path.is_dir():
            files.extend(sorted(root_path.rglob("*.py")))
    return sorted(set(files))


def _module_from_path(path: pathlib.Path) -> str:
    rel = path.relative_to(_REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


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


def _is_allowlisted(
    *,
    source_module: str,
    imported_module: str,
    manifest: _RuntimePackageManifest,
) -> bool:
    for exc in manifest.temporary_exceptions:
        if source_module.startswith(exc.source_module_prefix) and imported_module.startswith(
            exc.allowed_management_import_prefix
        ):
            return True
    return False


def _matches_prefixes(imported_module: str, prefixes: tuple[str, ...]) -> bool:
    return any(imported_module.startswith(prefix) for prefix in prefixes)


def _runtime_image_violations(
    *, allow_temporary_exceptions: bool
) -> tuple[list[_ImportMatch], list[_ImportMatch]]:
    manifest = _load_manifest()
    violations: list[_ImportMatch] = []
    allowlisted: list[_ImportMatch] = []
    for file_path in _iter_python_files(manifest.runtime_image_roots):
        module_name = _module_from_path(file_path)
        if any(
            module_name.startswith(prefix)
            for prefix in manifest.runtime_image_excluded_module_prefixes
        ):
            continue
        for line_number, imported_module in _collect_imports(file_path):
            is_forbidden = _matches_prefixes(
                imported_module, _RUNTIME_IMAGE_FORBIDDEN_IMPORT_PREFIXES
            ) or imported_module in _RUNTIME_IMAGE_FORBIDDEN_EXACT_IMPORTS
            if not is_forbidden:
                continue

            match = _ImportMatch(
                file_path=file_path,
                module_name=module_name,
                imported_module=imported_module,
                line_number=line_number,
            )
            if allow_temporary_exceptions and _is_allowlisted(
                source_module=module_name,
                imported_module=imported_module,
                manifest=manifest,
            ):
                allowlisted.append(match)
                continue
            violations.append(match)
    return violations, allowlisted


def _runner_package_violations() -> list[_ImportMatch]:
    manifest = _load_manifest()
    manifest_forbidden_prefixes = (
        tuple(manifest.management_module_prefixes)
        if manifest.management_module_prefixes
        else _RUNNER_PACKAGE_FORBIDDEN_IMPORT_PREFIXES_FALLBACK
    )
    forbidden_prefixes = tuple(
        dict.fromkeys(
            manifest_forbidden_prefixes
            + _RUNNER_PACKAGE_REQUIRED_FORBIDDEN_IMPORT_PREFIXES
        )
    )
    violations: list[_ImportMatch] = []
    for file_path in _iter_python_files(manifest.runner_package_roots):
        module_name = _module_from_path(file_path)
        for line_number, imported_module in _collect_imports(file_path):
            if not _matches_prefixes(
                imported_module, forbidden_prefixes
            ) and imported_module not in _RUNNER_PACKAGE_REQUIRED_FORBIDDEN_EXACT_IMPORTS:
                continue
            if _is_allowlisted(
                source_module=module_name,
                imported_module=imported_module,
                manifest=manifest,
            ):
                continue
            violations.append(
                _ImportMatch(
                    file_path=file_path,
                    module_name=module_name,
                    imported_module=imported_module,
                    line_number=line_number,
                )
            )
    return violations


def _cloud_runner_provider_shell_violations() -> list[_ImportMatch]:
    cloud_provider_files: set[pathlib.Path] = set()
    for pattern in _CLOUD_RUNNER_PROVIDER_GLOB_PATTERNS:
        cloud_provider_files.update(_REPO_ROOT.glob(pattern))

    violations: list[_ImportMatch] = []
    for file_path in sorted(cloud_provider_files):
        module_name = _module_from_path(file_path)
        for line_number, imported_module in _collect_imports(file_path):
            if not _matches_prefixes(
                imported_module, _CLOUD_RUNNER_PROVIDER_FORBIDDEN_IMPORT_PREFIXES
            ):
                continue
            violations.append(
                _ImportMatch(
                    file_path=file_path,
                    module_name=module_name,
                    imported_module=imported_module,
                    line_number=line_number,
                )
            )
    return violations


def test_runtime_image_boundary_blocks_management_and_docker_imports() -> None:
    """Execution plane lock: runtime-image modules must not import management or Docker packages."""
    violations, _allowlisted = _runtime_image_violations(allow_temporary_exceptions=False)

    assert not violations, (
        "Runtime-image modules must not import management-only or Docker SDK modules. "
        "Known execution_plane blockers (nmap/nuclei semantic helper imports) should fail here "
        "until Phase 2 Task 2.1 removes them. Found:\n  - "
        + "\n  - ".join(sorted(item.format() for item in violations))
    )


def test_runtime_image_boundary_respects_only_explicit_temporary_exceptions() -> None:
    """Execution plane lock: runtime-image violations are blocked except explicit temporary exceptions."""
    strict_violations, _strict_allowlisted = _runtime_image_violations(
        allow_temporary_exceptions=False
    )
    violations, allowlisted = _runtime_image_violations(allow_temporary_exceptions=True)

    assert not violations, (
        "Runtime-image modules must not import management-only or Docker SDK modules "
        "outside explicit temporary exceptions. Found:\n  - "
        + "\n  - ".join(item.format() for item in violations)
    )
    if _load_manifest().temporary_exceptions and strict_violations:
        assert allowlisted, (
            "Expected explicit temporary exceptions when forbidden imports remain and "
            "manifest exceptions are configured."
        )


def test_runner_package_boundary_blocks_management_and_frontend_imports() -> None:
    """Execution plane lock: runner-package imports stay separated from management/frontend modules."""
    violations = _runner_package_violations()
    assert not violations, (
        "Runner-package modules must not import backend router/auth/model or frontend/server "
        "modules. Found:\n  - " + "\n  - ".join(item.format() for item in violations)
    )


def test_runner_package_manifest_roots_exist() -> None:
    """Execution plane lock: runner package roots listed in manifest must exist."""
    manifest = _load_manifest()
    missing_roots = [
        root for root in manifest.runner_package_roots if not (_REPO_ROOT / root).exists()
    ]
    assert not missing_roots, (
        "Runner package manifest includes roots that do not exist. Missing:\n  - "
        + "\n  - ".join(missing_roots)
    )


def test_runtime_image_dockerfile_does_not_copy_tool_or_agent_source() -> None:
    """Runtime image must not package backend-side tool/catalog source."""
    text = _RUNTIME_DOCKERFILE_PATH.read_text(encoding="utf-8")
    offenders = [
        source
        for source in _RUNTIME_IMAGE_FORBIDDEN_COPY_SOURCES
        if f"COPY {source}" in text or f"COPY ./{source}" in text
    ]

    assert not offenders, (
        "Runtime image must not copy backend-side agent/tool source. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_packaging_temporary_exceptions_are_explicit_and_todo_tagged() -> None:
    """Execution plane lock: packaging exceptions, when present, are future phase owned."""
    manifest = _load_manifest()
    for exception in manifest.temporary_exceptions:
        assert exception.todo.startswith("TODO(")
        assert exception.owner
        assert _FUTURE_OWNER_RE.fullmatch(exception.future_owner), (
            "Temporary exception must declare a future domain owner (tooling_plane+)."
        )
        assert "execution_plane" not in exception.todo
        assert "execution_plane" not in exception.owner
        assert "execution_plane" not in exception.removal_condition
        assert exception.removal_condition


def test_cloud_runner_provider_shell_blocks_local_docker_imports() -> None:
    """Remote runtime lock: cloud runner provider must not import execution-plane modules directly."""
    violations = _cloud_runner_provider_shell_violations()
    assert not violations, (
        "Cloud runner provider shell modules must not import local Docker/runtime execution-plane implementations. Found:\n  - "
        + "\n  - ".join(item.format() for item in violations)
    )
