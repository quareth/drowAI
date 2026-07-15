"""Metadata registrations for Kali runtime filesystem and shell utility tools.

These are bulk-registered since they follow a common pattern.
"""

from .enhanced_metadata_registry import (
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCatalogRole,
    ToolCategory,
    PentestPhase,
)


# ---------------------------------------------------------------------------
# Kali runtime filesystem utilities (filesystem.* namespace)
# Catalog descriptions follow the runbook shape:
#   <verb> <object/scope> <input shape>; returns <evidence/output>; <boundary>.
# See docs/runbooks/tool-catalog-description-optimization.md
# ---------------------------------------------------------------------------
for tool_id, display_name, capability_name, capability_desc in [
    ("filesystem.read_file", "Kali Runtime Read File", "read_file",
     "Read a file inside the active Kali runtime without modifying it; relative paths resolve from /workspace and absolute paths are allowed."),
    ("filesystem.write_file", "Kali Runtime Write File", "write_file",
     "Create or replace an entire file inside the active Kali runtime; relative paths resolve from /workspace and absolute paths are allowed."),
    ("filesystem.append_file", "Kali Runtime Append File", "append_file",
     "Append content to the end of an existing file inside the active Kali runtime without replacing existing content."),
    ("filesystem.edit_lines", "Kali Runtime Edit Lines", "edit_lines",
     "Edit specific line ranges in a file inside the active Kali runtime for targeted replace, insert, or delete operations; not a whole-file rewrite."),
    ("filesystem.read_head", "Read File Head", "read_head",
     "Read the first N lines of a workspace file; returns leading content for previewing structure or headers; not for full-file scans."),
    ("filesystem.read_tail", "Read File Tail", "read_tail",
     "Read the last N lines of a workspace file; returns trailing content for inspecting recent log entries or latest output; not for full-file scans."),
    ("filesystem.grep", "Search File Content", "grep",
     "Search a single workspace file for lines matching a regex; returns matching lines with line numbers; use for in-file lookups, not cross-file search."),
    ("filesystem.delete_path", "Kali Runtime Delete Path", "delete_path",
     "Remove a file or directory inside the active Kali runtime recursively; not reversible; only for paths no longer needed."),
    ("filesystem.make_dir", "Kali Runtime Make Directory", "make_dir",
     "Create a directory inside the active Kali runtime including any missing parents; returns the created path; not for file creation."),
    ("filesystem.list_dir", "Kali Runtime List Directory", "list_dir",
     "List entries in a directory inside the active Kali runtime with name, size, and type; returns a directory listing; not for file content."),
    ("filesystem.move_path", "Kali Runtime Move Path", "move_path",
     "Move or rename a file or directory inside the active Kali runtime to a new path; leaves no copy at the source; not for duplication."),
    ("filesystem.copy_path", "Kali Runtime Copy Path", "copy_path",
     "Copy a file or directory inside the active Kali runtime to a new path; preserves the source; not for moving or renaming."),
    ("filesystem.stat_path", "Kali Runtime Stat Path", "stat_path",
     "Retrieve metadata about a path inside the active Kali runtime; returns size, type, permissions, and timestamps; not for reading content."),
    ("filesystem.find_paths", "Kali Runtime Find Paths", "find_paths",
     "Locate files or directories inside the active Kali runtime matching name, glob, or type filters; use path='/' to search all of Kali, path='.' for /workspace."),
    ("filesystem.search_text", "Kali Runtime Search Text", "search_text",
     "Search files inside the active Kali runtime recursively for a regex pattern; use path='/' for all of Kali or path='.' for /workspace."),
]:
    register_enhanced_tool_metadata(
        EnhancedToolMetadata(
            tool_id=tool_id,
            display_name=display_name,
            category=ToolCategory.WORKSPACE_FILESYSTEM,
            catalog_role=ToolCatalogRole.UTILITY,
            applicable_phases=[
                PentestPhase.RECONNAISSANCE,
                PentestPhase.ENUMERATION,
                PentestPhase.POST_EXPLOITATION,
            ],
            capabilities=[
                ToolCapability(
                    name=capability_name,
                    description=capability_desc,
                    output_indicators=["path", "content"],
                )
            ],
            required_services=[],
            target_protocols=["local"],
            execution_priority=5,
            parallel_compatible=True,
            stealth_level=5,
            estimated_runtime_minutes=1,
        )
    )


# ---------------------------------------------------------------------------
# Shell utilities (shell.* namespace)
# ---------------------------------------------------------------------------
for tool_id, display_name, capability_name, capability_desc in [
    ("shell.exec", "Shell Command Executor", "shell_command",
     "Execute one guarded shell command in the task workspace or runtime; returns stdout, stderr, exit code, and artifacts."),
    ("shell.script", "Workspace Script Runner", "shell_script",
     "Execute a guarded multi-line shell script in the task workspace or runtime; returns stdout, stderr, and exit code; use when one command is not enough."),
]:
    register_enhanced_tool_metadata(
        EnhancedToolMetadata(
            tool_id=tool_id,
            display_name=display_name,
            category=ToolCategory.SHELL,
            catalog_role=ToolCatalogRole.UTILITY,
            applicable_phases=[
                PentestPhase.RECONNAISSANCE,
                PentestPhase.ENUMERATION,
                PentestPhase.POST_EXPLOITATION,
            ],
            capabilities=[
                ToolCapability(
                    name=capability_name,
                    description=capability_desc,
                    output_indicators=["stdout", "stderr"],
                )
            ],
            required_services=[],
            target_protocols=["local"],
            execution_priority=4,
            parallel_compatible=False,
            stealth_level=3,
            estimated_runtime_minutes=2,
            supported_transports=["file-comm", "pty"],
        )
    )


# ---------------------------------------------------------------------------
# Service access utilities (service_access.* namespace)
# ---------------------------------------------------------------------------
for tool_id, display_name, capability_name, capability_desc, protocols in [
    (
        "service_access.ftp_login",
        "FTP Login Proof",
        "ftp_login_proof",
        "Authenticate to one FTP service with supplied credentials and report whether login succeeds; no brute force and no interactive session.",
        ["ftp", "tcp"],
    ),
    (
        "service_access.ftp_list",
        "FTP Directory List",
        "ftp_directory_list",
        "Authenticate to one FTP service with supplied credentials and list one known remote directory; no brute force and no interactive session.",
        ["ftp", "tcp"],
    ),
    (
        "service_access.ftp_download",
        "FTP File Download",
        "ftp_file_download",
        "Authenticate to one FTP service with supplied credentials and download one known remote file into the task workspace without creating artifact duplicates.",
        ["ftp", "tcp"],
    ),
    (
        "service_access.ssh_login",
        "SSH Login Proof",
        "ssh_login_proof",
        "Authenticate to one SSH service with a supplied password and run an inert proof command; no brute force and no interactive shell.",
        ["ssh", "tcp"],
    ),
]:
    register_enhanced_tool_metadata(
        EnhancedToolMetadata(
            tool_id=tool_id,
            display_name=display_name,
            category=ToolCategory.SERVICE_ACCESS,
            catalog_role=ToolCatalogRole.UTILITY,
            applicable_phases=[
                PentestPhase.ENUMERATION,
                PentestPhase.POST_EXPLOITATION,
            ],
            capabilities=[
                ToolCapability(
                    name=capability_name,
                    description=capability_desc,
                    output_indicators=["auth_success", "stdout", "stderr", "metadata"],
                )
            ],
            required_services=[],
            target_protocols=protocols,
            execution_priority=5,
            parallel_compatible=False,
            stealth_level=3,
            estimated_runtime_minutes=1,
            supported_transports=["file-comm", "pty"],
        )
    )


# ---------------------------------------------------------------------------
# Catalog-only metadata for legacy network-discovery ids
# ---------------------------------------------------------------------------
register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="information_gathering.network_discovery.netdiscover",
        display_name="Netdiscover",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[
            PentestPhase.RECONNAISSANCE,
            PentestPhase.ENUMERATION,
        ],
        capabilities=[
            ToolCapability(
                name="arp_host_discovery",
                description=(
                    "Discover local Layer 2 hosts with ARP sweeps; returns "
                    "MAC/IP/vendor evidence; prefer for same-LAN host "
                    "discovery, not for port discovery"
                ),
                output_indicators=["ip", "mac", "vendor"],
            )
        ],
        required_services=[],
        target_protocols=["arp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=2,
    )
)


# ---------------------------------------------------------------------------
# HTTP web enumeration utilities (information_gathering.web_enumeration.*)
# ---------------------------------------------------------------------------
for (
    tool_id,
    display_name,
    capability_name,
    capability_desc,
    output_indicators,
    catalog_role,
) in [
    (
        "information_gathering.web_enumeration.http_request",
        "HTTP Request",
        "http_probe",
        (
            "Fetch one known HTTP(S) URL and return status, headers, body preview, redirects, "
            "timing, and artifacts; not for crawling or fuzzing"
        ),
        [
            "status_code",
            "response_headers",
            "content_type",
            "content_length",
            "timing_ms",
            "auth_mode_used",
            "session_cookie_source",
            "multipart_used",
            "mtls_used",
            "trace_mode",
            "http_version_applied",
        ],
        ToolCatalogRole.PENTEST,
    ),
    (
        "information_gathering.web_enumeration.http_download",
        "HTTP Download",
        "file_download",
        (
            "Download one known HTTP(S) resource to workspace and return path, size, sha256, "
            "status, and timing; not for crawling or fuzzing"
        ),
        [
            "status_code",
            "saved_path",
            "bytes_written",
            "sha256",
            "timing_ms",
            "auth_mode_used",
            "session_cookie_source",
            "mtls_used",
            "trace_mode",
            "http_version_applied",
        ],
        ToolCatalogRole.UTILITY,
    ),
]:
    register_enhanced_tool_metadata(
        EnhancedToolMetadata(
            tool_id=tool_id,
            display_name=display_name,
            category=ToolCategory.WEB_ENUMERATION,
            catalog_role=catalog_role,
            applicable_phases=[
                PentestPhase.RECONNAISSANCE,
                PentestPhase.ENUMERATION,
            ],
            capabilities=[
                ToolCapability(
                    name=capability_name,
                    description=capability_desc,
                    output_indicators=output_indicators,
                )
            ],
            required_services=[],
            target_protocols=["http", "https"],
            execution_priority=8,
            parallel_compatible=True,
            stealth_level=5,
            estimated_runtime_minutes=2,
            supported_transports=["file-comm", "pty"],
        )
    )


# ---------------------------------------------------------------------------
# fs.* namespace DEPRECATED - use filesystem.* namespace instead
# The fs.* aliases are handled by agent/tools/filesystem/aliases.py
# ---------------------------------------------------------------------------
