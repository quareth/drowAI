"""Shared durable-only masking API for app-owned persistence sinks."""

from __future__ import annotations

from .detectors import detect_durable_secret_spans
from .masker import mask_durable_secrets
from .types import SecretMatch

__all__ = [
    "SecretMatch",
    "detect_durable_secret_spans",
    "mask_durable_secrets",
]

