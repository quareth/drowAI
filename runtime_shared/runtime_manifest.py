"""Runtime image manifest contract and deterministic serialization helpers.

This module defines the execution-plane runtime metadata emitted by the
packaged executor daemon for compatibility checks and version probing.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Mapping

RUNTIME_CONTRACT_VERSION = "1.1"
FILE_COMM_SCHEMA_VERSION = "2.0"
WORKSPACE_LAYOUT_VERSION = "2.0"
SEMANTIC_SCHEMA_VERSIONS: Mapping[str, str] = {
    "network": "1.0",
    "web": "1.0",
}
SUPPORTED_TOOL_FAMILIES: tuple[str, ...] = (
    "filesystem",
    "information_gathering",
    "web_applications",
    "password_attacks",
    "maintaining_access",
    "exploitation_tools",
    "sniffing_spoofing",
    "reverse_engineering",
    "stress_testing",
    "system_services",
)


@dataclass(frozen=True)
class RuntimeManifest:
    """Stable runtime metadata contract for runner compatibility checks."""

    runtime_contract_version: str
    source_revision: str
    supported_tool_families: tuple[str, ...]
    file_comm_schema_version: str
    semantic_schema_versions: Mapping[str, str]
    workspace_layout_version: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable mapping representation."""
        payload = asdict(self)
        payload["semantic_schema_versions"] = dict(self.semantic_schema_versions)
        payload["supported_tool_families"] = list(self.supported_tool_families)
        return payload

    def to_json(self) -> str:
        """Return a deterministic JSON string representation."""
        return json.dumps(self.to_dict(), sort_keys=True)


def resolve_source_revision() -> str:
    """Resolve source revision from build/runtime environment variables."""
    for env_name in ("DROWAI_RUNTIME_BUILD_REVISION", "GIT_COMMIT", "SOURCE_REVISION"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return "unknown"


def build_runtime_manifest() -> RuntimeManifest:
    """Build the runtime manifest payload for daemon version probing."""
    return RuntimeManifest(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        source_revision=resolve_source_revision(),
        supported_tool_families=SUPPORTED_TOOL_FAMILIES,
        file_comm_schema_version=FILE_COMM_SCHEMA_VERSION,
        semantic_schema_versions=SEMANTIC_SCHEMA_VERSIONS,
        workspace_layout_version=WORKSPACE_LAYOUT_VERSION,
    )
