"""Configuration for artifact chunking and indexing.

Centralizes paths and settings used by SimpleArtifactIngestor
across all execution modes.

This module eliminates fragile relative path dependencies by computing
the profiles directory path once at import time, ensuring consistent
behavior regardless of where the code is invoked from.
"""

from __future__ import annotations

import os
from pathlib import Path


# Chunking profiles directory (tool-specific parsing rules)
# Computed relative to agent/context/chunking/profiles
_AGENT_ROOT = Path(__file__).parent.parent
_CONTEXT_MODULE = _AGENT_ROOT / "context"
CHUNKING_PROFILES_DIR = _CONTEXT_MODULE / "chunking" / "profiles"

# Validate profiles directory exists
if not CHUNKING_PROFILES_DIR.exists():
    import logging
    _logger = logging.getLogger(__name__)
    _logger.warning(
        f"Chunking profiles directory not found: {CHUNKING_PROFILES_DIR}. "
        "Tool-specific parsing rules will not be available."
    )
    # Set to None to indicate unavailability
    CHUNKING_PROFILES_DIR = None


# Default chunk size (tokens)
# Can be overridden via CONTEXT_ARTIFACT_MAX_CHUNK_TOKENS environment variable
DEFAULT_MAX_CHUNK_TOKENS = int(os.getenv("CONTEXT_ARTIFACT_MAX_CHUNK_TOKENS", "800"))


# Sibling artifact ingestion settings
# When enabled, automatically ingests related artifacts (e.g., nmap XML files)
INGEST_SIBLING_ARTIFACTS = os.getenv("CONTEXT_INGEST_SIBLINGS", "true").lower() == "true"

# Extensions to consider for sibling ingestion
# These are commonly produced by pentest tools alongside main output
SIBLING_EXTENSIONS = {".xml", ".json", ".log"}

# Maximum number of sibling artifacts to ingest per primary artifact
# Prevents excessive processing if many files exist in artifacts directory
MAX_SIBLINGS_PER_ARTIFACT = 5


__all__ = [
    "CHUNKING_PROFILES_DIR",
    "DEFAULT_MAX_CHUNK_TOKENS",
    "INGEST_SIBLING_ARTIFACTS",
    "SIBLING_EXTENSIONS",
    "MAX_SIBLINGS_PER_ARTIFACT",
]

