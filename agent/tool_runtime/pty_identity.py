"""Deterministic identity helpers for isolated parallel PTY tool calls.

The runtime uses one named agent PTY session per parallel tool call. This
module keeps session naming and artifact stamp derivation centralized so the
graph adapter, transport router, and artifact helpers do not grow divergent
identity logic.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9_]+")
_MAX_TOKEN_LEN = 40


@dataclass(frozen=True, slots=True)
class ParallelPtyIdentity:
    """Names used to isolate one parallel PTY-backed tool call."""

    session_name: str
    artifact_stamp: int


def derive_parallel_pty_identity(
    *,
    tool_batch_id: Optional[str],
    tool_call_id: Optional[str],
) -> Optional[ParallelPtyIdentity]:
    """Return deterministic PTY identity for a batch call, or ``None`` if incomplete."""
    if not _has_identity_part(tool_batch_id) or not _has_identity_part(tool_call_id):
        return None

    batch = str(tool_batch_id)
    call = str(tool_call_id)
    digest = hashlib.sha256(f"{batch}:{call}".encode("utf-8")).hexdigest()
    session_name = (
        f"parallel_{_safe_token(batch)}_{_safe_token(call)}_{digest[:10]}"
    )
    artifact_stamp = int(digest[:14], 16)
    return ParallelPtyIdentity(
        session_name=session_name,
        artifact_stamp=artifact_stamp,
    )


def derive_parallel_pty_session_name(
    *,
    tool_batch_id: Optional[str],
    tool_call_id: Optional[str],
) -> Optional[str]:
    """Return the isolated PTY session name for one parallel tool call."""
    identity = derive_parallel_pty_identity(
        tool_batch_id=tool_batch_id,
        tool_call_id=tool_call_id,
    )
    return identity.session_name if identity else None


def derive_artifact_stamp(
    *,
    tool_batch_id: Optional[str],
    tool_call_id: Optional[str],
) -> Optional[int]:
    """Return the deterministic artifact stamp for one parallel tool call."""
    identity = derive_parallel_pty_identity(
        tool_batch_id=tool_batch_id,
        tool_call_id=tool_call_id,
    )
    return identity.artifact_stamp if identity else None


def _has_identity_part(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_token(value: str) -> str:
    token = _SAFE_TOKEN_RE.sub("_", value.strip()).strip("_").lower()
    return (token or "call")[:_MAX_TOKEN_LEN]


__all__ = [
    "ParallelPtyIdentity",
    "derive_artifact_stamp",
    "derive_parallel_pty_identity",
    "derive_parallel_pty_session_name",
]
