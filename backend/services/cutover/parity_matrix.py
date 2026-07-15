"""Cutover certification parity matrix and deterministic certification report model.

This module declares workflow coverage, reused certification target inventory,
and report rendering helpers for cutover/runtime-path certification gates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

CoverageState = Literal["covered", "partial", "missing"]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNNER_TESTS_DIR = _REPO_ROOT / "tests" / "runner"


@dataclass(frozen=True)
class CutoverParityMatrixRow:
    """Single user-visible cutover certification workflow with coverage and ownership metadata."""

    workflow: str
    user_visible_behavior: str
    local_dev_coverage: CoverageState
    managed_runtime_coverage: CoverageState
    single_host_deployment_preset_coverage: CoverageState
    distributed_deployment_preset_coverage: CoverageState
    data_plane_coverage: CoverageState
    tenant_security_coverage: CoverageState
    recovery_coverage: CoverageState
    owner: str
    test_target: str
    blocking: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize a matrix row into a JSON-safe dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class CutoverCertificationTarget:
    """Existing test or script that is reused by the cutover certification certification gate."""

    id: str
    kind: Literal["pytest", "script"]
    path: str
    command: str
    tier: Literal["quick", "main", "both"] = "both"

    def exists(self, repo_root: Path) -> bool:
        """Return True when the referenced artifact exists in the repository."""
        return (repo_root / self.path).exists()

    def to_dict(self, repo_root: Path) -> dict[str, object]:
        """Serialize target inventory with existence status."""
        payload = asdict(self)
        payload["exists"] = self.exists(repo_root)
        return payload


@dataclass(frozen=True)
class CutoverCertificationReport:
    """Deterministic cutover certification report payload for scripts/tests and CI artifacts."""

    generated_at: str
    matrix_rows: tuple[CutoverParityMatrixRow, ...]
    reused_targets: tuple[CutoverCertificationTarget, ...]

    def to_dict(self, *, repo_root: Path) -> dict[str, object]:
        """Render a machine-readable dictionary report."""
        blocking_missing_workflows = [
            row.workflow
            for row in self.matrix_rows
            if row.blocking and _row_has_missing_required_coverage(row)
        ]
        missing_targets = [
            target.id for target in self.reused_targets if not target.exists(repo_root)
        ]
        return {
            "generated_at": self.generated_at,
            "summary": {
                "workflow_count": len(self.matrix_rows),
                "blocking_missing_workflows": len(blocking_missing_workflows),
                "missing_reused_targets": len(missing_targets),
            },
            "blocking_missing_workflows": blocking_missing_workflows,
            "missing_reused_targets": missing_targets,
            "matrix": [row.to_dict() for row in self.matrix_rows],
            "reused_targets": [target.to_dict(repo_root) for target in self.reused_targets],
        }

    def to_json(self, *, repo_root: Path) -> str:
        """Render JSON report text."""
        return json.dumps(self.to_dict(repo_root=repo_root), indent=2, sort_keys=True)

    def to_markdown(self, *, repo_root: Path) -> str:
        """Render Markdown report text."""
        payload = self.to_dict(repo_root=repo_root)
        lines = [
            "# Cutover Parity Certification Report",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- Workflow rows: `{payload['summary']['workflow_count']}`",
            (
                "- Blocking missing workflows: "
                f"`{payload['summary']['blocking_missing_workflows']}`"
            ),
            f"- Missing reused targets: `{payload['summary']['missing_reused_targets']}`",
            "",
            "## Matrix",
            "",
            (
                "| Workflow | Local Dev | Managed Runtime | Single-host Preset | "
                "Distributed Preset | Data plane | Tenant | Recovery | Owner | Test target | Blocking |"
            ),
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in self.matrix_rows:
            lines.append(
                "| "
                f"{row.workflow} | {row.local_dev_coverage} | {row.managed_runtime_coverage} | "
                f"{row.single_host_deployment_preset_coverage} | "
                f"{row.distributed_deployment_preset_coverage} | {row.data_plane_coverage} | "
                f"{row.tenant_security_coverage} | {row.recovery_coverage} | {row.owner} | "
                f"{row.test_target} | {'yes' if row.blocking else 'no'} |"
            )

        lines.extend(
            [
                "",
                "## Reused Targets",
                "",
                "| ID | Kind | Tier | Path | Exists |",
                "|---|---|---|---|---|",
            ]
        )
        for target in self.reused_targets:
            exists = "yes" if target.exists(repo_root) else "no"
            lines.append(
                f"| {target.id} | {target.kind} | {target.tier} | {target.path} | {exists} |"
            )

        return "\n".join(lines).rstrip() + "\n"


def _row_has_missing_required_coverage(row: CutoverParityMatrixRow) -> bool:
    """Return True when a blocking workflow row still has required coverage gaps."""
    has_missing_core_axis = any(
        state == "missing"
        for state in (
            row.local_dev_coverage,
            row.managed_runtime_coverage,
            row.data_plane_coverage,
            row.tenant_security_coverage,
            row.recovery_coverage,
        )
    )
    has_incomplete_deployment_preset_proof = any(
        state != "covered"
        for state in (
            row.single_host_deployment_preset_coverage,
            row.distributed_deployment_preset_coverage,
        )
    )
    return has_missing_core_axis or has_incomplete_deployment_preset_proof


def _runner_reused_targets() -> tuple[CutoverCertificationTarget, ...]:
    """Return explicit cutover certification reused targets for each runner package test module."""
    targets: list[CutoverCertificationTarget] = []
    for test_file in sorted(_RUNNER_TESTS_DIR.glob("test_*.py")):
        relative_path = str(test_file.relative_to(_REPO_ROOT))
        targets.append(
            CutoverCertificationTarget(
                id=f"runner-package-{test_file.stem}",
                kind="pytest",
                path=relative_path,
                command=f"python -m pytest {relative_path} -q",
                tier="main",
            )
        )
    return tuple(targets)


def get_cutover_reused_certification_targets() -> tuple[CutoverCertificationTarget, ...]:
    """Return the reused certification targets for the cutover certification runtime-path gate."""
    return (
        CutoverCertificationTarget(
            id="runner-control-plane-certification",
            kind="pytest",
            path="backend/tests/integration/test_runner_control_plane_integration.py",
            command="python -m pytest backend/tests/integration/test_runner_control_plane_integration.py -q",
        ),
        CutoverCertificationTarget(
            id="remote-runtime-terminal-proxy-certification",
            kind="pytest",
            path="backend/tests/integration/test_remote_runtime_terminal_proxy_dispatch_path.py",
            command="python -m pytest backend/tests/integration/test_remote_runtime_terminal_proxy_dispatch_path.py -q",
        ),
        CutoverCertificationTarget(
            id="tooling-plane-certification",
            kind="pytest",
            path="backend/tests/integration/test_tooling_plane_integration.py",
            command="python -m pytest backend/tests/integration/test_tooling_plane_integration.py -q",
        ),
        CutoverCertificationTarget(
            id="data-plane-certification",
            kind="pytest",
            path="backend/tests/integration/test_data_plane_certification.py",
            command="python -m pytest backend/tests/integration/test_data_plane_certification.py -q",
        ),
        CutoverCertificationTarget(
            id="tenant-isolation-certification",
            kind="pytest",
            path="backend/tests/integration/test_tenant_isolation_certification.py",
            command="python -m pytest backend/tests/integration/test_tenant_isolation_certification.py -q",
        ),
        *_runner_reused_targets(),
        CutoverCertificationTarget(
            id="runner-package-manifest-check",
            kind="script",
            path="scripts/package_runner.py",
            command="python scripts/package_runner.py --check",
        ),
        CutoverCertificationTarget(
            id="runtime-package-verify",
            kind="script",
            path="scripts/verify_runtime_package.py",
            command="python scripts/verify_runtime_package.py",
        ),
        CutoverCertificationTarget(
            id="runtime-image-boundary-check",
            kind="script",
            path="scripts/build_runtime_image.py",
            command="python scripts/build_runtime_image.py --check",
            tier="main",
        ),
    )


def build_cutover_parity_matrix() -> tuple[CutoverParityMatrixRow, ...]:
    """Return the typed cutover certification workflow matrix for managed-runtime certification."""
    row = CutoverParityMatrixRow
    return (
        row(
            workflow="task create/start/status",
            user_visible_behavior="Tasks start with stable status and runtime placement metadata.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="runtime-platform",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_task_lifecycle_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="chat normal turn",
            user_visible_behavior="Normal chat responses are stable under runner placement.",
            local_dev_coverage="covered",
            managed_runtime_coverage="partial",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="partial",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="chat-platform",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_chat_turn_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="simple tool execution",
            user_visible_behavior="Simple tool commands run and return normalized output.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="tooling-platform",
            test_target="backend/tests/integration/test_tooling_plane_integration.py",
            blocking=False,
        ),
        row(
            workflow="deep reasoning tool execution",
            user_visible_behavior="Deep-reasoning tool lanes preserve runner/tool boundaries.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="tooling-platform",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_deep_reasoning_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="HITL approval/resume/retry/cancel",
            user_visible_behavior="HITL lifecycle keeps task/tenant/runtime identity stable.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="chat-platform",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_hitl_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="terminal open/input/read/resize/close",
            user_visible_behavior="Terminal sessions are task-bound and interactive.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="runner-control",
            test_target="backend/tests/integration/test_remote_runtime_terminal_proxy_dispatch_path.py",
            blocking=False,
        ),
        row(
            workflow="logs stream and snapshot",
            user_visible_behavior="Log snapshots/streams preserve existing client payload shape.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="observability",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_logs_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="metrics stream and snapshot",
            user_visible_behavior="Metrics snapshots/streams preserve existing client payload shape.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="observability",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_metrics_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="VPN config/retry/status",
            user_visible_behavior="VPN status and retry flows keep stable error semantics.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="runtime-platform",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_vpn_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="artifact manifest/upload/read/search",
            user_visible_behavior="Artifacts are object-backed with manifest/upload/read/search parity.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="covered",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="data-plane",
            test_target="backend/tests/integration/test_data_plane_certification.py",
            blocking=False,
        ),
        row(
            workflow="file explorer tree/content/download/ZIP/search",
            user_visible_behavior="File explorer uses artifact/object-backed data instead of live workspace reads.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="covered",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="data-plane",
            test_target="backend/tests/integration/test_data_plane_certification.py",
            blocking=False,
        ),
        row(
            workflow="report read/write/export",
            user_visible_behavior="Report lifecycle remains tenant-safe and durable.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="reports",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_report_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="knowledge ingestion/projection/evidence read/replay",
            user_visible_behavior="Knowledge projections and evidence replay survive runtime cleanup.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="covered",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="knowledge",
            test_target="backend/tests/integration/test_data_plane_certification.py",
            blocking=False,
        ),
        row(
            workflow="stream replay after reconnect",
            user_visible_behavior="Reconnect can replay stream events without cross-task leakage.",
            local_dev_coverage="partial",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="streaming",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_stream_replay_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="task delete/retention/export",
            user_visible_behavior="Delete/retention/export stays durable and tenant-scoped.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="partial",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="partial",
            owner="data-plane",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_delete_retention_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="runner disconnect/reconnect",
            user_visible_behavior="Runner reconnect reconciles pending runtime jobs safely.",
            local_dev_coverage="partial",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="runner-control",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_runner_reconnect_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="duplicate runner messages",
            user_visible_behavior="Duplicate runner envelopes remain idempotent.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="covered",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="runner-control",
            test_target="backend/tests/integration/test_data_plane_certification.py",
            blocking=False,
        ),
        row(
            workflow="stale runtime jobs",
            user_visible_behavior="Stale runtime jobs are reconciled without cross-tenant impact.",
            local_dev_coverage="partial",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="runner-control",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_stale_runtime_job_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="partial artifact upload recovery",
            user_visible_behavior="Interrupted artifact uploads can recover without data corruption.",
            local_dev_coverage="partial",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="covered",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="data-plane",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_partial_upload_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="capacity exhaustion and no-eligible-runner failure",
            user_visible_behavior="Capacity failures surface stable, tenant-safe errors.",
            local_dev_coverage="partial",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="partial",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="runner-control",
            test_target="backend/tests/integration/test_cutover_parity_certification.py::test_capacity_inventory_gate",
            blocking=True,
        ),
        row(
            workflow="cross-tenant denial for all public surfaces",
            user_visible_behavior="Cross-tenant reads/writes fail closed across APIs and channels.",
            local_dev_coverage="covered",
            managed_runtime_coverage="covered",
            single_host_deployment_preset_coverage="covered",
            distributed_deployment_preset_coverage="covered",
            data_plane_coverage="covered",
            tenant_security_coverage="covered",
            recovery_coverage="covered",
            owner="tenant-platform",
            test_target="backend/tests/integration/test_tenant_isolation_certification.py",
            blocking=False,
        ),
    )


def build_cutover_certification_report() -> CutoverCertificationReport:
    """Build the current cutover certification matrix + reused-target inventory report model."""
    return CutoverCertificationReport(
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        matrix_rows=build_cutover_parity_matrix(),
        reused_targets=get_cutover_reused_certification_targets(),
    )


__all__ = [
    "CoverageState",
    "CutoverCertificationReport",
    "CutoverCertificationTarget",
    "CutoverParityMatrixRow",
    "build_cutover_certification_report",
    "build_cutover_parity_matrix",
    "get_cutover_reused_certification_targets",
]
