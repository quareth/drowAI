"""Runtime-path consolidation architecture tests.

Responsibilities:
- Lock product runtime selection to one managed runner provider.
- Fail closed when product runtime config still accepts standalone backend selection.
- Prevent standalone-provider imports from re-entering backend code or tests.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest

from backend.config.deployment_topology import get_deployment_profile_state
from backend.services.cutover.parity_matrix import (
    CutoverCertificationReport,
    CutoverParityMatrixRow,
    build_cutover_parity_matrix,
)
from backend.services.runtime_provider.registry import RuntimeProviderRegistry
from backend.services.runtime_provider.runner_provider_selection import (
    build_runner_runtime_provider,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNNER_PROVIDER_SELECTION_PATH = (
    _REPO_ROOT / "backend/services/runtime_provider/runner_provider_selection.py"
)
_PRODUCT_RUNTIME_POLICY_TEST_PATH = (
    _REPO_ROOT / "backend/tests/services/test_product_runtime_policy.py"
)


def _set_product_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", profile)
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "runner")
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "true")
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "local")


def _collect_standalone_provider_importers() -> set[str]:
    importers: set[str] = set()
    for py_file in sorted((_REPO_ROOT / "backend").rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("standalone_runner_provider"):
                    importers.add(str(py_file.relative_to(_REPO_ROOT)))
                    continue
                if any(
                    alias.name == "StandaloneRunnerRuntimeProvider" for alias in node.names
                ) and node.module.endswith("runtime_provider"):
                    importers.add(str(py_file.relative_to(_REPO_ROOT)))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("standalone_runner_provider"):
                        importers.add(str(py_file.relative_to(_REPO_ROOT)))
    return importers


@pytest.mark.parametrize("profile_name", ("single_host", "distributed"))
def test_runtime_path_consolidation_runtime_path_consolidation_product_profiles_share_managed_runner_provider(
    monkeypatch: pytest.MonkeyPatch,
    profile_name: str,
) -> None:
    """Runtime-path consolidation invariant: product profiles select one managed runner provider."""
    _set_product_runtime_env(monkeypatch, profile=profile_name)

    profile = get_deployment_profile_state()
    provider = build_runner_runtime_provider(
        cloud_runner_control_enabled=profile.cloud_runner_control_enabled,
    )

    assert provider.provider_name == "cloud_runner"


def test_runtime_path_consolidation_runtime_path_consolidation_runner_placement_resolves_managed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime-path consolidation invariant: runner placement resolves to managed provider semantics."""
    _set_product_runtime_env(
        monkeypatch,
        profile="distributed",
    )

    provider = RuntimeProviderRegistry(default_mode="runner").get_provider()

    assert provider.provider_name == "cloud_runner"


def test_runtime_path_consolidation_runtime_path_consolidation_standalone_imports_are_explicitly_scoped() -> None:
    """Runtime-path consolidation invariant: backend code and tests do not import standalone provider types."""
    importers = _collect_standalone_provider_importers()

    assert not importers, (
        "Standalone provider imports must be removed from backend code and tests. "
        "Unexpected importers:\n  - " + "\n  - ".join(sorted(importers))
    )


def test_runtime_path_consolidation_runtime_path_consolidation_product_provider_selection_has_no_standalone_import() -> None:
    """Runtime-path consolidation invariant: product provider selection does not import standalone provider."""
    text = _RUNNER_PROVIDER_SELECTION_PATH.read_text(encoding="utf-8")

    assert "standalone_runner_provider" not in text
    assert "StandaloneRunnerRuntimeProvider" not in text


def test_runtime_path_consolidation_product_policy_contract_does_not_construct_local_provider() -> None:
    """Runtime-path consolidation invariant: product policy tests reject local without local provider construction."""
    text = _PRODUCT_RUNTIME_POLICY_TEST_PATH.read_text(encoding="utf-8")

    assert "LocalDockerRuntimeProvider(" not in text
    assert (
        "from backend.services.runtime_provider.local_docker_provider import"
        not in text
    )


def test_runtime_path_consolidation_runtime_path_consolidation_certification_matrix_uses_managed_runtime_axis() -> None:
    """Runtime-path consolidation invariant: certification matrix uses managed runtime + deployment preset coverage."""
    row_fields = {field_.name for field_ in fields(CutoverParityMatrixRow)}

    assert "managed_runtime_coverage" in row_fields
    assert "single_host_deployment_preset_coverage" in row_fields
    assert "distributed_deployment_preset_coverage" in row_fields
    assert "standalone_runner_coverage" not in row_fields
    assert "cloud_runner_coverage" not in row_fields


def test_runtime_path_consolidation_runtime_path_consolidation_certification_blocking_requires_preset_coverage() -> None:
    """Runtime-path consolidation invariant: blocking workflows require covered single-host and distributed presets."""
    report = CutoverCertificationReport(
        generated_at="2026-01-01T00:00:00+00:00",
        matrix_rows=(
            CutoverParityMatrixRow(
                workflow="missing-presets",
                user_visible_behavior="Workflow is missing deployment-preset proof.",
                local_dev_coverage="covered",
                managed_runtime_coverage="covered",
                single_host_deployment_preset_coverage="missing",
                distributed_deployment_preset_coverage="missing",
                data_plane_coverage="covered",
                tenant_security_coverage="covered",
                recovery_coverage="covered",
                owner="runtime-platform",
                test_target="backend/tests/architecture/test_runtime_path_consolidation.py",
                blocking=True,
            ),
            CutoverParityMatrixRow(
                workflow="partial-single-host",
                user_visible_behavior="Workflow has incomplete single-host preset proof.",
                local_dev_coverage="covered",
                managed_runtime_coverage="covered",
                single_host_deployment_preset_coverage="partial",
                distributed_deployment_preset_coverage="covered",
                data_plane_coverage="covered",
                tenant_security_coverage="covered",
                recovery_coverage="covered",
                owner="runtime-platform",
                test_target="backend/tests/architecture/test_runtime_path_consolidation.py",
                blocking=True,
            ),
        ),
        reused_targets=(),
    )

    payload = report.to_dict(repo_root=_REPO_ROOT)
    assert payload["blocking_missing_workflows"] == [
        "missing-presets",
        "partial-single-host",
    ]


def test_runtime_path_consolidation_runtime_path_consolidation_certification_tracks_single_host_and_distributed_layouts() -> None:
    """Runtime-path consolidation invariant: each matrix row records both single-host and distributed layout coverage."""
    rows = build_cutover_parity_matrix()

    assert rows
    assert all(
        row.single_host_deployment_preset_coverage in {"covered", "partial", "missing"}
        and row.distributed_deployment_preset_coverage
        in {"covered", "partial", "missing"}
        for row in rows
    )
