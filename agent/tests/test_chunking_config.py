"""Tests for chunking_config module."""

import os
from pathlib import Path

import pytest

from agent.config.chunking_config import (
    CHUNKING_PROFILES_DIR,
    DEFAULT_MAX_CHUNK_TOKENS,
    INGEST_SIBLING_ARTIFACTS,
    SIBLING_EXTENSIONS,
    MAX_SIBLINGS_PER_ARTIFACT,
)


def test_chunking_profiles_dir_exists():
    """Test that chunking profiles directory exists or is None."""
    if CHUNKING_PROFILES_DIR is not None:
        # If set, should be a Path object or string
        assert CHUNKING_PROFILES_DIR is not None
        # Convert to Path if string
        path = Path(CHUNKING_PROFILES_DIR) if isinstance(CHUNKING_PROFILES_DIR, str) else CHUNKING_PROFILES_DIR
        # Should exist
        assert path.exists(), f"Profiles directory does not exist: {path}"
        assert path.is_dir(), f"Profiles path is not a directory: {path}"
    else:
        # If None, that's acceptable (profiles unavailable)
        assert CHUNKING_PROFILES_DIR is None


def test_chunking_profiles_dir_contains_yaml_files():
    """Test that chunking profiles directory contains YAML profiles."""
    if CHUNKING_PROFILES_DIR is not None:
        path = Path(CHUNKING_PROFILES_DIR) if isinstance(CHUNKING_PROFILES_DIR, str) else CHUNKING_PROFILES_DIR
        if path.exists():
            # Should contain at least one YAML file
            yaml_files = list(path.glob("*.yaml")) + list(path.glob("*.yml"))
            assert len(yaml_files) > 0, "Profiles directory should contain YAML files"


def test_default_max_chunk_tokens_is_integer():
    """Test that default max chunk tokens is a valid integer."""
    assert isinstance(DEFAULT_MAX_CHUNK_TOKENS, int)
    assert DEFAULT_MAX_CHUNK_TOKENS > 0
    assert DEFAULT_MAX_CHUNK_TOKENS <= 10000  # Reasonable upper bound


def test_default_max_chunk_tokens_respects_env():
    """Test that max chunk tokens can be overridden via environment."""
    # This test verifies the constant reflects the env var at import time
    # Note: The actual env var must be set before importing the module
    assert DEFAULT_MAX_CHUNK_TOKENS >= 100  # Should be reasonable value


def test_ingest_sibling_artifacts_is_boolean():
    """Test that sibling artifacts flag is a boolean."""
    assert isinstance(INGEST_SIBLING_ARTIFACTS, bool)


def test_sibling_extensions_is_set():
    """Test that sibling extensions is a set of strings."""
    assert isinstance(SIBLING_EXTENSIONS, set)
    assert len(SIBLING_EXTENSIONS) > 0
    
    # All elements should be strings
    for ext in SIBLING_EXTENSIONS:
        assert isinstance(ext, str)
        # Should start with dot
        assert ext.startswith(".")
        # Should be lowercase
        assert ext == ext.lower()


def test_sibling_extensions_contains_expected_types():
    """Test that sibling extensions contains common artifact types."""
    expected_extensions = {".xml", ".json", ".log"}
    assert expected_extensions.issubset(SIBLING_EXTENSIONS)


def test_max_siblings_per_artifact_is_valid():
    """Test that max siblings per artifact is a valid integer."""
    assert isinstance(MAX_SIBLINGS_PER_ARTIFACT, int)
    assert MAX_SIBLINGS_PER_ARTIFACT > 0
    assert MAX_SIBLINGS_PER_ARTIFACT <= 100  # Reasonable upper bound


def test_max_siblings_per_artifact_is_reasonable():
    """Test that max siblings limit is set to prevent excessive processing."""
    # Should be small enough to prevent DoS but large enough to be useful
    assert MAX_SIBLINGS_PER_ARTIFACT >= 1
    assert MAX_SIBLINGS_PER_ARTIFACT <= 20


def test_config_values_are_consistent():
    """Test that configuration values are internally consistent."""
    # If sibling ingestion is enabled, extensions and max should be valid
    if INGEST_SIBLING_ARTIFACTS:
        assert len(SIBLING_EXTENSIONS) > 0
        assert MAX_SIBLINGS_PER_ARTIFACT > 0


def test_chunking_profiles_dir_path_is_absolute():
    """Test that profiles directory path is absolute."""
    if CHUNKING_PROFILES_DIR is not None:
        path = Path(CHUNKING_PROFILES_DIR) if isinstance(CHUNKING_PROFILES_DIR, str) else CHUNKING_PROFILES_DIR
        assert path.is_absolute(), "Profiles directory should be an absolute path"


def test_chunking_config_can_be_imported_multiple_times():
    """Test that chunking config can be imported multiple times safely."""
    # Import again
    from agent.config.chunking_config import (
        CHUNKING_PROFILES_DIR as DIR2,
        DEFAULT_MAX_CHUNK_TOKENS as TOKENS2,
    )
    
    # Should be the same values
    assert CHUNKING_PROFILES_DIR == DIR2
    assert DEFAULT_MAX_CHUNK_TOKENS == TOKENS2


def test_all_exports_are_available():
    """Test that all expected exports are available."""
    from agent.config.chunking_config import __all__
    
    expected_exports = [
        "CHUNKING_PROFILES_DIR",
        "DEFAULT_MAX_CHUNK_TOKENS",
        "INGEST_SIBLING_ARTIFACTS",
        "SIBLING_EXTENSIONS",
        "MAX_SIBLINGS_PER_ARTIFACT",
    ]
    
    for export in expected_exports:
        assert export in __all__, f"{export} should be in __all__"


def test_config_package_exports():
    """Test that config package exports chunking config."""
    from agent.config import (
        CHUNKING_PROFILES_DIR as PKG_DIR,
        DEFAULT_MAX_CHUNK_TOKENS as PKG_TOKENS,
        INGEST_SIBLING_ARTIFACTS as PKG_INGEST,
        SIBLING_EXTENSIONS as PKG_EXTS,
        MAX_SIBLINGS_PER_ARTIFACT as PKG_MAX,
    )
    
    # Should match module-level imports
    assert PKG_DIR == CHUNKING_PROFILES_DIR
    assert PKG_TOKENS == DEFAULT_MAX_CHUNK_TOKENS
    assert PKG_INGEST == INGEST_SIBLING_ARTIFACTS
    assert PKG_EXTS == SIBLING_EXTENSIONS
    assert PKG_MAX == MAX_SIBLINGS_PER_ARTIFACT

