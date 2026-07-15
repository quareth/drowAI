"""Runner CLI app for managed control-plane startup and local diagnostics.

This module provides a backend-free process entrypoint with stable command exit
codes for managed runner startup, health, runtime-info, and cleanup operations.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner.configure import configure_runner_toml
from drowai_runner.control_channel.entrypoint import run_cloud_mode
from drowai_runner.config import RunnerConfig
from drowai_runner.control_channel.errors import RunnerCloudClientError
from drowai_runner.control_channel.terminal.pty_adapter import _RunnerPtyAdapter
from drowai_runner.credentials import mask_secret
from drowai_runner.control_channel.identity.persistence import (
    _load_runner_id_if_present,
    _load_runner_secret_if_present,
    _load_runner_tenant_id_if_present,
)
from drowai_runner.docker_runtime import (
    RunnerDockerRuntime,
    build_runner_container_name,
)
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.logging import configure_runner_logging
from drowai_runner.logs_metrics import RunnerLogsMetricsAdapter
from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.runtime_image import verify_runtime_info_payload
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.workspace import RunnerWorkspaceManager

EXIT_OK = 0
EXIT_INVALID_CONFIG = 2
EXIT_HEALTH_FAILED = 3
EXIT_RUNTIME_INFO_FAILED = 4
EXIT_CONFIGURE_FAILED = 5
EXIT_CLOUD_RUN_FAILED = 6
EXIT_CLEANUP_FAILED = 7
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Structured health report emitted by `drowai_runner health`."""

    status: str
    checks: dict[str, str]

    def exit_code(self) -> int:
        return EXIT_OK if self.status == "ok" else EXIT_HEALTH_FAILED


def build_parser() -> argparse.ArgumentParser:
    """Build the runner CLI parser."""
    parser = argparse.ArgumentParser(prog="drowai_runner")
    common_config_option_kwargs = {
        "type": Path,
        "help": "Path to runner TOML config. Falls back to environment variables.",
    }
    parser.add_argument("--config", default=None, **common_config_option_kwargs)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subcommand_config_option_kwargs = dict(common_config_option_kwargs)
    subcommand_config_option_kwargs["default"] = argparse.SUPPRESS
    run_parser = subparsers.add_parser("run", help="Start runner control-plane client.")
    run_parser.add_argument("--config", **subcommand_config_option_kwargs)
    health_parser = subparsers.add_parser("health", help="Run local runner health checks.")
    health_parser.add_argument("--config", **subcommand_config_option_kwargs)
    runtime_info_parser = subparsers.add_parser(
        "runtime-info",
        help="Print runtime image contract manifest from executor_daemon --runtime-info.",
    )
    runtime_info_parser.add_argument("--config", **subcommand_config_option_kwargs)
    configure_parser = subparsers.add_parser(
        "configure",
        help="Interactively write runner TOML config.",
    )
    configure_parser.add_argument("--config", type=Path, required=True, help="Path to write runner TOML.")
    configure_parser.add_argument("--control-plane-url", default=None)
    configure_parser.add_argument("--install-token", default=None)
    configure_parser.add_argument("--tenant-id", type=int, default=None)
    tls_group = configure_parser.add_mutually_exclusive_group()
    tls_group.add_argument("--tls-verify", dest="tls_verify", action="store_true", default=None)
    tls_group.add_argument("--no-tls-verify", dest="tls_verify", action="store_false")
    configure_parser.add_argument(
        "--allow-insecure-cloud-endpoint",
        action="store_true",
        default=None,
    )
    configure_parser.add_argument("--non-interactive", action="store_true")
    cleanup_parser = subparsers.add_parser(
        "cleanup-runtime",
        help="Retire runner-local runtime resources by runtime job or task id.",
    )
    cleanup_parser.add_argument("--config", **subcommand_config_option_kwargs)
    cleanup_target = cleanup_parser.add_mutually_exclusive_group(required=True)
    cleanup_target.add_argument("--runtime-job-id", default=None)
    cleanup_target.add_argument("--task-id", type=int, default=None)
    cleanup_parser.add_argument(
        "--tenant-id",
        default=os.getenv("DROWAI_RUNNER_TENANT_ID"),
        help="Tenant id used to find deterministic orphan containers when no job-store row exists.",
    )
    return parser


def load_config(config_path: Path | None) -> RunnerConfig:
    """Load and validate runner config from TOML or environment."""
    if config_path is None:
        return RunnerConfig.from_env()
    return RunnerConfig.from_toml(config_path)


def _masked_runner_log(config: RunnerConfig) -> str:
    control_plane_url = config.cloud_base_url or "<NO_URL>"
    credential_secret_path = (
        str(config.credential_secret_path) if config.credential_secret_path else "<NO_PATH>"
    )
    return (
        f"runner_root={config.runner_root} "
        f"runtime_image=<SET> "
        f"control_plane_url={control_plane_url} "
        f"registration_token={mask_secret(config.registration_token)} "
        f"credential_secret_path={credential_secret_path} "
        f"log_level={config.log_level}"
    )


def _docker_client_factory() -> object:
    import docker

    return docker.from_env()


def _docker_daemon_available() -> bool:
    try:
        _docker_client_factory().ping()
    except Exception:
        return False
    return True


def run_health(config: RunnerConfig) -> HealthReport:
    """Check config, workspace root readiness, and Docker daemon reachability."""
    checks: dict[str, str] = {"config": "ok"}

    try:
        config.runner_root.mkdir(parents=True, exist_ok=True)
        checks["workspace_root"] = "ok"
    except OSError:
        checks["workspace_root"] = "failed"
        return HealthReport(status="failed", checks=checks)

    checks["docker"] = "ok" if _docker_daemon_available() else "failed"
    if checks["docker"] != "ok":
        return HealthReport(status="failed", checks=checks)

    if config.cloud_base_url:
        runner_id = (config.runner_id or "").strip() or _load_runner_id_if_present(config)
        tenant_id = config.tenant_id or _load_runner_tenant_id_if_present(config)
        runner_secret = _load_runner_secret_if_present(config)
        checks["registration"] = "ok" if runner_id and tenant_id and runner_secret else "failed"
        if checks["registration"] != "ok":
            return HealthReport(status="failed", checks=checks)

    status = "ok" if all(state == "ok" for state in checks.values()) else "failed"
    return HealthReport(status=status, checks=checks)


def cleanup_runtime_command(config: RunnerConfig, args: argparse.Namespace) -> int:
    """Retire runner-local task runtime resources using the runner cleanup path."""
    workspace = RunnerWorkspaceManager(config.runner_root)
    workspace.initialize_runner_root()
    job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    docker_runtime = RunnerDockerRuntime(client_factory=_docker_client_factory)
    cleanup = RunnerCleanupService(
        workspace_manager=workspace,
        job_store=job_store,
        remove_container=lambda container_id: docker_runtime.remove_container(
            container_id,
            force=True,
        ),
        cleanup_retention_hours=config.cleanup_retention_hours,
        remove_orphan_network=docker_runtime.remove_orphan_task_network,
    )
    operation_service = RunnerOperationService(
        config=config,
        workspace=workspace,
        job_store=job_store,
        docker_runtime=docker_runtime,
        logs_metrics=RunnerLogsMetricsAdapter(
            job_store=job_store,
            docker_runtime=docker_runtime,
            workspace_manager=workspace,
        ),
        terminal_proxy=RunnerTerminalProxy(
            job_store=job_store,
            pty_adapter=_RunnerPtyAdapter(docker_runtime=docker_runtime),
        ),
        cleanup=cleanup,
    )

    target_runtime_job_id = str(args.runtime_job_id or "").strip()
    target_task_id = args.task_id
    jobs = []
    if target_runtime_job_id:
        job = job_store.find_job(target_runtime_job_id)
        if job is not None:
            jobs.append(job)
    elif target_task_id is not None:
        jobs = [job for job in job_store.list_jobs() if str(job.task_id) == str(target_task_id)]

    results: list[dict[str, object]] = []
    for job in jobs:
        result = operation_service.dispatch_operation(
            operation="retire_runtime",
            params={
                "runtime_job_id": job.runtime_job_id,
                "tenant_id": job.tenant_id,
                "task_id": job.task_id,
                "workspace_id": job.workspace_id,
            },
        )
        results.append(
            {
                "runtime_job_id": job.runtime_job_id,
                "task_id": job.task_id,
                "status": result.get("status"),
                "accepted": bool(result.get("accepted", result.get("status") == "succeeded")),
                "error_code": result.get("error_code"),
                "error_message": result.get("error_message"),
                "metadata": result.get("metadata"),
            }
        )

    if not jobs and target_task_id is not None:
        orphan_result = _cleanup_orphan_runtime_for_task(
            config=config,
            workspace=workspace,
            docker_runtime=docker_runtime,
            tenant_id=str(args.tenant_id or "").strip(),
            task_id=int(target_task_id),
        )
        results.append(orphan_result)

    ok = bool(results) and all(bool(item.get("accepted")) for item in results)
    if not results:
        results.append(
            {
                "accepted": False,
                "status": "failed",
                "error_code": "RUNNER_JOB_NOT_FOUND",
                "error_message": "No matching runner-local runtime job was found.",
            }
        )
    print(
        json.dumps(
            {
                "status": "ok" if ok else "failed",
                "results": results,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK if ok else EXIT_CLEANUP_FAILED


def _cleanup_orphan_runtime_for_task(
    *,
    config: RunnerConfig,
    workspace: RunnerWorkspaceManager,
    docker_runtime: RunnerDockerRuntime,
    tenant_id: str,
    task_id: int,
) -> dict[str, object]:
    """Best-effort cleanup for a deterministic task container with no job row."""
    if not tenant_id:
        return {
            "accepted": False,
            "status": "failed",
            "error_code": "TENANT_ID_REQUIRED",
            "error_message": "tenant_id is required to clean an orphan runtime by task id.",
        }
    container_name = build_runner_container_name(tenant_id=tenant_id, task_id=task_id)
    container_id = docker_runtime.find_container_id_by_name(container_name)
    container_removed = False
    if container_id:
        try:
            try:
                docker_runtime.stop_container(container_id)
            except Exception:
                pass
            docker_runtime.remove_container(container_id, force=True)
            container_removed = True
        except Exception as exc:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "ORPHAN_RUNTIME_REMOVE_FAILED",
                "error_message": str(exc),
                "container_name": container_name,
                "container_id": container_id,
            }
    network_removed = False
    try:
        network_removed = docker_runtime.remove_task_network(
            tenant_id=tenant_id,
            task_id=task_id,
            container_name=container_name,
            runtime_identity=container_name,
            pool_cidr=config.runtime_network_pool,
        )
    except Exception as exc:
        return {
            "accepted": False,
            "status": "failed",
            "error_code": "ORPHAN_NETWORK_REMOVE_FAILED",
            "error_message": str(exc),
            "container_name": container_name,
            "container_id": container_id,
            "container_removed": container_removed,
        }
    workspace_removed = False
    workspace_id = f"task-{task_id}"
    try:
        workspace_path = workspace.resolve_task_workspace(workspace_id)
        if workspace_path.exists():
            workspace.cleanup_task_workspace(workspace_id)
            workspace_removed = True
    except Exception as exc:
        return {
            "accepted": False,
            "status": "failed",
            "error_code": "ORPHAN_WORKSPACE_REMOVE_FAILED",
            "error_message": str(exc),
            "container_name": container_name,
            "container_id": container_id,
            "container_removed": container_removed,
        }
    return {
        "accepted": True,
        "status": "succeeded",
        "task_id": task_id,
        "workspace_id": workspace_id,
        "container_name": container_name,
        "container_id": container_id,
        "container_removed": container_removed,
        "network_removed": network_removed,
        "workspace_removed": workspace_removed,
        "idempotent": container_id is None and not workspace_removed,
    }


def runtime_info_command(config: RunnerConfig) -> int:
    """Print runtime image contract manifest from executor daemon probe."""
    try:
        client = _docker_client_factory()
        client.ping()
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "DOCKER_UNAVAILABLE",
                    "runtime_image_tag": config.runtime_image_tag,
                }
            )
        )
        return EXIT_RUNTIME_INFO_FAILED

    try:
        client.images.get(config.runtime_image_tag)
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "RUNTIME_IMAGE_MISSING",
                    "runtime_image_tag": config.runtime_image_tag,
                }
            )
        )
        return EXIT_RUNTIME_INFO_FAILED

    try:
        probe_output = client.containers.run(
            config.runtime_image_tag,
            command=[
                "/opt/drowai/runtime/python/executor_daemon.py",
                "--runtime-info",
            ],
            entrypoint="python3",
            remove=True,
            stderr=True,
            stdout=True,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "RUNTIME_INFO_PROBE_FAILED",
                    "runtime_image_tag": config.runtime_image_tag,
                    "error_message": str(exc),
                }
            )
        )
        return EXIT_RUNTIME_INFO_FAILED

    if isinstance(probe_output, bytes):
        probe_text = probe_output.decode("utf-8", errors="replace")
    else:
        probe_text = str(probe_output)
    try:
        payload = json.loads(probe_text)
    except json.JSONDecodeError:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "RUNTIME_INFO_INVALID_PAYLOAD",
                    "runtime_image_tag": config.runtime_image_tag,
                }
            )
        )
        return EXIT_RUNTIME_INFO_FAILED
    verification = verify_runtime_info_payload(payload)
    if not verification.ok:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "RUNTIME_CONTRACT_MISMATCH",
                    "runtime_image_tag": config.runtime_image_tag,
                    "mismatch_keys": list(verification.mismatch_keys),
                },
                sort_keys=True,
            )
        )
        return EXIT_RUNTIME_INFO_FAILED

    print(
        json.dumps(
            {
                "status": "ok",
                "runtime_image_tag": config.runtime_image_tag,
                "runtime_info": verification.payload,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def managed_run_command(config: RunnerConfig) -> int:
    """Run managed runner control-plane loop with deterministic error mapping."""
    try:
        logger.info("runner.cloud.start %s", _masked_runner_log(config))
        return run_cloud_mode(config)
    except RunnerCloudClientError as exc:
        logger.error(
            "runner.cloud.failed error_code=%s message=%s",
            exc.error_code,
            str(exc),
        )
        return EXIT_CLOUD_RUN_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "configure":
        try:
            path = configure_runner_toml(
                config_path=args.config,
                control_plane_url=args.control_plane_url,
                install_token=args.install_token,
                tenant_id=args.tenant_id,
                tls_verify=args.tls_verify,
                allow_insecure_cloud_endpoint=args.allow_insecure_cloud_endpoint,
                interactive=not args.non_interactive,
            )
        except (OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error_code": "CONFIGURE_FAILED",
                        "message": str(exc),
                    },
                    sort_keys=True,
                )
            )
            return EXIT_CONFIGURE_FAILED
        print(json.dumps({"status": "ok", "config": str(path)}, sort_keys=True))
        return EXIT_OK

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "INVALID_CONFIG",
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return EXIT_INVALID_CONFIG
    configure_runner_logging(
        config.log_level,
        log_file=config.runner_root / "logs" / "runner.log",
    )

    if args.command == "run":
        return managed_run_command(config)
    if args.command == "health":
        report = run_health(config)
        print(json.dumps({"status": report.status, "checks": report.checks}, sort_keys=True))
        return report.exit_code()
    if args.command == "runtime-info":
        return runtime_info_command(config)
    if args.command == "cleanup-runtime":
        return cleanup_runtime_command(config, args)

    parser.error(f"Unknown command: {args.command}")
    return EXIT_INVALID_CONFIG


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
