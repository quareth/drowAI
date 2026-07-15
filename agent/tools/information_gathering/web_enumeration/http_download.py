"""HTTP download tool with workspace-safe output and integrity checks.

This module provides `information_gathering.web_enumeration.http_download`.
Responsibilities:
- build curl argv commands for deterministic file downloads,
- enforce workspace-relative destination safety,
- apply overwrite/resume semantics consistently,
- verify file integrity (size bounds + optional SHA-256),
- report the saved workspace file through structured metadata without creating artifacts.
"""

from __future__ import annotations

import json
import posixpath
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...base_tool import BaseTool, ToolPostprocessResult, ToolRuntimeOutputFile
from ...filesystem._helpers import resolve_workspace_path_safe, to_workspace_relative, workspace_root
from ...filesystem._platform import compute_checksums
from ...schemas import ToolResult
from .contracts import HttpDownloadArgs
from runtime_shared.workspace_files import (
    RuntimeWorkspaceDirectory,
    materialize_runtime_workspace_preparation,
)
from ._helpers import (
    UnsupportedHttpVersionError,
    build_auth_curl_args,
    build_connection_control_curl_args,
    build_curl_common_args,
    build_http_version_curl_args,
    build_retry_rate_curl_args,
    build_session_curl_args,
    build_tls_curl_args,
    detect_curl_http_capabilities,
    redact_text_secrets,
    redact_url_credentials,
)

_WRITE_OUT_MARKER = "__DROWAI_HTTP_DOWNLOAD_META__"


def _workspace_parent_directory(path: Optional[str]) -> Optional[str]:
    """Return a workspace-relative parent directory for a relative file path."""

    if not path:
        return None
    parent = posixpath.dirname(str(path).replace("\\", "/").strip())
    if not parent or parent == ".":
        return None
    return parent


def _extract_write_out(stdout_text: str) -> Tuple[str, Dict[str, Any]]:
    """Extract curl --write-out trailer metadata from stdout text."""
    marker_idx = stdout_text.rfind(f"\n{_WRITE_OUT_MARKER}")
    if marker_idx == -1:
        marker_idx = stdout_text.rfind(_WRITE_OUT_MARKER)
        if marker_idx == -1:
            return stdout_text, {}

    payload = stdout_text[marker_idx:].strip()
    if payload.startswith(_WRITE_OUT_MARKER):
        payload = payload[len(_WRITE_OUT_MARKER):]
    payload = payload.strip()

    output_without_trailer = stdout_text[:marker_idx].rstrip("\n")
    if payload.startswith("{"):
        try:
            raw_json = json.loads(payload)
        except json.JSONDecodeError:
            raw_json = None
        if isinstance(raw_json, dict):
            parsed: Dict[str, Any] = {}
            http_code = raw_json.get("http_code") or raw_json.get("response_code")
            try:
                parsed["http_code"] = int(http_code)
            except Exception:
                pass
            effective_url = raw_json.get("url_effective")
            parsed["effective_url"] = str(effective_url) if effective_url else None
            try:
                parsed["size_download"] = int(float(raw_json.get("size_download")))
            except Exception:
                pass
            try:
                parsed["num_redirects"] = int(raw_json.get("num_redirects"))
            except Exception:
                pass
            try:
                parsed["timing_ms"] = int(float(raw_json.get("time_total")) * 1000)
            except Exception:
                pass
            return output_without_trailer, parsed

    parts = payload.split("\t")
    if len(parts) != 5:
        return output_without_trailer, {}

    http_code_raw, effective_url, size_download, num_redirects, time_total = parts
    parsed: Dict[str, Any] = {}
    try:
        parsed["http_code"] = int(http_code_raw)
    except Exception:
        pass
    parsed["effective_url"] = effective_url or None
    try:
        parsed["size_download"] = int(float(size_download))
    except Exception:
        pass
    try:
        parsed["num_redirects"] = int(num_redirects)
    except Exception:
        pass
    try:
        parsed["timing_ms"] = int(float(time_total) * 1000)
    except Exception:
        pass

    return output_without_trailer, parsed


def _runtime_output_metadata_for_path(value: Any, relative_path: str) -> Optional[Dict[str, Any]]:
    """Return generic runtime-output metadata for a workspace-relative path."""
    if not isinstance(value, list):
        return None
    normalized = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    for item in value:
        if not isinstance(item, dict):
            continue
        item_path = str(item.get("relative_path") or "").strip().replace("\\", "/").lstrip("/")
        if item_path == normalized:
            return dict(item)
    return None


class HttpDownloadTool(BaseTool):
    """Download an HTTP resource into the workspace with integrity guards."""

    args_model = HttpDownloadArgs

    def __init__(self) -> None:
        super().__init__()
        self._workspace_root: Optional[Path] = None
        self._resolved_output_path: Optional[Path] = None
        self._resolved_output_relative: Optional[str] = None
        self._preexisting_size_bytes: int = 0
        self._resume_mode_used: bool = False
        self._cookies_persisted: bool = False
        self._session_cookie_source: Optional[str] = None
        self._auth_mode_used: str = "none"
        self._mtls_used: bool = False
        self._ca_cert_used: bool = False
        self._connection_controls_applied: Dict[str, Any] = {}
        self._transfer_controls_applied: Dict[str, Any] = {}
        self._retry_config_applied: Dict[str, Any] = {}
        self._dump_headers_artifact_enabled: bool = False
        self._dump_headers_path: Optional[str] = None
        self._trace_mode: str = "none"
        self._trace_artifact_path: Optional[str] = None
        self._http_version_requested: str = "auto"
        self._http_version_applied: str = "auto"
        self._curl_capabilities: Dict[str, Any] = {}

    def _prepare_output_path(self, args: HttpDownloadArgs) -> Path:
        """Resolve and validate output destination inside workspace boundaries."""
        workspace = workspace_root()
        resolved_output = resolve_workspace_path_safe(args.output_path, workspace=workspace)
        if resolved_output.exists() and resolved_output.is_dir():
            raise ValueError("output_path must point to a file, not a directory")

        parent = resolved_output.parent
        if not parent.exists():
            if args.create_parents:
                pass
            else:
                raise ValueError("output_path parent directory does not exist; set create_parents=true")

        preexisting_size = 0
        if resolved_output.exists():
            preexisting_size = resolved_output.stat().st_size
            if args.resume:
                pass
            elif not args.overwrite:
                raise ValueError("output_path already exists and overwrite=false")
        elif args.resume:
            preexisting_size = 0

        if args.resume and args.overwrite:
            raise ValueError("resume=true cannot be combined with overwrite=true")

        self._workspace_root = workspace
        self._resolved_output_path = resolved_output
        self._resolved_output_relative = to_workspace_relative(resolved_output, workspace)
        self._preexisting_size_bytes = preexisting_size
        self._resume_mode_used = bool(args.resume and preexisting_size > 0)
        return resolved_output

    def _resolve_session_paths(self, args: HttpDownloadArgs) -> Tuple[Optional[str], Optional[str]]:
        """Resolve cookie file/jar paths inside workspace boundaries."""
        workspace = self._workspace_root or workspace_root()
        cookie_file_relative: Optional[str] = None
        cookie_jar_relative: Optional[str] = None

        if args.cookie_file:
            resolved_cookie_file = resolve_workspace_path_safe(args.cookie_file, workspace=workspace)
            if not resolved_cookie_file.exists() or not resolved_cookie_file.is_file():
                raise ValueError("cookie_file must point to an existing file inside workspace")
            cookie_file_relative = to_workspace_relative(resolved_cookie_file, workspace)

        if args.cookie_jar:
            resolved_cookie_jar = resolve_workspace_path_safe(args.cookie_jar, workspace=workspace)
            if resolved_cookie_jar.exists() and resolved_cookie_jar.is_dir():
                raise ValueError("cookie_jar must point to a file path, not a directory")
            cookie_jar_relative = to_workspace_relative(resolved_cookie_jar, workspace)

        return cookie_file_relative, cookie_jar_relative

    def _resolve_tls_paths(
        self,
        args: HttpDownloadArgs,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve mTLS certificate/key/CA paths into workspace-relative safe paths."""
        if not (args.client_cert_path or args.client_key_path or args.ca_cert_path):
            return None, None, None

        workspace = self._workspace_root or workspace_root()
        cert_relative: Optional[str] = None
        key_relative: Optional[str] = None
        ca_relative: Optional[str] = None

        if args.client_cert_path:
            cert_path = resolve_workspace_path_safe(args.client_cert_path, workspace=workspace)
            if not cert_path.exists() or not cert_path.is_file():
                raise ValueError("client_cert_path must point to an existing file inside workspace")
            cert_relative = to_workspace_relative(cert_path, workspace)
        if args.client_key_path:
            key_path = resolve_workspace_path_safe(args.client_key_path, workspace=workspace)
            if not key_path.exists() or not key_path.is_file():
                raise ValueError("client_key_path must point to an existing file inside workspace")
            key_relative = to_workspace_relative(key_path, workspace)
        if args.ca_cert_path:
            ca_path = resolve_workspace_path_safe(args.ca_cert_path, workspace=workspace)
            if not ca_path.exists() or not ca_path.is_file():
                raise ValueError("ca_cert_path must point to an existing file inside workspace")
            ca_relative = to_workspace_relative(ca_path, workspace)

        return cert_relative, key_relative, ca_relative

    def _resolve_postprocess_output_path(self, runtime_context: Optional[Any]) -> Optional[Path]:
        """Resolve absolute host path for postprocess integrity checks."""
        workspace_candidates: List[Path] = []
        host_workspace = getattr(runtime_context, "host_workspace_path", None) if runtime_context else None
        if isinstance(host_workspace, str) and host_workspace.strip():
            workspace_candidates.append(Path(host_workspace).resolve())
        if self._workspace_root is not None:
            workspace_candidates.append(self._workspace_root)
        runtime_workspace = getattr(runtime_context, "workspace_path", None) if runtime_context else None
        if isinstance(runtime_workspace, str) and runtime_workspace.strip():
            workspace_candidates.append(Path(runtime_workspace).resolve())
        try:
            workspace_candidates.append(workspace_root())
        except Exception:
            pass
        workspace_candidates.append(Path.cwd().resolve())

        relative_output = self._resolved_output_relative
        if relative_output:
            resolved_candidates: List[Path] = []
            seen: set[str] = set()
            for workspace in workspace_candidates:
                key = str(workspace)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    candidate = resolve_workspace_path_safe(relative_output, workspace=workspace)
                except Exception:
                    continue
                if candidate.exists():
                    return candidate
                resolved_candidates.append(candidate)
            if resolved_candidates:
                return resolved_candidates[0]
            try:
                return resolve_workspace_path_safe(relative_output)
            except Exception:
                return None

        return self._resolved_output_path

    def _prepare_trace_and_header_artifacts(self, args: HttpDownloadArgs) -> None:
        """Disable artifact-style trace/header outputs for the utility download tool."""
        if args.trace_artifact:
            workspace = self._workspace_root or workspace_root()
            trace_path = resolve_workspace_path_safe(args.trace_artifact, workspace=workspace)
            if trace_path.exists() and trace_path.is_dir():
                raise ValueError("trace_artifact must be a file path, not a directory")
        self._dump_headers_artifact_enabled = False
        self._dump_headers_path = None
        self._trace_mode = "none"
        self._trace_artifact_path = None

    def build_command(self, args: HttpDownloadArgs) -> List[str]:
        """Build argv-only curl command for deterministic file download behavior."""
        self._prepare_output_path(args)
        self._dump_headers_artifact_enabled = False
        self._dump_headers_path = None
        self._trace_mode = "none"
        self._trace_artifact_path = None
        self._http_version_requested = args.http_version
        self._http_version_applied = "auto"
        enforce_capability_checks = args.transport != "pty"
        self._curl_capabilities = (
            detect_curl_http_capabilities()
            if enforce_capability_checks
            else {
                "http2": None,
                "http3": None,
                "source": "deferred_to_runtime_transport",
            }
        )
        self._prepare_trace_and_header_artifacts(args)
        output_relative = self._resolved_output_relative
        if not output_relative:
            raise ValueError("failed to resolve output path")

        cmd = build_curl_common_args(
            timeout=args.timeout,
            follow_redirects=args.follow_redirects,
            max_redirects=args.max_redirects,
            insecure_tls=args.insecure_tls,
            proxy=args.proxy,
            user_agent=args.user_agent,
            connect_timeout=args.connect_timeout,
            speed_limit=args.speed_limit,
            speed_time=args.speed_time,
        )
        cookie_file_relative, cookie_jar_relative = self._resolve_session_paths(args)
        client_cert_relative, client_key_relative, ca_cert_relative = self._resolve_tls_paths(args)
        session_args, cookies_persisted = build_session_curl_args(
            cookie=args.cookie,
            cookie_file=cookie_file_relative,
            cookie_jar=cookie_jar_relative,
            persist_cookies=args.persist_cookies,
            default_cookie_jar="artifacts/http_download_cookies.jar",
        )
        cmd.extend(session_args)
        auth_args, auth_mode_used = build_auth_curl_args(
            auth_mode=args.auth_mode,
            username=args.username,
            password=args.password,
            bearer_token=args.bearer_token,
        )
        cmd.extend(auth_args)
        tls_args = build_tls_curl_args(
            client_cert_path=client_cert_relative,
            client_key_path=client_key_relative,
            client_key_passphrase=args.client_key_passphrase,
            ca_cert_path=ca_cert_relative,
        )
        cmd.extend(tls_args)
        connection_args, connection_applied = build_connection_control_curl_args(
            resolve=args.resolve,
            connect_to=args.connect_to,
            interface=args.interface,
            local_port=args.local_port,
            ipv4_only=args.ipv4_only,
            ipv6_only=args.ipv6_only,
        )
        cmd.extend(connection_args)
        retry_args, retry_applied = build_retry_rate_curl_args(
            retries=args.retries,
            retry_delay=args.retry_delay,
            retry_max_time=args.retry_max_time,
            retry_connrefused=args.retry_connrefused,
            limit_rate=args.limit_rate,
        )
        cmd.extend(retry_args)
        version_args, version_applied = build_http_version_curl_args(
            http_version=args.http_version,
            capabilities=self._curl_capabilities,
            enforce_capability_checks=enforce_capability_checks,
        )
        cmd.extend(version_args)

        if self._resume_mode_used:
            cmd.extend(["--continue-at", "-"])
        cmd.extend(["--output", output_relative])
        if self._dump_headers_path:
            cmd.extend(["--dump-header", self._dump_headers_path])
        if self._trace_artifact_path:
            if self._trace_mode == "trace_ascii":
                cmd.extend(["--trace-ascii", self._trace_artifact_path])
            else:
                cmd.extend(["--trace", self._trace_artifact_path])

        write_out = f"{_WRITE_OUT_MARKER}%{{json}}"
        cmd.extend(["--write-out", write_out])
        cmd.append(args.target)
        self._cookies_persisted = cookies_persisted
        if args.cookie:
            self._session_cookie_source = "inline"
        elif cookie_file_relative:
            self._session_cookie_source = "file"
        else:
            self._session_cookie_source = None
        self._auth_mode_used = auth_mode_used
        self._mtls_used = bool(client_cert_relative or client_key_relative)
        self._ca_cert_used = bool(ca_cert_relative)
        self._connection_controls_applied = connection_applied
        self._transfer_controls_applied = {
            key: value
            for key, value in {
                "connect_timeout": args.connect_timeout,
                "speed_limit": args.speed_limit,
                "speed_time": args.speed_time,
            }.items()
            if value is not None
        }
        self._retry_config_applied = retry_applied
        self._http_version_applied = version_applied
        return cmd

    def prepare_workspace_directories(
        self,
        args: HttpDownloadArgs,
    ) -> List[RuntimeWorkspaceDirectory]:
        directories: set[str] = set()
        for path in (
            self._resolved_output_relative if args.create_parents else None,
            self._dump_headers_path,
            self._trace_artifact_path,
            args.cookie_jar,
        ):
            parent = _workspace_parent_directory(path)
            if parent:
                directories.add(parent)
        return [
            RuntimeWorkspaceDirectory(
                relative_path=path,
                description="http_download runtime output directory",
            )
            for path in sorted(directories)
        ]

    def runtime_output_files(
        self,
        args: HttpDownloadArgs,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[ToolRuntimeOutputFile]:
        """Declare the downloaded file as the primary runtime output."""
        _ = metadata
        relative_output = self._resolved_output_relative or str(args.output_path).strip().replace("\\", "/")
        return [
            ToolRuntimeOutputFile(
                relative_path=relative_output,
                description="http_download destination file",
                required=True,
                min_bytes=args.min_bytes,
                max_bytes=args.max_bytes,
                expected_sha256=args.expected_sha256,
            )
        ]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: HttpDownloadArgs,
    ) -> Dict[str, Any]:
        """Parse curl output and return stable metadata keys."""
        _, write_out = _extract_write_out(stdout)
        _ = stderr

        effective_url = write_out.get("effective_url") or args.target

        return {
            "status_code": write_out.get("http_code"),
            "effective_url": redact_url_credentials(effective_url),
            "download_resumed": self._resume_mode_used,
            "session_cookie_source": self._session_cookie_source,
            "cookies_persisted": self._cookies_persisted,
            "auth_mode_used": self._auth_mode_used if self._auth_mode_used != "none" else args.auth_mode,
            "mtls_used": self._mtls_used,
            "ca_cert_used": self._ca_cert_used,
            "connection_controls_applied": self._connection_controls_applied,
            "transfer_controls_applied": self._transfer_controls_applied,
            "retry_config_applied": self._retry_config_applied,
            "trace_mode": self._trace_mode,
            "trace_artifacts": [path for path in [self._trace_artifact_path, self._dump_headers_path] if path],
            "http_version_requested": self._http_version_requested,
            "http_version_applied": self._http_version_applied,
            "curl_capabilities": dict(self._curl_capabilities),
            "curl_exit_code": exit_code,
            "redirect_count": write_out.get("num_redirects", 0),
            "timing_ms": write_out.get("timing_ms"),
        }

    def postprocess_execution(
        self,
        *,
        args: HttpDownloadArgs,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Optional[Any] = None,
    ) -> ToolPostprocessResult:
        """Apply transport-agnostic integrity checks and output shaping."""
        if metadata.get("postprocess_applied"):
            return ToolPostprocessResult(
                success=success,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                metadata=metadata,
                artifacts=artifacts,
            )

        result_success = bool(success)
        result_exit_code = int(exit_code)
        result_metadata = dict(metadata or {})
        result_stderr = stderr
        result_stdout = stdout

        resolved_output_relative = self._resolved_output_relative or str(args.output_path).strip().replace("\\", "/")
        resolved_output_path = self._resolve_postprocess_output_path(runtime_context)

        error_messages: List[str] = []
        sha256_hex: Optional[str] = None
        bytes_written: Optional[int] = None
        checksum_verified = False

        runtime_output = _runtime_output_metadata_for_path(
            result_metadata.get("runtime_output_files"),
            resolved_output_relative,
        )

        if resolved_output_path is None and runtime_output is None:
            result_success = False
            result_exit_code = 3
            error_messages.append("failed to resolve output path for integrity checks")
        elif result_success:
            status_code = result_metadata.get("status_code")
            if isinstance(status_code, int) and not (200 <= status_code < 400):
                result_success = False
                result_exit_code = 3
                error_messages.append(f"HTTP download returned non-success status_code={status_code}")

            if runtime_output is not None:
                if not runtime_output.get("exists", False):
                    result_success = False
                    result_exit_code = 3
                    error_messages.append("curl reported success but destination file was not created")
                else:
                    try:
                        bytes_written = int(runtime_output.get("size_bytes"))
                    except (TypeError, ValueError):
                        bytes_written = None
                    sha256_hex = str(runtime_output.get("content_sha256") or "").strip().lower() or None
                    if bytes_written is None:
                        result_success = False
                        result_exit_code = 3
                        error_messages.append("downloaded file size could not be verified")
                    elif args.min_bytes is not None and bytes_written < args.min_bytes:
                        result_success = False
                        result_exit_code = 3
                        error_messages.append(
                            f"downloaded file is smaller than min_bytes ({bytes_written} < {args.min_bytes})"
                        )
                    elif args.max_bytes is not None and bytes_written > args.max_bytes:
                        result_success = False
                        result_exit_code = 3
                        error_messages.append(
                            f"downloaded file exceeds max_bytes ({bytes_written} > {args.max_bytes})"
                        )
                    if args.expected_sha256 is not None:
                        checksum_verified = sha256_hex == args.expected_sha256
                        if not checksum_verified:
                            result_success = False
                            result_exit_code = 3
                            error_messages.append("sha256 checksum mismatch")
            elif not resolved_output_path.exists():
                result_success = False
                result_exit_code = 3
                error_messages.append("curl reported success but destination file was not created")
            else:
                bytes_written = resolved_output_path.stat().st_size
                if args.min_bytes is not None and bytes_written < args.min_bytes:
                    result_success = False
                    result_exit_code = 3
                    error_messages.append(
                        f"downloaded file is smaller than min_bytes ({bytes_written} < {args.min_bytes})"
                    )
                if args.max_bytes is not None and bytes_written > args.max_bytes:
                    result_success = False
                    result_exit_code = 3
                    error_messages.append(
                        f"downloaded file exceeds max_bytes ({bytes_written} > {args.max_bytes})"
                    )

                try:
                    _, sha256_hex = compute_checksums(resolved_output_path)
                except Exception as exc:
                    result_success = False
                    result_exit_code = 3
                    error_messages.append(f"failed to compute sha256: {exc}")

                if args.expected_sha256 is not None and sha256_hex is not None:
                    checksum_verified = sha256_hex == args.expected_sha256
                    if not checksum_verified:
                        result_success = False
                        result_exit_code = 3
                        error_messages.append("sha256 checksum mismatch")

        result_metadata.update(
            {
                "saved_path": resolved_output_relative,
                "bytes_written": bytes_written,
                "sha256": sha256_hex,
                "checksum_verified": checksum_verified,
            }
        )

        safe_target = redact_url_credentials(args.target)
        safe_effective_url = redact_url_credentials(result_metadata.get("effective_url"))
        result_metadata["effective_url"] = safe_effective_url

        status_code = result_metadata.get("status_code")
        compact_status = f"status={status_code}" if status_code is not None else "status=unknown"
        compact_bytes = f"bytes={bytes_written}" if bytes_written is not None else "bytes=unknown"
        compact_sha = f"sha256={sha256_hex}" if sha256_hex else "sha256=unavailable"
        compact_action = "Downloaded" if result_success else "Download failed for"
        compact_summary = (
            f"{compact_action} {safe_target} to {resolved_output_relative} "
            f"({compact_status}; {compact_bytes}; {compact_sha})."
        )
        result_metadata.setdefault("compact_summary", compact_summary)
        result_metadata.setdefault(
            "compact_key_findings",
            [
                f"saved_path={resolved_output_relative}",
                compact_bytes,
                compact_sha,
            ],
        )
        result_metadata.setdefault(
            "compact_decision_evidence",
            [
                f"download_intent_satisfied={str(result_success).lower()}",
                f"saved_path={resolved_output_relative}",
                compact_bytes,
                compact_sha,
            ],
        )

        if result_success:
            status_line = f"Downloaded {safe_target} -> {resolved_output_relative}"
            size_line = f"bytes={bytes_written}" if bytes_written is not None else "bytes=unknown"
            sha_line = f"sha256={sha256_hex}" if sha256_hex else "sha256=unavailable"
            result_stdout = f"{status_line}\n{size_line}\n{sha_line}"
        else:
            result_stdout = ""
            if error_messages:
                result_stderr = "\n".join([result_stderr, *error_messages]).strip()

        result_stdout = redact_text_secrets(result_stdout)
        result_stderr = redact_text_secrets(result_stderr)
        result_metadata["postprocess_applied"] = True

        return ToolPostprocessResult(
            success=result_success,
            exit_code=result_exit_code,
            stdout=result_stdout,
            stderr=result_stderr,
            metadata=result_metadata,
            artifacts=list(artifacts or []),
        )

    def create_artifacts(
        self,
        stdout: str,
        args: HttpDownloadArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Do not create artifacts; the downloaded file is the primary workspace output."""
        _ = stdout, args, timestamp
        return []

    def run(self, args: HttpDownloadArgs) -> ToolResult:
        """Execute curl download and apply deterministic integrity validation."""
        start = time.time()

        try:
            cmd = self.build_command(args)
            resolved_output_relative = self._resolved_output_relative
            if resolved_output_relative is None:
                raise ValueError("failed to resolve output path")
        except UnsupportedHttpVersionError as exc:
            return ToolResult(
                success=False,
                exit_code=-3,
                stdout="",
                stderr=str(exc),
                artifacts=[],
                metadata={
                    "error_type": "unsupported_http_version",
                    "http_version_requested": exc.requested,
                    "curl_capabilities": dict(exc.capabilities),
                    "curl_exit_code": -3,
                },
                execution_time=time.time() - start,
            )
        except ValueError as exc:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                artifacts=[],
                metadata={"error_type": "validation_error"},
                execution_time=time.time() - start,
            )

        run_cwd = str(self._workspace_root or workspace_root())
        try:
            materialize_runtime_workspace_preparation(
                workspace=run_cwd,
                directories=self.prepare_workspace_directories(args),
            )
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=False,
                timeout=args.timeout,
                cwd=run_cwd,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={
                    "curl_exit_code": -2,
                    "saved_path": resolved_output_relative,
                },
                execution_time=time.time() - start,
            )

        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        metadata = self.parse_output(stdout_text, stderr_text, proc.returncode, args)

        base_result = ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            artifacts=[],
            metadata=metadata,
            execution_time=time.time() - start,
        )

        try:
            from agent.tool_runtime.runtime_context import get_tool_runtime_context

            runtime_context = get_tool_runtime_context()
        except Exception:
            runtime_context = None

        finalized = self.apply_postprocess_to_tool_result(
            args=args,
            result=base_result,
            runtime_context=runtime_context,
        )

        artifacts = self.create_artifacts(stdout=stdout_text, args=args, timestamp=int(start))
        if artifacts:
            finalized = ToolResult(
                success=finalized.success,
                exit_code=finalized.exit_code,
                stdout=finalized.stdout,
                stderr=finalized.stderr,
                artifacts=list(finalized.artifacts or []) + artifacts,
                metadata=finalized.metadata,
                execution_time=finalized.execution_time,
                validation_errors=finalized.validation_errors,
            )

        return finalized
