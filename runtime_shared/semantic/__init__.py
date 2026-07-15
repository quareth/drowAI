"""Runtime-safe semantic helper contracts.

This package contains deterministic, backend-free helper functions used by
both runtime-image modules and backend knowledge adapters.
"""

from .canonical_keys import build_finding_vulnerability_key
from .network_common import normalize_service_version
from .web_common import build_finding_subject_key, sanitize_token

__all__ = [
    "build_finding_subject_key",
    "build_finding_vulnerability_key",
    "normalize_service_version",
    "sanitize_token",
]
