"""Cutover production production cutover architecture boundary tests.

Responsibilities:
- Lock production-profile guardrails to runner/cloud/data-plane prerequisites.
- Keep local Docker provider usage constrained to explicit dev compatibility paths.
- Ensure docs and provider boundaries do not regress to production DinD guidance.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.config.feature_flags import (
    get_default_task_runtime_placement_mode,
    is_cloud_runner_control_enabled,
    is_runner_tool_command_enabled,
)
from backend.services.runtime_provider.registry import (
    RuntimeProviderRegistry,
    resolve_task_runtime_placement_mode,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLOUD_PROVIDER_PATH = _REPO_ROOT / "backend/services/runtime_provider/cloud_runner_provider.py"
_CLOUD_ARTIFACT_OPERATIONS_PATH = (
    _REPO_ROOT / "backend/services/runtime_provider/cloud_runner/operations/artifact.py"
)
_DOCKER_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_DEPLOYMENT_DOC_PATH = _REPO_ROOT / "docs/architecture/deployment.md"
_PRODUCT_ROUTER_ROOT = _REPO_ROOT / "backend/routers"
_RUNNER_PACKAGE_ROOT = _REPO_ROOT / "drowai_runner"
_RUNTIME_SHARED_ROOT = _REPO_ROOT / "runtime_shared"

_DIAGNOSTIC_LOCAL_USAGE_ALLOWLIST = {
    "backend/services/runtime_provider/registry.py": (
        "lazy local provider construction for explicit dev/test/diagnostic placement"
    ),
    "backend/tests/services/runtime_provider/test_local_docker_provider.py": (
        "local provider unit coverage"
    ),
}
_ALLOWED_LOCAL_PROVIDER_IMPORTERS = set(_DIAGNOSTIC_LOCAL_USAGE_ALLOWLIST)

_FORBIDDEN_PRODUCT_LOCAL_IMPORT_MODULES = {
    "backend.services.runtime_provider.local_docker_provider",
    "backend.services.unified_docker_service",
}
_FORBIDDEN_PRODUCT_LOCAL_IMPORT_NAMES = {
    "LocalDockerRuntimeProvider",
    "local_docker_provider",
    "unified_docker_service",
}


@dataclass(frozen=True)
class _ProductionCutoverFlags:
    placement_mode: str
    cloud_runner_control_enabled: bool
    runner_tool_command_enabled: bool


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_async_function_body(source: str, func_name: str) -> str:
    start = source.index(f"async def {func_name}(")
    next_def = source.find("\n    async def ", start + 1)
    if next_def < 0:
        next_def = source.find("\n    def ", start + 1)
    if next_def < 0:
        return source[start:]
    return source[start:next_def]


def _collect_local_provider_importers() -> set[str]:
    importers: set[str] = set()
    for py_file in sorted((_REPO_ROOT / "backend").rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                if module.endswith("local_docker_provider"):
                    importers.add(str(py_file.relative_to(_REPO_ROOT)))
                    continue
                for alias in node.names:
                    if alias.name == "local_docker_provider":
                        importers.add(str(py_file.relative_to(_REPO_ROOT)))
                        break
                    if f"{module}.{alias.name}".endswith("local_docker_provider"):
                        importers.add(str(py_file.relative_to(_REPO_ROOT)))
                        break
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("local_docker_provider"):
                        importers.add(str(py_file.relative_to(_REPO_ROOT)))
    return importers


def _collect_direct_local_runtime_imports(root: Path) -> list[str]:
    violations: list[str] = []
    for py_file in sorted(root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        rel_path = py_file.relative_to(_REPO_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.name
                    if imported in _FORBIDDEN_PRODUCT_LOCAL_IMPORT_MODULES or imported.endswith(
                        (".local_docker_provider", ".unified_docker_service")
                    ):
                        violations.append(f"{rel_path}:{node.lineno}: import `{imported}`")
            elif isinstance(node, ast.ImportFrom):
                if node.level != 0 or not node.module:
                    continue
                module = node.module
                imported_names = {alias.name for alias in node.names}
                if module in _FORBIDDEN_PRODUCT_LOCAL_IMPORT_MODULES or module.endswith(
                    (".local_docker_provider", ".unified_docker_service")
                ):
                    violations.append(
                        f"{rel_path}:{node.lineno}: from `{module}` import "
                        + ", ".join(sorted(imported_names))
                    )
                    continue
                if (
                    module == "backend.services.runtime_provider"
                    and imported_names & _FORBIDDEN_PRODUCT_LOCAL_IMPORT_NAMES
                ) or (
                    module == "backend.services"
                    and "unified_docker_service" in imported_names
                ):
                    violations.append(
                        f"{rel_path}:{node.lineno}: from `{module}` import "
                        + ", ".join(sorted(imported_names))
                    )
    return violations


def _read_production_flags_from_env() -> _ProductionCutoverFlags:
    return _ProductionCutoverFlags(
        placement_mode=get_default_task_runtime_placement_mode(),
        cloud_runner_control_enabled=is_cloud_runner_control_enabled(),
        runner_tool_command_enabled=is_runner_tool_command_enabled(),
    )


def _collect_backend_import_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for py_file in sorted(root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "backend" or alias.name.startswith("backend."):
                        violations.append(
                            f"{py_file.relative_to(_REPO_ROOT)}:{node.lineno}: import `{alias.name}`"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level != 0 or not node.module:
                    continue
                if node.module == "backend" or node.module.startswith("backend."):
                    violations.append(
                        f"{py_file.relative_to(_REPO_ROOT)}:{node.lineno}: from `{node.module}` import ..."
                    )
    return violations


def _assert_production_cutover_flags(flags: _ProductionCutoverFlags) -> None:
    assert flags.placement_mode == "runner", (
        "Production cutover profile must require TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner."
    )
    assert flags.cloud_runner_control_enabled, (
        "Production cutover profile must require ENABLE_CLOUD_RUNNER_CONTROL=true."
    )
    assert flags.runner_tool_command_enabled, (
        "Production cutover profile must require RUNNER_TOOL_COMMAND_ENABLED=true."
    )


def _set_production_cutover_env(monkeypatch) -> None:
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "runner")
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "true")


def test_production_profile_flags_fail_closed_against_local_defaults(
    monkeypatch,
) -> None:
    """Cutover production lock: production profile cannot silently fall back to local Docker."""
    _set_production_cutover_env(monkeypatch)
    _assert_production_cutover_flags(_read_production_flags_from_env())


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        (
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
            "local",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
        ),
    ),
)
def test_production_profile_flag_guardrail_rejects_local_defaults(
    monkeypatch,
    name: str,
    value: str,
    message: str,
) -> None:
    """Cutover production lock: production guardrail rejects local placement."""
    _set_production_cutover_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(AssertionError, match=message):
        _assert_production_cutover_flags(_read_production_flags_from_env())


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        (
            "ENABLE_CLOUD_RUNNER_CONTROL",
            "false",
            "ENABLE_CLOUD_RUNNER_CONTROL=true",
        ),
        (
            "RUNNER_TOOL_COMMAND_ENABLED",
            "false",
            "RUNNER_TOOL_COMMAND_ENABLED=true",
        ),
    ),
)
def test_production_profile_flag_guardrail_rejects_required_boolean_cutover_flags(
    monkeypatch,
    name: str,
    value: str,
    message: str,
) -> None:
    """Cutover production lock: production guardrail rejects disabled runner/data-plane cutover booleans."""
    _set_production_cutover_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(AssertionError, match=message):
        _assert_production_cutover_flags(_read_production_flags_from_env())


def test_registry_in_production_mode_uses_real_default_selection_chain(monkeypatch) -> None:
    """Cutover production lock: production env must resolve runner/cloud without forcing registry defaults."""
    _set_production_cutover_env(monkeypatch)
    calls: dict[str, int] = {"local": 0, "runner": 0}

    class _Provider:
        provider_name = "stub"

    def _build_local() -> _Provider:
        calls["local"] += 1
        return _Provider()

    def _build_runner() -> _Provider:
        calls["runner"] += 1
        return _Provider()

    registry = RuntimeProviderRegistry(
        local_provider_factory=_build_local,
        runner_provider_factory=_build_runner,
    )

    resolved_mode = resolve_task_runtime_placement_mode(task=object())
    registry.get_provider()

    assert resolved_mode.value == "runner"
    assert calls["runner"] == 1
    assert calls["local"] == 0, "Production runner default must not instantiate local provider."


def test_product_routers_do_not_import_direct_local_runtime_authority() -> None:
    """Cutover production lock: product routes cannot bypass runner/runtime-provider dispatch."""
    violations = _collect_direct_local_runtime_imports(_PRODUCT_ROUTER_ROOT)

    assert not violations, (
        "Product routers must not import LocalDockerRuntimeProvider or "
        "unified_docker_service directly. Found:\n  - "
        + "\n  - ".join(violations)
    )


def test_diagnostic_local_provider_allowlist_is_explicit_and_current() -> None:
    """Cutover production lock: diagnostic local provider imports are named exceptions."""
    importers = _collect_local_provider_importers()
    disallowed = sorted(importers - _ALLOWED_LOCAL_PROVIDER_IMPORTERS)
    stale_allowlist = sorted(_ALLOWED_LOCAL_PROVIDER_IMPORTERS - importers)

    assert not disallowed, (
        "LocalDockerRuntimeProvider import escaped provider/test/diagnostic "
        "boundaries:\n  - " + "\n  - ".join(disallowed)
    )
    assert not stale_allowlist, (
        "Diagnostic local provider allowlist contains stale entries:\n  - "
        + "\n  - ".join(
            f"{path} ({_DIAGNOSTIC_LOCAL_USAGE_ALLOWLIST[path]})"
            for path in stale_allowlist
        )
    )


def test_local_docker_provider_imports_are_constrained_to_allowlist() -> None:
    """Cutover production lock: local Docker provider imports stay in explicit compatibility modules."""
    importers = _collect_local_provider_importers()
    disallowed = sorted(importers - _ALLOWED_LOCAL_PROVIDER_IMPORTERS)
    assert not disallowed, (
        "LocalDockerRuntimeProvider import escaped compatibility boundaries:\n  - "
        + "\n  - ".join(disallowed)
    )


def test_cloud_runner_provider_dispatches_live_workspace_reads() -> None:
    """Production lock: cloud provider reads live workspace files through runner-control."""
    provider_text = _read_text(_CLOUD_PROVIDER_PATH)
    artifact_text = _read_text(_CLOUD_ARTIFACT_OPERATIONS_PATH)
    provider_read_body = _extract_async_function_body(
        provider_text, "read_runtime_artifact_file"
    )
    provider_query_body = _extract_async_function_body(
        provider_text, "query_runtime_artifacts"
    )
    read_body = _extract_async_function_body(artifact_text, "read_runtime_artifact_file")
    query_body = _extract_async_function_body(artifact_text, "query_runtime_artifacts")

    assert "self._artifact.read_runtime_artifact_file(request)" in provider_read_body
    assert "self._artifact.query_runtime_artifacts(request)" in provider_query_body
    assert "self._deferred_result(" not in read_body
    assert '"read_runtime_artifact_file"' in read_body
    assert "RunnerMessageType.RUNTIME_WORKSPACE_READ" in read_body
    assert "self._operation_waiter._wait_for_runtime_operation_result(" in read_body

    assert "self._deferred_result(" not in query_body
    assert '"query_runtime_artifacts"' in query_body
    assert "RunnerMessageType.RUNTIME_WORKSPACE_QUERY" in query_body
    assert "self._operation_waiter._wait_for_runtime_operation_result(" in query_body


def test_runner_packages_remain_backend_free() -> None:
    """Cutover production lock: runner-side packages must not import backend modules."""
    violations = _collect_backend_import_violations(_RUNNER_PACKAGE_ROOT)
    violations.extend(_collect_backend_import_violations(_RUNTIME_SHARED_ROOT))
    assert not violations, (
        "Runner/runtime shared packages must stay backend-free. Found:\n  - "
        + "\n  - ".join(violations)
    )


def test_root_docker_compose_file_is_removed() -> None:
    """Cutover production lock: legacy DinD docker-compose must not ship as a deployment entrypoint."""
    assert not _DOCKER_COMPOSE_PATH.exists(), (
        "Root docker-compose.yml must be removed; use deploy/compose profiles or local dev launchers."
    )


def test_production_deployment_doc_does_not_list_dind_as_deployment_path() -> None:
    """Cutover production lock: deployment docs must not describe DinD as an active deployment path."""
    text = _read_text(_DEPLOYMENT_DOC_PATH).lower()
    assert "docker-in-docker" not in text
    assert "docker-compose.yml" not in text
    assert "dind" not in text
