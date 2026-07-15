"""HTTP response body/header parsing helpers for curl output.

This module isolates response parsing concerns from command assembly and
redaction so request/download tools can compose stable metadata behavior.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional, Tuple

_STATUS_LINE_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+(.*))?$")


def split_http_response(raw_output: str) -> Tuple[str, str]:
    """Split curl output into the final response header block and body."""
    if not raw_output:
        return "", ""

    text = raw_output.replace("\r\n", "\n")
    parts = text.split("\n\n")
    idx = 0
    last_header_block = ""

    while idx < len(parts) and parts[idx].startswith("HTTP/"):
        last_header_block = parts[idx]
        idx += 1

    body = "\n\n".join(parts[idx:]) if idx < len(parts) else ""
    return last_header_block, body


def parse_status_line(header_block: str) -> Tuple[Optional[int], Optional[str]]:
    """Parse an HTTP status line from a header block."""
    if not header_block:
        return None, None

    first_line = header_block.splitlines()[0].strip()
    match = _STATUS_LINE_RE.match(first_line)
    if not match:
        return None, None
    reason = (match.group(2) or "").strip() or None
    return int(match.group(1)), reason


def parse_response_headers(header_block: str) -> Dict[str, str]:
    """Parse response headers from a raw header block into a dictionary."""
    parsed: Dict[str, str] = {}
    if not header_block:
        return parsed

    lines = header_block.splitlines()[1:]
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key in parsed:
            parsed[key] = f"{parsed[key]}, {value}"
        else:
            parsed[key] = value
    return parsed


def build_multipart_form_args(
    *,
    form_fields: Optional[Mapping[str, str]] = None,
    form_files: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Build curl multipart --form argv segments from field/file mappings."""
    cmd: List[str] = []
    for key, value in dict(form_fields or {}).items():
        cmd.extend(["--form", f"{key}={value}"])
    for key, path in dict(form_files or {}).items():
        cmd.extend(["--form", f"{key}=@{path}"])
    return cmd
