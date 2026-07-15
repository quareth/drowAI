"""HTTP request reconnaissance tool built on curl with structured parsing.

This module provides `information_gathering.web_enumeration.http_request`.
Responsibilities:
- build safe argv-only curl commands using shared helper primitives,
- parse response status/headers/body into deterministic metadata,
- redact sensitive output by default,
- persist optional artifacts for large response bodies and header snapshots.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from ...base_tool import BaseTool
from ...filesystem._helpers import resolve_workspace_path_safe, to_workspace_relative, workspace_root
from ...schemas import ToolResult
from .contracts import HttpRequestArgs
from runtime_shared.workspace_files import (
    RuntimeWorkspaceDirectory,
    RuntimeWorkspaceFile,
    RuntimeWorkspaceFileError,
    materialize_runtime_workspace_preparation,
)
from ._helpers import (
    UnsupportedHttpVersionError,
    build_auth_curl_args,
    build_connection_control_curl_args,
    build_curl_common_args,
    build_http_version_curl_args,
    build_multipart_form_args,
    build_retry_rate_curl_args,
    build_session_curl_args,
    build_tls_curl_args,
    detect_curl_http_capabilities,
    parse_response_headers,
    parse_status_line,
    redact_sensitive_headers,
    redact_text_secrets,
    redact_url_credentials,
    split_http_response,
)

_WRITE_OUT_MARKER = "__DROWAI_HTTP_META__"
logger = logging.getLogger(__name__)


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
    parts = payload.split("\t")
    if len(parts) != 6:
        return output_without_trailer, {}

    http_code_raw, effective_url, content_type, size_download, num_redirects, time_total = parts
    parsed: Dict[str, Any] = {}
    try:
        parsed["http_code"] = int(http_code_raw)
    except Exception:
        pass
    parsed["effective_url"] = effective_url or None
    parsed["content_type"] = content_type or None
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


def _pick_header_case_insensitive(headers: Dict[str, str], header_name: str) -> Optional[str]:
    """Return a header value using case-insensitive lookup."""
    target = header_name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _body_extension_for_content_type(content_type: Optional[str]) -> str:
    """Determine artifact extension from Content-Type."""
    if not content_type:
        return "txt"
    ctype = content_type.lower()
    if "json" in ctype:
        return "json"
    if "html" in ctype:
        return "html"
    if ctype.startswith("text/"):
        return "txt"
    return "bin"


class HttpRequestTool(BaseTool):
    """Perform HTTP requests with structured output and secure defaults."""

    args_model = HttpRequestArgs

    def __init__(self) -> None:
        super().__init__()
        self._last_header_block: str = ""
        self._last_response_body: str = ""
        self._last_body_for_stdout: str = ""
        self._last_body_truncated: bool = False
        self._last_content_type: Optional[str] = None
        self._last_should_write_headers_artifact: bool = False
        self._cookies_persisted: bool = False
        self._session_cookie_source: Optional[str] = None
        self._workspace_root: Optional[Path] = None
        self._multipart_used: bool = False
        self._auth_mode_used: str = "none"
        self._mtls_used: bool = False
        self._ca_cert_used: bool = False
        self._connection_controls_applied: Dict[str, Any] = {}
        self._retry_config_applied: Dict[str, Any] = {}
        self._binary_body_used: bool = False
        self._binary_response_detected: bool = False
        self._response_mode: str = "text"
        self._http_version_requested: str = "auto"
        self._http_version_applied: str = "auto"
        self._curl_capabilities: Dict[str, Any] = {}
        self._artifact_token: str = "default"

    def _artifact_name(self, stem: str, extension: str) -> str:
        """Build per-invocation artifact name to avoid concurrent collisions."""
        return f"{stem}_{self._artifact_token}.{extension}"

    def _resolve_binary_body_source(self, args: HttpRequestArgs) -> Optional[str]:
        """Resolve binary request body source into workspace-relative path if used."""
        if args.body_file_path is None and args.body_base64 is None:
            return None

        workspace = self._workspace_root or workspace_root()
        self._workspace_root = workspace

        if args.body_file_path is not None:
            body_path = resolve_workspace_path_safe(args.body_file_path, workspace=workspace)
            if not body_path.exists() or not body_path.is_file():
                raise ValueError("body_file_path must point to an existing file inside workspace")
            return to_workspace_relative(body_path, workspace)

        # body_base64 path
        try:
            decoded = base64.b64decode(args.body_base64 or "", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("body_base64 must be valid base64") from exc
        _ = decoded
        return f"artifacts/{self._artifact_name('http_request_body_base64', 'bin')}"

    def _resolve_session_paths(self, args: HttpRequestArgs) -> Tuple[Optional[str], Optional[str]]:
        """Resolve cookie input file paths into workspace-relative safe paths."""
        cookie_file_relative: Optional[str] = None

        if args.cookie_file:
            workspace = self._workspace_root or workspace_root()
            self._workspace_root = workspace
        else:
            return None, None

        if args.cookie_file:
            resolved_cookie_file = resolve_workspace_path_safe(args.cookie_file, workspace=workspace)
            if not resolved_cookie_file.exists() or not resolved_cookie_file.is_file():
                raise ValueError("cookie_file must point to an existing file inside workspace")
            cookie_file_relative = to_workspace_relative(resolved_cookie_file, workspace)

        return cookie_file_relative, None

    def _resolve_multipart_files(self, args: HttpRequestArgs) -> Dict[str, str]:
        """Resolve multipart upload file paths into workspace-relative safe paths."""
        resolved_form_files: Dict[str, str] = {}
        if not args.form_files:
            return resolved_form_files

        workspace = self._workspace_root or workspace_root()
        self._workspace_root = workspace
        for field_name, upload_path in args.form_files.items():
            resolved_upload = resolve_workspace_path_safe(upload_path, workspace=workspace)
            if not resolved_upload.exists() or not resolved_upload.is_file():
                raise ValueError(f"form_files '{field_name}' must point to an existing file inside workspace")
            resolved_form_files[field_name] = to_workspace_relative(resolved_upload, workspace)

        return resolved_form_files

    def _resolve_tls_paths(
        self,
        args: HttpRequestArgs,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve mTLS certificate/key/CA paths into workspace-relative safe paths."""
        if not (args.client_cert_path or args.client_key_path or args.ca_cert_path):
            return None, None, None

        workspace = self._workspace_root or workspace_root()
        self._workspace_root = workspace
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

    def build_command(self, args: HttpRequestArgs) -> List[str]:
        """Build argv-only curl command with deterministic trailer metadata."""
        self._workspace_root = None
        self._binary_body_used = False
        self._binary_response_detected = False
        self._response_mode = "text"
        self._http_version_requested = args.http_version
        self._http_version_applied = "auto"
        self._artifact_token = uuid4().hex
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
        cookie_file_relative, _ = self._resolve_session_paths(args)
        resolved_form_files = self._resolve_multipart_files(args)
        client_cert_relative, client_key_relative, ca_cert_relative = self._resolve_tls_paths(args)
        binary_body_source = self._resolve_binary_body_source(args)
        cmd = build_curl_common_args(
            timeout=args.timeout,
            follow_redirects=args.follow_redirects,
            max_redirects=args.max_redirects,
            insecure_tls=args.insecure_tls,
            proxy=args.proxy,
            user_agent=args.user_agent,
            headers=args.headers,
            content_type=args.content_type,
        )
        session_args, cookies_persisted = build_session_curl_args(
            cookie=args.cookie,
            cookie_file=cookie_file_relative,
            cookie_jar=None,
            persist_cookies=False,
            default_cookie_jar=None,
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
        multipart_args = build_multipart_form_args(
            form_fields=args.form_fields,
            form_files=resolved_form_files,
        )
        cmd.extend(multipart_args)
        effective_method = "POST" if multipart_args and args.method == "GET" else args.method
        if effective_method == "HEAD":
            cmd.append("--head")
        else:
            cmd.extend(["--request", effective_method])
        cmd.append("--include")
        if args.body is not None:
            cmd.extend(["--data", args.body])
        elif binary_body_source is not None:
            cmd.extend(["--data-binary", f"@{binary_body_source}"])
            self._binary_body_used = True

        write_out = (
            f"\n{_WRITE_OUT_MARKER}"
            "%{http_code}\t%{url_effective}\t%{content_type}\t%{size_download}\t%{num_redirects}\t%{time_total}"
        )
        cmd.extend(["--write-out", write_out])
        cmd.append(args.target)
        self._cookies_persisted = cookies_persisted
        if args.cookie:
            self._session_cookie_source = "inline"
        elif cookie_file_relative:
            self._session_cookie_source = "file"
        else:
            self._session_cookie_source = None
        self._multipart_used = bool(multipart_args)
        self._auth_mode_used = auth_mode_used
        self._mtls_used = bool(client_cert_relative or client_key_relative)
        self._ca_cert_used = bool(ca_cert_relative)
        self._connection_controls_applied = connection_applied
        self._retry_config_applied = retry_applied
        self._http_version_applied = version_applied
        return cmd

    def prepare_workspace_files(self, args: HttpRequestArgs) -> List[RuntimeWorkspaceFile]:
        if args.body_base64 is None:
            return []
        try:
            decoded = base64.b64decode(args.body_base64 or "", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeWorkspaceFileError("body_base64 must be valid base64") from exc
        return [
            RuntimeWorkspaceFile.from_bytes(
                relative_path=f"artifacts/{self._artifact_name('http_request_body_base64', 'bin')}",
                content=decoded,
                description="http_request decoded body_base64 payload",
            )
        ]

    def prepare_workspace_directories(
        self,
        args: HttpRequestArgs,
    ) -> List[RuntimeWorkspaceDirectory]:
        directories: set[str] = set()
        if args.body_base64 is not None:
            directories.add("artifacts")
        return [
            RuntimeWorkspaceDirectory(
                relative_path=path,
                description="http_request runtime output directory",
            )
            for path in sorted(directories)
        ]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: HttpRequestArgs,
    ) -> Dict[str, Any]:
        """Parse curl output into stable metadata keys for reasoning."""
        response_text, write_out = _extract_write_out(stdout)
        header_block, body = split_http_response(response_text)
        self._binary_response_detected = "\x00" in body
        status_code, reason_phrase = parse_status_line(header_block)
        response_headers = parse_response_headers(header_block)

        header_content_type = _pick_header_case_insensitive(response_headers, "Content-Type")
        content_type = header_content_type or write_out.get("content_type")
        content_length_raw = _pick_header_case_insensitive(response_headers, "Content-Length")
        content_length: Optional[int] = None
        if content_length_raw:
            try:
                content_length = int(content_length_raw)
            except Exception:
                content_length = None
        if content_length is None and isinstance(write_out.get("size_download"), int):
            content_length = int(write_out["size_download"])

        body_bytes = body.encode("utf-8", errors="replace")
        body_truncated = False
        body_for_stdout = ""
        if args.capture_body and self._response_mode == "text":
            if len(body_bytes) > args.max_body_bytes:
                body_for_stdout = body_bytes[: args.max_body_bytes].decode("utf-8", errors="replace")
                body_truncated = True
            else:
                body_for_stdout = body

        safe_response_headers = redact_sensitive_headers(response_headers) if args.redact_output else response_headers
        safe_request_headers = redact_sensitive_headers(args.headers) if args.redact_output else dict(args.headers)
        safe_body_for_stdout = redact_text_secrets(body_for_stdout) if args.redact_output else body_for_stdout

        resolved_status_code = status_code
        if resolved_status_code is None and isinstance(write_out.get("http_code"), int):
            resolved_status_code = write_out["http_code"]

        self._last_header_block = header_block
        self._last_response_body = body
        self._last_body_for_stdout = safe_body_for_stdout
        self._last_body_truncated = body_truncated
        self._last_content_type = content_type
        self._last_should_write_headers_artifact = bool(header_block) and (
            body_truncated or bool(write_out.get("num_redirects", 0)) or bool(stderr)
        )

        effective_url = write_out.get("effective_url") or args.target
        safe_effective_url = redact_url_credentials(effective_url) if args.redact_output else effective_url

        return {
            "execution_outcome": "succeeded" if exit_code == 0 else "failed",
            "status_code": resolved_status_code,
            "reason_phrase": reason_phrase,
            "effective_url": safe_effective_url,
            "response_headers": safe_response_headers,
            "content_type": content_type,
            "content_length": content_length,
            "redirect_count": write_out.get("num_redirects", 0),
            "timing_ms": write_out.get("timing_ms"),
            "body_truncated": body_truncated,
            "curl_exit_code": exit_code,
            "request_method": ("POST" if self._multipart_used and args.method == "GET" else args.method),
            "request_headers": safe_request_headers,
            "body_captured": bool(args.capture_body),
            "session_cookie_source": self._session_cookie_source,
            "cookies_persisted": self._cookies_persisted,
            "multipart_used": self._multipart_used,
            "auth_mode_used": self._auth_mode_used if self._auth_mode_used != "none" else args.auth_mode,
            "mtls_used": self._mtls_used,
            "ca_cert_used": self._ca_cert_used,
            "connection_controls_applied": self._connection_controls_applied,
            "retry_config_applied": self._retry_config_applied,
            "binary_body_used": self._binary_body_used,
            "binary_response_detected": self._binary_response_detected,
            "response_mode": self._response_mode,
            "trace_mode": "none",
            "trace_artifacts": [],
            "http_version_requested": self._http_version_requested,
            "http_version_applied": self._http_version_applied,
            "curl_capabilities": dict(self._curl_capabilities),
        }

    def render_result_output(
        self,
        args: HttpRequestArgs,
        stdout: str,
        stderr: str,
    ) -> Tuple[str, str]:
        """Render final stdout/stderr exactly like direct run() output."""
        _ = stdout
        output_sections: List[str] = []
        if self._last_header_block:
            output_sections.append(self._last_header_block)
        if args.capture_body and self._last_body_for_stdout:
            output_sections.append(self._last_body_for_stdout)
        stdout_out = "\n\n".join(output_sections)
        if self._last_body_truncated:
            stdout_out = (
                f"{stdout_out}\n\n[body truncated to {args.max_body_bytes} bytes; full response saved as artifact]"
                if stdout_out
                else f"[body truncated to {args.max_body_bytes} bytes; full response saved as artifact]"
            )

        stderr_out = stderr
        if args.redact_output:
            stdout_out = redact_text_secrets(stdout_out)
            stderr_out = redact_text_secrets(stderr_out)
        return stdout_out, stderr_out

    def create_artifacts(
        self,
        stdout: str,
        args: HttpRequestArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Persist large response body and optional header snapshot artifacts."""
        _ = stdout, args
        artifacts: List[str] = []
        ts = timestamp if timestamp is not None else int(time.time())

        def _redact_and_include_text_artifact(path: Optional[str]) -> bool:
            """Redact text artifact content and include only on successful rewrite."""
            if not path or not os.path.exists(path):
                return False
            artifact_path = Path(path)
            try:
                redacted = redact_text_secrets(artifact_path.read_text(encoding="utf-8", errors="replace"))
                artifact_path.write_text(redacted, encoding="utf-8")
            except Exception as exc:
                logger.warning("Skipping unsafe http_request artifact %s: %s", path, exc)
                try:
                    artifact_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return False
            return True

        try:
            os.makedirs("artifacts", exist_ok=True)
            full_body_bytes = self._last_response_body.encode("utf-8", errors="replace")
            if self._last_body_truncated and full_body_bytes:
                ext = _body_extension_for_content_type(self._last_content_type)
                body_path = f"artifacts/http_request_{ts}.{ext}"
                with open(body_path, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(self._last_response_body)
                if _redact_and_include_text_artifact(body_path):
                    artifacts.append(body_path)

            if self._last_should_write_headers_artifact and self._last_header_block:
                header_path = f"artifacts/http_request_{ts}_headers.txt"
                with open(header_path, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(self._last_header_block)
                if _redact_and_include_text_artifact(header_path):
                    artifacts.append(header_path)
        except Exception as exc:
            logger.warning("Failed to persist http_request artifacts: %s", exc)

        return artifacts

    def run(self, args: HttpRequestArgs) -> ToolResult:
        """Execute curl request and return structured, redacted output."""
        start = time.time()
        try:
            cmd = self.build_command(args)
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

        try:
            run_kwargs: Dict[str, Any] = {
                "capture_output": True,
                "text": False,
                "timeout": args.timeout,
            }
            if self._workspace_root is not None:
                run_kwargs["cwd"] = str(self._workspace_root)
                materialize_runtime_workspace_preparation(
                    workspace=self._workspace_root,
                    files=self.prepare_workspace_files(args),
                    directories=self.prepare_workspace_directories(args),
                )
            proc = subprocess.run(cmd, **run_kwargs)
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={"curl_exit_code": -2},
                execution_time=time.time() - start,
            )

        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        stderr_text = proc.stderr.decode("utf-8", errors="replace")

        metadata = self.parse_output(stdout_text, stderr_text, proc.returncode, args)
        artifacts = self.create_artifacts(stdout=stdout_text, args=args, timestamp=int(start))

        stdout_out, stderr_text = self.render_result_output(
            args=args,
            stdout=stdout_text,
            stderr=stderr_text,
        )

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=stdout_out,
            stderr=stderr_text,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )
