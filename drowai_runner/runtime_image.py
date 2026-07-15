"""Runtime image manifest parsing and fail-closed verification helpers.

This module keeps runtime-info contract validation deterministic and backend-free
for runner Docker runtime operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime_shared.runtime_manifest import (
    FILE_COMM_SCHEMA_VERSION,
    RUNTIME_CONTRACT_VERSION,
    SEMANTIC_SCHEMA_VERSIONS,
    WORKSPACE_LAYOUT_VERSION,
)


@dataclass(frozen=True, slots=True)
class RuntimeManifestVerification:
    """Result of runtime-info contract parity verification."""

    ok: bool
    mismatch_keys: tuple[str, ...]
    payload: Mapping[str, Any]


def verify_runtime_info_payload(
    payload: Mapping[str, Any],
) -> RuntimeManifestVerification:
    """Validate runtime-info payload against expected shared contract versions."""
    expected = {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
        "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
        "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
    }
    mismatches = tuple(
        key
        for key, expected_value in expected.items()
        if payload.get(key) != expected_value
    )
    return RuntimeManifestVerification(
        ok=not mismatches,
        mismatch_keys=mismatches,
        payload=dict(payload),
    )
