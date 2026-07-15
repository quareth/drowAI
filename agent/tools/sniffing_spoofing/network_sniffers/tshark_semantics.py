"""Rich TShark parsing and semantic helper boundary.

This companion module owns the stable compatibility facade for TShark output
normalization, keyed fingerprint helpers, semantic observation builders, and
semantic evidence builders. Refactored parser modules own the implementation.

This module must not own backend knowledge projection, artifact storage, or
TShark command construction.
"""

from .tshark_parsing.common import (
    DEFAULT_MAX_ROWS,
    DEFAULT_SENSITIVE_PROOF_MODE,
    SECRET_FINGERPRINT_KEY_ENV,
    TSHARK_FIELD_EXTRACT_ALLOWLIST,
    TSHARK_FIELD_NAME_RE,
    TSHARK_SCHEMA_VERSION,
    normalize_tshark_field_extract_fields,
)
from .tshark_parsing.parser import parse_tshark_output
from .tshark_parsing.security import fingerprint_secret
from .tshark_parsing.semantic_emitters import (
    build_tshark_semantic_evidence,
    build_tshark_semantic_observations,
)

__all__ = [
    "build_tshark_semantic_evidence",
    "build_tshark_semantic_observations",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_SENSITIVE_PROOF_MODE",
    "fingerprint_secret",
    "normalize_tshark_field_extract_fields",
    "parse_tshark_output",
    "SECRET_FINGERPRINT_KEY_ENV",
    "TSHARK_FIELD_EXTRACT_ALLOWLIST",
    "TSHARK_FIELD_NAME_RE",
    "TSHARK_SCHEMA_VERSION",
]
