"""FTP download tool with workspace-safe output and no artifact duplication."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base_tool import BaseTool, ToolPostprocessResult, ToolRuntimeOutputFile
from ..filesystem._platform import compute_checksums
from ..schemas import ToolResult
from .common import (
    FtpDownloadArgs,
    ServiceAccessToolMixin,
    bounded_preview,
    command_tool_result,
    container_output_path,
    ftp_runtime_output_files,
    ftp_workspace_directories,
    lftp_quote,
    service_metadata,
)


class FtpDownloadTool(ServiceAccessToolMixin, BaseTool):
    """Authenticate to FTP and download one remote file into the task workspace."""

    tool_id = "service_access.ftp_download"
    args_model = FtpDownloadArgs
    operation_name = "ftp_download"
    planner_guidance = "Use only to download one known FTP file with supplied credentials into /workspace."

    def build_command(self, args: FtpDownloadArgs) -> List[str]:
        output = container_output_path(args.output_path)
        overwrite_guard = "set xfer:clobber on" if args.overwrite else "set xfer:clobber off"
        commands = (
            f"set cmd:fail-exit yes; set net:timeout {args.timeout_seconds}; "
            f"{overwrite_guard}; get {lftp_quote(args.remote_path)} -o {lftp_quote(output)}; bye"
        )
        return [
            "lftp",
            "-u",
            f"{args.username},{args.password}",
            "-p",
            str(args.port),
            "-e",
            commands,
            args.host,
        ]

    def prepare_workspace_directories(self, args: FtpDownloadArgs):
        return ftp_workspace_directories(args)

    def runtime_output_files(
        self,
        args: FtpDownloadArgs,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[ToolRuntimeOutputFile]:
        _ = metadata
        return ftp_runtime_output_files(args)

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: FtpDownloadArgs) -> Dict[str, Any]:
        return service_metadata(
            operation=self.operation_name,
            host=args.host,
            port=args.port,
            username=args.username,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            extra={
                "remote_path": args.remote_path,
                "saved_path": args.output_path,
            },
        )

    def postprocess_execution(
        self,
        *,
        args: FtpDownloadArgs,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Optional[Any] = None,
    ) -> ToolPostprocessResult:
        result = super().postprocess_execution(
            args=args,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            success=success,
            metadata=metadata,
            artifacts=[],
            runtime_context=runtime_context,
        )
        result_metadata = dict(result.metadata or {})
        bytes_written: int | None = None
        sha256_hex: str | None = None
        errors: list[str] = []
        resolved_path = self._resolve_host_output_path(args, runtime_context)

        if result.success and resolved_path is not None:
            if not resolved_path.exists():
                errors.append("lftp reported success but destination file was not created")
            else:
                bytes_written = resolved_path.stat().st_size
                if args.min_bytes is not None and bytes_written < args.min_bytes:
                    errors.append(f"downloaded file is smaller than min_bytes ({bytes_written} < {args.min_bytes})")
                if args.max_bytes is not None and bytes_written > args.max_bytes:
                    errors.append(f"downloaded file exceeds max_bytes ({bytes_written} > {args.max_bytes})")
                try:
                    _, sha256_hex = compute_checksums(resolved_path)
                except Exception as exc:
                    errors.append(f"failed to compute sha256: {exc}")

        result_metadata.update(
            {
                "saved_path": args.output_path,
                "bytes_written": bytes_written,
                "sha256": sha256_hex,
            }
        )

        if errors:
            return ToolPostprocessResult(
                success=False,
                exit_code=3,
                stdout="",
                stderr=bounded_preview("\n".join([result.stderr, *errors]).strip()),
                metadata=result_metadata,
                artifacts=[],
            )

        if result.success:
            stdout_out = (
                f"Downloaded ftp://{args.host}:{args.port}{args.remote_path} -> {args.output_path}\n"
                f"bytes={bytes_written if bytes_written is not None else 'unknown'}\n"
                f"sha256={sha256_hex if sha256_hex else 'unavailable'}"
            )
            return ToolPostprocessResult(
                success=True,
                exit_code=result.exit_code,
                stdout=bounded_preview(stdout_out),
                stderr=result.stderr,
                metadata=result_metadata,
                artifacts=[],
            )

        return ToolPostprocessResult(
            success=result.success,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            metadata=result_metadata,
            artifacts=[],
        )

    def run(self, args: FtpDownloadArgs) -> ToolResult:
        return command_tool_result(tool=self, args=args, command=self.build_command(args), start=time.time())

    @staticmethod
    def _resolve_host_output_path(args: FtpDownloadArgs, runtime_context: Optional[Any]) -> Path | None:
        host_workspace = getattr(runtime_context, "host_workspace_path", None) if runtime_context else None
        if isinstance(host_workspace, str) and host_workspace.strip():
            return Path(host_workspace).resolve() / args.output_path
        return None
