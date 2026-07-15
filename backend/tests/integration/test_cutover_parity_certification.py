"""Cutover parity certification tests for managed-runtime coverage.

Responsibilities:
- Verify the cutover matrix is code-backed, complete, and reportable.
- Verify reused certification assets are inventoried in one gate.
- Verify the cutover certification runner exits zero when matrix coverage and reused targets pass.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

from backend.services.cutover.parity_matrix import (
    CutoverCertificationReport,
    CutoverParityMatrixRow,
    build_cutover_certification_report,
    build_cutover_parity_matrix,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_cutover_certification.py"


def _runner_test_inventory_paths() -> set[str]:
    """Return repo-relative runner package test paths expected in cutover reused inventory."""
    return {
        str(path.relative_to(_REPO_ROOT))
        for path in sorted((_REPO_ROOT / "tests" / "runner").glob("test_*.py"))
    }


_REQUIRED_WORKFLOWS = {
    "task create/start/status",
    "chat normal turn",
    "simple tool execution",
    "deep reasoning tool execution",
    "HITL approval/resume/retry/cancel",
    "terminal open/input/read/resize/close",
    "logs stream and snapshot",
    "metrics stream and snapshot",
    "VPN config/retry/status",
    "artifact manifest/upload/read/search",
    "file explorer tree/content/download/ZIP/search",
    "report read/write/export",
    "knowledge ingestion/projection/evidence read/replay",
    "stream replay after reconnect",
    "task delete/retention/export",
    "runner disconnect/reconnect",
    "duplicate runner messages",
    "stale runtime jobs",
    "partial artifact upload recovery",
    "capacity exhaustion and no-eligible-runner failure",
    "cross-tenant denial for all public surfaces",
}


_REQUIRED_REUSED_TARGET_PATHS = {
    "backend/tests/integration/test_runner_control_plane_integration.py",
    "backend/tests/integration/test_remote_runtime_terminal_proxy_dispatch_path.py",
    "backend/tests/integration/test_tooling_plane_integration.py",
    "backend/tests/integration/test_data_plane_certification.py",
    "backend/tests/integration/test_tenant_isolation_certification.py",
    "scripts/package_runner.py",
    "scripts/verify_runtime_package.py",
    "scripts/build_runtime_image.py",
} | _runner_test_inventory_paths()


def test_cutover_matrix_covers_all_phase1_workflows() -> None:
    """Every required cutover workflow has owner and test-target metadata."""
    rows = build_cutover_parity_matrix()
    workflows = {row.workflow for row in rows}

    assert workflows == _REQUIRED_WORKFLOWS
    assert all(row.owner.strip() for row in rows)
    assert all(row.test_target.strip() for row in rows)


def test_cutover_report_flags_blocking_workflows_missing_preset_coverage() -> None:
    """Blocking workflows must prove single-host + distributed coverage."""
    report = build_cutover_certification_report()
    payload = report.to_dict(repo_root=_REPO_ROOT)

    blocking_missing = payload["blocking_missing_workflows"]
    assert isinstance(blocking_missing, list)
    assert blocking_missing == [
        "chat normal turn",
        "logs stream and snapshot",
        "metrics stream and snapshot",
        "report read/write/export",
        "task delete/retention/export",
    ]


def test_cutover_report_flags_blocking_rows_with_core_or_preset_gaps() -> None:
    """Blocking rows cannot ignore missing core coverage or incomplete preset proof."""
    report = CutoverCertificationReport(
        generated_at="2026-01-01T00:00:00+00:00",
        matrix_rows=(
            CutoverParityMatrixRow(
                workflow="data-plane-gap",
                user_visible_behavior="Data-plane certification is missing.",
                local_dev_coverage="covered",
                managed_runtime_coverage="covered",
                single_host_deployment_preset_coverage="covered",
                distributed_deployment_preset_coverage="covered",
                data_plane_coverage="missing",
                tenant_security_coverage="covered",
                recovery_coverage="covered",
                owner="data-plane",
                test_target="backend/tests/integration/test_cutover_parity_certification.py",
                blocking=True,
            ),
            CutoverParityMatrixRow(
                workflow="preset-gap",
                user_visible_behavior="Deployment preset coverage is incomplete.",
                local_dev_coverage="covered",
                managed_runtime_coverage="covered",
                single_host_deployment_preset_coverage="partial",
                distributed_deployment_preset_coverage="covered",
                data_plane_coverage="covered",
                tenant_security_coverage="covered",
                recovery_coverage="covered",
                owner="runner-control",
                test_target="backend/tests/integration/test_cutover_parity_certification.py",
                blocking=True,
            ),
        ),
        reused_targets=(),
    )

    payload = report.to_dict(repo_root=_REPO_ROOT)
    assert payload["blocking_missing_workflows"] == ["data-plane-gap", "preset-gap"]


def test_cutover_reused_inventory_lists_existing_runner_control_to_tenant_isolation_and_packaging_targets() -> None:
    """Reused certification suites/scripts are listed in one gate."""
    report = build_cutover_certification_report()
    payload = report.to_dict(repo_root=_REPO_ROOT)
    paths = {item["path"] for item in payload["reused_targets"]}

    assert paths == _REQUIRED_REUSED_TARGET_PATHS
    assert all(item["exists"] is True for item in payload["reused_targets"])


def test_cutover_runner_quick_tier_fails_when_blocking_preset_gaps_exist() -> None:
    """Runner command fails when blocking deployment-preset proof is incomplete."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--tier", "quick"],
        cwd=str(_REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 1
    assert "blocking_missing_workflows" in output


def test_cutover_runner_run_targets_uses_active_python_interpreter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Reused target execution resolves python/pytest via the active interpreter."""
    spec = importlib.util.spec_from_file_location("cutover_cert_script", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    target = module.CutoverCertificationTarget(
        id="cutover-self-check",
        kind="pytest",
        path="backend/tests/integration/test_cutover_parity_certification.py",
        command="python -m pytest backend/tests/integration/test_cutover_parity_certification.py -q",
        tier="quick",
    )

    class _FakeReport:
        reused_targets = (target,)

        def to_dict(self, *, repo_root: Path) -> dict[str, object]:
            del repo_root
            return {
                "blocking_missing_workflows": [],
                "missing_reused_targets": [],
            }

        def to_json(self, *, repo_root: Path) -> str:
            del repo_root
            return "{}"

        def to_markdown(self, *, repo_root: Path) -> str:
            del repo_root
            return "# cutover\n"

    captured_commands: list[list[str]] = []

    def _fake_run(command, **kwargs):
        del kwargs
        captured_commands.append(command)
        return subprocess.CompletedProcess(command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(module, "build_cutover_certification_report", lambda: _FakeReport())
    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "run_cutover_certification.py",
            "--tier",
            "quick",
            "--run-targets",
            "--output-dir",
            str(tmp_path),
        ],
    )

    exit_code = module.main()
    assert exit_code == 0
    assert captured_commands
    assert captured_commands[0][0] == sys.executable
    assert captured_commands[0][1:3] == ["-m", "pytest"]
