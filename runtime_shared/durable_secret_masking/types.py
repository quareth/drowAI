"""Types shared by the durable secret masking package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecretMatch:
    """One reusable secret-like span detected in durable text."""

    start: int
    end: int
    kind: str

