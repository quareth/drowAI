"""Tests for credential-safe CORS origin configuration."""

from __future__ import annotations

import pytest

from backend.config import _parse_allowed_origins


def test_allowed_origins_default_to_local_frontend() -> None:
    assert _parse_allowed_origins(None) == (
        "http://localhost:5000",
        "http://127.0.0.1:5000",
    )


def test_allowed_origins_parse_explicit_comma_separated_values() -> None:
    assert _parse_allowed_origins(" https://drowai.example/ , http://localhost:5000 ") == (
        "https://drowai.example",
        "http://localhost:5000",
    )


def test_allowed_origins_reject_wildcard_for_credentialed_requests() -> None:
    with pytest.raises(ValueError, match="must list explicit origins"):
        _parse_allowed_origins("*")
