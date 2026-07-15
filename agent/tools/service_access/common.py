"""Shared validation, redaction, and result helpers for service access tools.

This module keeps the FTP and SSH proof tools aligned: all commands are
finite, non-interactive, and return bounded redacted public surfaces.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from runtime_shared.workspace_files import RuntimeWorkspaceDirectory, normalize_workspace_relative_path

from ..base_tool import BaseTool, ToolPostprocessResult, ToolRuntimeOutputFile
from ..schemas import ToolResult

MAX_PREVIEW_CHARS = 4000
REDACTED_SECRET = "<redacted>"

_SECRET_PATTERNS = (
    re.compile(
        r"(?P<prefix>\bsshpass\s+-p\s+)(?P<secret>[^\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<prefix>(?<!\S)(?:--password|--pass|-p)\s+)(?P<secret>[^\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<prefix>(?<!\S)(?:--user|-u)\s+)(?P<user>[^,\s:]+)[,:](?P<secret>[^\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<key>password|passwd|pwd|token|secret)\b"
        r"(?P<sep>\s*[:=]\s*)(?P<quote>[\"']?)(?P<secret>[^\"'\s,;]+)(?P=quote)",
        re.IGNORECASE,
    ),
)

_AUTH_FAILURE_MARKERS = (
    "530",
    "login incorrect",
    "login failed",
    "authentication failed",
    "permission denied",
    "access denied",
    "invalid password",
)

_CONNECTION_FAILURE_MARKERS = (
    "connection refused",
    "connection timed out",
    "no route to host",
    "network is unreachable",
    "could not resolve",
    "name or service not known",
)


class ServiceAccessArgs(BaseModel):
    """Common password-authenticated remote service access arguments."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(..., min_length=1, max_length=255, description="Target host or IP.")
    port: int = Field(..., ge=1, le=65535, description="Target TCP port.")
    username: str = Field(..., min_length=1, max_length=256, description="Username to test.")
    password: str = Field(..., min_length=1, max_length=4096, description="Password to test.")
    timeout_seconds: int = Field(30, ge=1, le=120, description="Finite command timeout in seconds.")

    @field_validator("host", "username")
    @classmethod
    def validate_token(cls, value: str) -> str:
        """Reject option-like, whitespace-bearing, or control-character tokens."""
        token = str(value or "").strip()
        if not token:
            raise ValueError("value cannot be empty")
        if token.startswith("-"):
            raise ValueError("value cannot start with '-'")
        if any(ch.isspace() for ch in token):
            raise ValueError("value cannot contain whitespace")
        if any(ord(ch) < 32 for ch in token):
            raise ValueError("value cannot contain control characters")
        return token

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        """Reject control characters that would make non-interactive clients ambiguous."""
        text = str(value or "")
        if not text:
            raise ValueError("password cannot be empty")
        if any(ord(ch) < 32 for ch in text):
            raise ValueError("password cannot contain control characters")
        return text


class FtpListArgs(ServiceAccessArgs):
    """Arguments for FTP directory listing."""

    port: int = Field(21, ge=1, le=65535, description="FTP TCP port.")
    remote_path: str = Field("/", min_length=1, max_length=2048, description="Remote directory to list.")

    @field_validator("remote_path")
    @classmethod
    def validate_remote_path(cls, value: str) -> str:
        return validate_remote_path(value)


class FtpLoginArgs(ServiceAccessArgs):
    """Arguments for FTP login proof."""

    port: int = Field(21, ge=1, le=65535, description="FTP TCP port.")


class FtpDownloadArgs(ServiceAccessArgs):
    """Arguments for FTP file download into the task workspace."""

    port: int = Field(21, ge=1, le=65535, description="FTP TCP port.")
    remote_path: str = Field(..., min_length=1, max_length=2048, description="Remote file path to download.")
    output_path: str = Field(..., min_length=1, max_length=2048, description="Workspace-relative destination file.")
    overwrite: bool = Field(False, description="Allow replacing an existing destination file.")
    create_parents: bool = Field(False, description="Create destination parent directories when needed.")
    min_bytes: Optional[int] = Field(None, ge=0, description="Optional minimum downloaded size.")
    max_bytes: Optional[int] = Field(None, ge=0, description="Optional maximum downloaded size.")

    @field_validator("remote_path")
    @classmethod
    def validate_remote_path(cls, value: str) -> str:
        return validate_remote_path(value)

    @field_validator("output_path")
    @classmethod
    def validate_output_path(cls, value: str) -> str:
        return normalize_workspace_relative_path(value)

    @model_validator(mode="after")
    def validate_size_bounds(self) -> "FtpDownloadArgs":
        if (
            self.min_bytes is not None
            and self.max_bytes is not None
            and self.min_bytes > self.max_bytes
        ):
            raise ValueError("min_bytes cannot exceed max_bytes")
        return self


class SshLoginArgs(ServiceAccessArgs):
    """Arguments for SSH password login proof."""

    port: int = Field(22, ge=1, le=65535, description="SSH TCP port.")


def validate_remote_path(value: str) -> str:
    """Validate a remote FTP path without applying workspace semantics."""
    path = str(value or "").strip()
    if not path:
        raise ValueError("remote_path cannot be empty")
    if any(ord(ch) < 32 for ch in path):
        raise ValueError("remote_path cannot contain control characters")
    if "\n" in path or "\r" in path:
        raise ValueError("remote_path cannot contain newlines")
    return path


def lftp_quote(value: str) -> str:
    """Quote one value for lftp's command language, not for a shell."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "'\\''") + "'"


def redact_service_access_text(value: Any) -> str:
    """Return text with deterministic credential shapes redacted."""
    text = str(value or "")
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def bounded_preview(value: Any) -> str:
    """Return a bounded, redacted text preview."""
    text = redact_service_access_text(value)
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[:MAX_PREVIEW_CHARS] + "\n...[truncated]"


def classify_auth_success(exit_code: int, stdout: str, stderr: str) -> bool:
    """Return whether the service authentication proof succeeded."""
    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in _AUTH_FAILURE_MARKERS):
        return False
    return int(exit_code) == 0


def classify_failure_reason(exit_code: int, stdout: str, stderr: str) -> str | None:
    """Return a compact failure reason for metadata."""
    if int(exit_code) == 0:
        return None
    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in _AUTH_FAILURE_MARKERS):
        return "authentication_failed"
    if any(marker in combined for marker in _CONNECTION_FAILURE_MARKERS):
        return "connection_failed"
    return "command_failed"


def service_metadata(
    *,
    operation: str,
    host: str,
    port: int,
    username: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build stable redacted metadata for service access tool results."""
    auth_success = classify_auth_success(exit_code, stdout, stderr)
    metadata: Dict[str, Any] = {
        "operation": operation,
        "host": host,
        "port": int(port),
        "username": username,
        "auth_success": auth_success,
        "exit_code": int(exit_code),
        "failure_reason": classify_failure_reason(exit_code, stdout, stderr),
        "stdout_preview": bounded_preview(stdout),
        "stderr_preview": bounded_preview(stderr),
    }
    if extra:
        metadata.update(extra)
    return metadata


def workspace_parent_directory(path: str) -> str | None:
    """Return the workspace-relative parent directory for a file path."""
    parent = posixpath.dirname(str(path).replace("\\", "/").strip())
    if not parent or parent == ".":
        return None
    return parent


def container_output_path(relative_path: str) -> str:
    """Return the absolute in-container output path for a workspace file."""
    return f"/workspace/{normalize_workspace_relative_path(relative_path)}"


def command_tool_result(
    *,
    tool: BaseTool,
    args: BaseModel,
    command: List[str],
    start: float,
) -> ToolResult:
    """Execute a command locally for direct tool invocation paths."""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=getattr(args, "timeout_seconds", 30),
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        metadata = tool.parse_output(stdout, stderr, int(proc.returncode), args)
        post = tool.postprocess_execution(
            args=args,
            stdout=stdout,
            stderr=stderr,
            exit_code=int(proc.returncode),
            success=tool.is_success_exit_code(
                int(proc.returncode),
                args,
                stdout=stdout,
                stderr=stderr,
                parsed_metadata=metadata,
            ),
            metadata=metadata,
            artifacts=[],
        )
        return ToolResult(
            success=post.success,
            exit_code=post.exit_code,
            stdout=post.stdout,
            stderr=post.stderr,
            artifacts=post.artifacts,
            metadata=post.metadata,
            execution_time=time.time() - start,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr = stderr or f"Command timed out after {getattr(args, 'timeout_seconds', 30)} seconds"
        metadata = service_metadata(
            operation=getattr(tool, "operation_name", "service_access"),
            host=getattr(args, "host", ""),
            port=getattr(args, "port", 0),
            username=getattr(args, "username", ""),
            exit_code=-2,
            stdout=stdout,
            stderr=stderr,
        )
        metadata["timed_out"] = True
        return ToolResult(
            success=False,
            exit_code=-2,
            stdout=bounded_preview(stdout),
            stderr=bounded_preview(stderr),
            artifacts=[],
            metadata=metadata,
            execution_time=time.time() - start,
        )


class ServiceAccessToolMixin:
    """Shared postprocess behavior for login/list proof tools."""

    operation_name = "service_access"

    def render_result_output(self, args: BaseModel, stdout: str, stderr: str) -> tuple[str, str]:
        """Render bounded redacted command streams."""
        _ = args
        return bounded_preview(stdout), bounded_preview(stderr)

    def postprocess_execution(
        self,
        *,
        args: BaseModel,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Optional[Any] = None,
    ) -> ToolPostprocessResult:
        """Keep public streams redacted and treat auth failure as a normal failure."""
        _ = runtime_context
        auth_success = bool((metadata or {}).get("auth_success"))
        return ToolPostprocessResult(
            success=bool(success and auth_success),
            exit_code=int(exit_code),
            stdout=bounded_preview(stdout),
            stderr=bounded_preview(stderr),
            metadata=dict(metadata or {}),
            artifacts=[],
        )

    def create_artifacts(self, stdout: str, args: BaseModel, timestamp: Optional[int] = None) -> List[str]:
        """Service access tools do not create artifact duplicates."""
        _ = stdout, args, timestamp
        return []


def ftp_workspace_directories(args: FtpDownloadArgs) -> List[RuntimeWorkspaceDirectory]:
    """Declare FTP download output directories using http_download semantics."""
    parent = workspace_parent_directory(args.output_path) if args.create_parents else None
    if not parent:
        return []
    return [
        RuntimeWorkspaceDirectory(
            relative_path=parent,
            description="ftp_download runtime output directory",
        )
    ]


def ftp_runtime_output_files(args: FtpDownloadArgs) -> List[ToolRuntimeOutputFile]:
    """Declare the FTP downloaded file as the primary workspace output."""
    return [
        ToolRuntimeOutputFile(
            relative_path=args.output_path,
            description="ftp_download destination file",
            required=True,
            min_bytes=args.min_bytes,
            max_bytes=args.max_bytes,
        )
    ]


def _redact_match(match: re.Match[str]) -> str:
    groups = match.groupdict()
    if "key" in groups and groups.get("key"):
        quote = groups.get("quote") or ""
        return f"{groups['key']}{groups.get('sep', '')}{quote}{REDACTED_SECRET}{quote}"
    if groups.get("user"):
        return f"{groups.get('prefix', '')}{groups['user']}:{REDACTED_SECRET}"
    return f"{groups.get('prefix', '')}{REDACTED_SECRET}"

